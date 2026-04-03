#!/usr/bin/env python3
"""
AIMOS Shared Listener — v4.2.1 (CR-157: At-Least-Once Delivery)
=================================================================
Unified I/O relay: polls Telegram bots AND IMAP mailboxes for all agents.
Incoming messages → pending_messages table → Orchestrator wakes the agent.

Zero VRAM usage — no LLM loaded.

Usage:
  python scripts/shared_listener.py
"""

import asyncio
import email as email_lib
import email.header
import imaplib
import json
import logging
import os
import signal
import ssl
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import psycopg2.extras
from core.config import Config

# CR-171: Log rotation for shared listener
_log_dir = Path(__file__).parent.parent / "logs"
_log_dir.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = RotatingFileHandler(
    _log_dir / "shared_listener.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger("AIMOS.listener")

_MAIL_POLL_INTERVAL = 20  # S6: reduced from 60s for faster email response
_DEDUP_WINDOW = 30  # seconds — ignore identical messages within this window
MAX_FILE_SIZE = 50 * 1024 * 1024  # CR-176: 50 MB file size limit for downloads

import hashlib
import time
import unicodedata


# ── CR-214: Filename sanitization ────────────────────────────────────────
# Prevents path traversal, executable injection, and special character attacks.
_BLOCKED_EXTENSIONS = frozenset({
    ".py", ".pyc", ".pyo", ".sh", ".bash", ".zsh", ".fish",
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif",
    ".jar", ".class", ".js", ".vbs", ".wsf", ".ps1", ".psm1",
    ".rb", ".pl", ".php", ".cgi", ".so", ".dll", ".dylib",
    ".app", ".elf", ".bin", ".run",
    ".db", ".sqlite", ".sqlite3",  # protect internal databases
})

def _sanitize_filename(raw_name: str, fallback_prefix: str = "file") -> str:
    """Make a user-supplied filename safe for local storage.

    - Strips path components (../../ etc)
    - Removes null bytes, control chars, shell metacharacters
    - Normalizes Unicode (NFC) to prevent homoglyph attacks
    - Blocks executable and database extensions
    - Limits length to 200 chars
    - Returns a safe fallback if nothing usable remains
    """
    if not raw_name:
        return f"{fallback_prefix}_{hashlib.md5(str(time.time()).encode(), usedforsecurity=False).hexdigest()[:8]}"

    # Strip path components — only keep the filename
    name = os.path.basename(raw_name)

    # Remove null bytes and control characters
    name = name.replace("\x00", "")
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")

    # Normalize Unicode
    name = unicodedata.normalize("NFC", name)

    # Replace shell metacharacters and problematic chars
    for ch in '`$|;&(){}[]!#~\'"\\<>':
        name = name.replace(ch, "_")

    # Replace whitespace with underscore
    name = "_".join(name.split())

    # Remove leading dots (hidden files)
    name = name.lstrip(".")

    # Limit length (keep extension)
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[:200 - len(ext)] + ext

    # Block dangerous extensions
    _, ext = os.path.splitext(name.lower())
    if ext in _BLOCKED_EXTENSIONS:
        name = name + ".blocked"
        log.warning(f"[CR-214] Blocked dangerous extension: {raw_name} → {name}")

    # Final fallback
    if not name or name == ".blocked":
        name = f"{fallback_prefix}_{hashlib.md5(raw_name.encode(), usedforsecurity=False).hexdigest()[:8]}"

    return name

# CR-165: Exponential backoff on Telegram 429 (rate limit) errors
_tg_backoff = 1  # seconds, doubles on each 429, resets on success

# CR-169: Telegram health check — track last getMe() call per bot
_TG_HEALTH_INTERVAL = 300  # seconds (5 minutes)
_last_tg_health: dict[str, float] = {}  # agent_name → monotonic timestamp

# In-memory dedup cache: hash → timestamp
_recent_hashes: dict[str, float] = {}

# CR-157: Retry queue for failed DB inserts (at-least-once delivery)
# Each entry: dict with keys: agent_name, sender_id, content, kind, file_path, retries
_retry_queue: list[dict] = []
_RETRY_MAX = 5  # drop after this many failed attempts

# CR-212: Rate limiting per sender
_sender_counts: dict[str, list[float]] = {}  # "agent:sender_id" → [timestamps]
_RATE_LIMIT_SOFT = 10   # messages per minute → "please wait"
_RATE_LIMIT_HARD = 20   # messages per 5 minutes → block for 30 min
_blocked_senders: dict[str, float] = {}  # "agent:sender_id" → block_until_timestamp


def _check_rate_limit(agent_name: str, sender_id: int) -> str | None:
    """CR-212: Check if sender is rate-limited. Returns warning message or None."""
    key = f"{agent_name}:{sender_id}"
    now = time.monotonic()

    # Check block
    if key in _blocked_senders:
        if now < _blocked_senders[key]:
            return "blocked"
        del _blocked_senders[key]

    # Periodic cleanup of stale entries (every 100th call)
    if len(_sender_counts) > 100:
        stale_keys = [k for k, v in _sender_counts.items() if not v or now - max(v) > 600]
        for k in stale_keys:
            del _sender_counts[k]
        stale_blocks = [k for k, t in _blocked_senders.items() if now > t]
        for k in stale_blocks:
            del _blocked_senders[k]

    # Track timestamps
    if key not in _sender_counts:
        _sender_counts[key] = []
    _sender_counts[key].append(now)
    # Clean old entries (5 min window)
    _sender_counts[key] = [t for t in _sender_counts[key] if now - t < 300]

    recent_1min = sum(1 for t in _sender_counts[key] if now - t < 60)
    recent_5min = len(_sender_counts[key])

    if recent_5min >= _RATE_LIMIT_HARD:
        _blocked_senders[key] = now + 1800  # Block 30 min
        log.warning(f"[CR-212] BLOCKED: {key} ({recent_5min} msgs in 5min) for 30 minutes")
        return "blocked"
    elif recent_1min >= _RATE_LIMIT_SOFT:
        log.info(f"[CR-212] Rate limit soft: {key} ({recent_1min} msgs in 1min)")
        return "throttled"

    return None


def _is_duplicate(agent_name: str, sender_id: int, content: str) -> bool:
    """Check if the same message was already enqueued within _DEDUP_WINDOW seconds."""
    now = time.monotonic()
    # Clean old entries
    stale = [k for k, t in _recent_hashes.items() if now - t > _DEDUP_WINDOW]
    for k in stale:
        del _recent_hashes[k]
    # Check
    h = hashlib.md5(f"{agent_name}:{sender_id}:{content}".encode(), usedforsecurity=False).hexdigest()
    if h in _recent_hashes:
        return True
    _recent_hashes[h] = now
    return False


# ── CR-169: Telegram Health Check ─────────────────────────────────────────────

async def _tg_health_check(agent_name: str, bot):
    """Periodic getMe() health check. Called from message handler every 5 min."""
    now = time.monotonic()
    last = _last_tg_health.get(agent_name, 0)
    if now - last < _TG_HEALTH_INTERVAL:
        return  # not due yet
    _last_tg_health[agent_name] = now
    try:
        me = await bot.get_me()
        log.info(f"[CR-169] [{agent_name}] Telegram health OK (bot: @{me.username})")
    except Exception as exc:
        log.error(f"[CR-169] [{agent_name}] Telegram health FAILED: {exc}")
        # Write alert to global_settings (same pattern as hybrid_reasoning alerts)
        try:
            conn = psycopg2.connect(
                host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
                user=Config.PG_USER, password=Config.PG_PASSWORD,
                connect_timeout=5,
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                import json as _json
                alert = _json.dumps({
                    "type": "telegram_health_fail",
                    "agent": agent_name,
                    "error": str(exc)[:200],
                    "timestamp": time.time(),
                })
                cur.execute(
                    "INSERT INTO global_settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s",
                    (f"alert.telegram_health.{agent_name}", alert, alert),
                )
            conn.close()
        except Exception as db_exc:
            log.error(f"[CR-169] [{agent_name}] Failed to write health alert to DB: {db_exc}")


# ── DB Helpers ────────────────────────────────────────────────────────────────

def _db_connect():
    return psycopg2.connect(
        host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
        user=Config.PG_USER, password=Config.PG_PASSWORD,
        connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _enqueue_message(agent_name: str, sender_id, content: str,
                     kind: str = "text", file_path: str = None,
                     thread_id: str = "",
                     _queue_on_fail: bool = True) -> int | None:
    """Write to pending_messages and set wake_up_needed.

    CR-157: If the DB insert fails and _queue_on_fail is True, the message
    is pushed onto _retry_queue for at-least-once delivery.
    CR-thread: thread_id for conversation threading.
    """
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_messages (agent_name, sender_id, content, kind, file_path, thread_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (agent_name, sender_id, content, kind, file_path, thread_id or ""),
            )
            msg_id = cur.fetchone()["id"]
            cur.execute("UPDATE agents SET wake_up_needed=TRUE WHERE name=%s", (agent_name,))
        conn.commit()
        conn.close()
        log.info(f"Enqueued #{msg_id} for '{agent_name}' [{kind}]")
        return msg_id
    except Exception as exc:
        log.error(f"Enqueue failed: {exc}")
        if _queue_on_fail:
            _retry_queue.append({
                "agent_name": agent_name, "sender_id": sender_id,
                "content": content, "kind": kind, "file_path": file_path,
                "thread_id": thread_id or "", "retries": 0,
            })
            log.warning(f"CR-157: Message queued for retry (queue size: {len(_retry_queue)})")
        return None


def _flush_retry_queue() -> None:
    """CR-157: Retry failed DB inserts from the retry queue.

    Called at the start of each poll cycle. Successfully inserted messages
    are removed; messages exceeding _RETRY_MAX attempts are dropped with
    an error log.
    """
    if not _retry_queue:
        return

    remaining: list[dict] = []
    for item in _retry_queue:
        item["retries"] += 1
        if item["retries"] > _RETRY_MAX:
            log.error(
                f"CR-157: Dropping message after {_RETRY_MAX} retries — "
                f"agent={item['agent_name']} sender={item['sender_id']} "
                f"content={item['content'][:60]}"
            )
            continue
        result = _enqueue_message(
            item["agent_name"], item["sender_id"], item["content"],
            item["kind"], item["file_path"], thread_id=item.get("thread_id", ""),
            _queue_on_fail=False,
        )
        if result is None:
            remaining.append(item)
            log.warning(
                f"CR-157: Retry {item['retries']}/{_RETRY_MAX} failed — "
                f"agent={item['agent_name']}"
            )
        else:
            log.info(f"CR-157: Retry succeeded — enqueued #{result} for '{item['agent_name']}'")

    _retry_queue.clear()
    _retry_queue.extend(remaining)


# ── Agent Config Loader ───────────────────────────────────────────────────────

def _load_agent_configs() -> list[dict]:
    """Load all agents with their config and env_secrets."""
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT name, config, env_secrets FROM agents")
            rows = cur.fetchall()
        conn.close()
        result = []
        for r in rows:
            cfg = r["config"]
            sec = r["env_secrets"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            if isinstance(sec, str):
                sec = json.loads(sec)
            result.append({
                "name": r["name"],
                "config": cfg or {},
                "secrets": sec or {},
            })
        return result
    except Exception as exc:
        log.error(f"Failed to load agent configs: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Telegram Polling
# ══════════════════════════════════════════════════════════════════════════════

# ── CR-K: Helpdesk customer context switch ─────────────────────────────────
_helpdesk_thread_override = {}   # chat_id → thread_id
_helpdesk_search_results = {}    # chat_id → list of match dicts
_helpdesk_last_query = {}        # chat_id → last search query string


# ── CR-231: Customer file (Kundenakte) helpers ────────────────────────────
def _find_customer_file(query: str) -> Path | None:
    """Search storage/customers/ for a JSON file matching the query.
    Returns Path object or None.
    Checks filename and file content, handles umlaut variants."""
    import re as _re_cf
    cust_dir = Path("storage/customers")
    if not cust_dir.exists():
        return None

    # Generate fuzzy variants
    ql = query.lower().strip()
    variants = {ql}
    for u, asc in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        if u in ql:
            variants.add(ql.replace(u, asc))
    for asc, u in [("ae", "ä"), ("oe", "ö"), ("ue", "ü")]:
        if asc in ql:
            variants.add(ql.replace(asc, u))
    # Also slug variant for filename matching
    slug_variant = _re_cf.sub(r'[^a-z0-9]+', '-', ql).strip('-')
    if slug_variant:
        variants.add(slug_variant)

    for f in cust_dir.glob("*.json"):
        fname_lower = f.stem.lower()
        # Check filename
        for v in variants:
            if v in fname_lower or fname_lower in v:
                return f
        # Check file content (name field)
        try:
            import json as _jcf
            data = _jcf.loads(f.read_text(encoding="utf-8"))
            name_lower = data.get("name", "").lower()
            company_lower = data.get("company", "").lower()
            for v in variants:
                if v in name_lower or v in company_lower:
                    return f
        except Exception:
            pass
    return None


def _search_customer_files(query: str) -> list[dict]:
    """CR-231: Search all customer JSON files for matches. Returns list of match dicts."""
    import re as _re_scf
    cust_dir = Path("storage/customers")
    if not cust_dir.exists():
        return []

    ql = query.lower().strip()
    variants = {ql}
    for u, asc in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        if u in ql:
            variants.add(ql.replace(u, asc))
    for asc, u in [("ae", "ä"), ("oe", "ö"), ("ue", "ü")]:
        if asc in ql:
            variants.add(ql.replace(asc, u))
    slug_variant = _re_scf.sub(r'[^a-z0-9]+', '-', ql).strip('-')
    if slug_variant:
        variants.add(slug_variant)

    results = []
    for f in cust_dir.glob("*.json"):
        try:
            import json as _jscf
            data = _jscf.loads(f.read_text(encoding="utf-8"))
            searchable = (
                f.stem.lower() + " " +
                data.get("name", "").lower() + " " +
                data.get("company", "").lower() + " " +
                data.get("email", "").lower()
            )
            for v in variants:
                if v in searchable:
                    # Use first linked thread_id or create a customer-based one
                    thread_ids = data.get("thread_ids", [])
                    tid = thread_ids[0] if thread_ids else f"customer:{data.get('name', query)}:{f.stem}"
                    results.append({
                        "thread_id": tid,
                        "name": data.get("name", f.stem),
                        "subject": data.get("company", ""),
                        "channel": "Kundenakte",
                        "last_activity": data.get("last_contact", "?"),
                        "_cust_file": f.name,
                    })
                    break
        except Exception:
            pass
    return results


async def _run_telegram_bot(agent_name: str, token: str, shutdown: asyncio.Event):
    """Long-poll one Telegram bot."""
    try:
        from telegram.ext import Application, MessageHandler, CommandHandler, filters as tg_filters
    except ImportError:
        log.error("python-telegram-bot not installed — Telegram disabled")
        return

    app = Application.builder().token(token).build()

    # Get bot's own user_id to filter self-messages
    bot_user_id = None

    async def _on_message(update, context):
        nonlocal bot_user_id
        # CR-157: Flush retry queue before processing new messages
        _flush_retry_queue()
        # CR-169: Periodic Telegram health check (every 5 min)
        await _tg_health_check(agent_name, context.bot)
        if not update.message:
            return

        # Lazy-init bot ID on first message
        if bot_user_id is None:
            try:
                me = await context.bot.get_me()
                bot_user_id = me.id
                log.info(f"[{agent_name}] Bot ID: {bot_user_id}")
            except Exception:
                pass

        # FILTER 1: Ignore messages from the bot itself
        sender_id = update.message.from_user.id if update.message.from_user else 0
        if bot_user_id and sender_id == bot_user_id:
            log.debug(f"[{agent_name}] Ignoring self-message from Bot ID {bot_user_id}")
            return

        chat_id = update.effective_chat.id

        # CR-215c: Telegram chat_id allowlist — reject unauthorized users
        # Config: telegram_allowlist = [123, 456] or empty (= allow all, legacy mode)
        _tg_allowlist = []
        try:
            _c = _db_connect()
            with _c.cursor() as _cur:
                _cur.execute("SELECT config FROM agents WHERE name=%s", (agent_name,))
                _row = _cur.fetchone()
                if _row and _row.get("config"):
                    import json as _json_al
                    _ag_cfg = _json_al.loads(_row["config"]) if isinstance(_row["config"], str) else _row["config"]
                    _tg_allowlist = _ag_cfg.get("telegram_allowlist", [])
            _c.close()
        except Exception:
            pass
        if _tg_allowlist and chat_id not in _tg_allowlist:
            log.warning(f"[{agent_name}] CR-215c: Rejected message from unauthorized chat_id={chat_id}")
            try:
                await update.message.reply_text(
                    "This bot is only available to authorized users. "
                    "Please contact the administrator."
                )
            except Exception:
                pass
            return

        # ── CR-K: Resolve thread_id (helpdesk override or default) ──────
        def _resolve_thread_id():
            override = _helpdesk_thread_override.get(chat_id)
            return override if override else f"tg:{chat_id}"

        # ── CR-K: Handle bare number selection for /k results ──────────
        # Expire stale search results (>5 min)
        if chat_id in _helpdesk_search_results:
            _sr_time = _helpdesk_search_results.get(f"_ts_{chat_id}", 0)
            if time.time() - _sr_time > 300:
                _helpdesk_search_results.pop(chat_id, None)
                _helpdesk_search_results.pop(f"_ts_{chat_id}", None)
                _helpdesk_last_query.pop(chat_id, None)

        if update.message.text and update.message.text.strip().isdigit() and chat_id in _helpdesk_search_results:
            idx = int(update.message.text.strip())
            results = _helpdesk_search_results.get(chat_id, [])
            if idx == 0:
                query = _helpdesk_last_query.get(chat_id, "unknown")
                thread_id = f"customer:{query}:{int(time.time())}"
                _helpdesk_thread_override[chat_id] = thread_id
                del _helpdesk_search_results[chat_id]
                await update.message.reply_text(f"Neuer Kunde \"{query}\" angelegt. Loslegen!")
                return
            if 1 <= idx <= len(results):
                chosen = results[idx - 1]
                _helpdesk_thread_override[chat_id] = chosen["thread_id"]
                del _helpdesk_search_results[chat_id]
                # CR-231: Show customer file (Kundenakte) if available
                # Try multiple search strategies to find the customer file
                _raw_name = chosen["name"]
                _cust_f = (
                    _find_customer_file(_raw_name) or
                    _find_customer_file(_raw_name.split("<")[0].strip()) or
                    _find_customer_file(_raw_name.split("@")[0].strip()) or
                    _find_customer_file(_raw_name.split("@")[0].split(".")[-1].strip())
                )
                if _cust_f:
                    try:
                        import json as _jbn
                        _cd = _jbn.loads(_cust_f.read_text(encoding="utf-8"))
                        # Link current helpdesk thread_id to customer file
                        _hd_tid = chosen["thread_id"]
                        if _hd_tid and _hd_tid not in _cd.get("thread_ids", []):
                            _cd.setdefault("thread_ids", []).append(_hd_tid)
                            _cust_f.write_text(_jbn.dumps(_cd, ensure_ascii=False, indent=2), encoding="utf-8")
                        _parts = [f"KUNDENAKTE: {_cd.get('name', '?')}"]
                        if _cd.get("company"): _parts.append(f"Firma: {_cd['company']}")
                        if _cd.get("email"): _parts.append(f"Email: {_cd['email']}")
                        if _cd.get("phone"): _parts.append(f"Tel: {_cd['phone']}")
                        if _cd.get("address"): _parts.append(f"Adresse: {_cd['address'][:80]}")
                        if _cd.get("products"): _parts.append(f"Produkte: {', '.join(_cd['products'])}")
                        if _cd.get("orders"):
                            _parts.append("Vorgaenge:")
                            for _o in _cd["orders"][-3:]:
                                _parts.append(f"  {_o[:80]}")
                        if _cd.get("notes"):
                            _parts.append("Notizen:")
                            for _n in _cd["notes"][-3:]:
                                _parts.append(f"  {_n[:80]}")
                        _parts.append("\nBereit — einfach lostippen.")
                        await update.message.reply_text("\n".join(_parts))
                        return
                    except Exception:
                        pass
                # Fallback: no customer file
                _reply = (
                    f"Kunde: {chosen['name']}\n"
                    f"Kanal: {chosen.get('channel', '?')} | Letzter Kontakt: {chosen.get('last_activity', '?')}\n"
                    f"Bereit — einfach lostippen."
                )
                await update.message.reply_text(_reply)
                return
            await update.message.reply_text(f"Ungueltige Auswahl. Waehle 0-{len(results)}.")
            return

        file_path = None
        if update.message.text:
            content, kind = update.message.text.strip(), "telegram"
        elif update.message.voice:
            # Download voice file to agent workspace for Whisper transcription
            content, kind = "[Sprachnachricht]", "telegram_voice"
            # CR-176: File size check before download
            voice = update.message.voice
            if voice.file_size and voice.file_size > MAX_FILE_SIZE:
                log.warning(f"[{agent_name}] Voice too large: {voice.file_size} bytes (max {MAX_FILE_SIZE})")
                content = "[Sprachnachricht — Datei zu gross]"
                _enqueue_message(agent_name, chat_id, content, kind, thread_id=_resolve_thread_id())
                return
            try:
                tg_file = await context.bot.get_file(voice.file_id)
                workspace = Path("storage") / "agents" / agent_name / "incoming"
                workspace.mkdir(parents=True, exist_ok=True)
                local_path = workspace / f"voice_{voice.file_id}.ogg"
                await tg_file.download_to_drive(str(local_path))
                file_path = str(local_path)
                log.info(f"[{agent_name}] Voice downloaded: {local_path} ({voice.duration}s)")
            except Exception as exc:
                log.error(f"[{agent_name}] Voice download failed: {exc}")
                content = "[Sprachnachricht — Download fehlgeschlagen]"
        elif update.message.document:
            doc = update.message.document
            content, kind = f"[document:{doc.file_name or doc.file_id}]", "telegram_doc"
            # CR-176: File size check before download
            if doc.file_size and doc.file_size > MAX_FILE_SIZE:
                log.warning(f"[{agent_name}] Document too large: {doc.file_size} bytes (max {MAX_FILE_SIZE})")
                content = f"[document:{doc.file_name or doc.file_id} — Datei zu gross ({doc.file_size} bytes)]"
                _enqueue_message(agent_name, chat_id, content, kind, thread_id=_resolve_thread_id())
                return
            # CR-118 + CR-207: Download to public/ so other agents can access via read_public
            try:
                tg_file = await context.bot.get_file(doc.file_id)
                workspace = Path("storage") / "agents" / agent_name / "public"
                workspace.mkdir(parents=True, exist_ok=True)
                local_name = _sanitize_filename(doc.file_name or "", fallback_prefix=f"doc_{doc.file_id[:8]}")
                local_path = workspace / local_name
                await tg_file.download_to_drive(str(local_path))
                file_path = str(local_path)
                # CR-thread: Also store under thread directory
                _thr_id = _resolve_thread_id()
                thread_dir = Path("storage") / "threads" / _thr_id
                thread_dir.mkdir(parents=True, exist_ok=True)
                thread_copy = thread_dir / local_name
                if not thread_copy.exists():
                    import shutil
                    shutil.copy2(str(local_path), str(thread_copy))
                log.info(f"[{agent_name}] Document downloaded: {local_path} ({doc.file_size} bytes)")
            except Exception as exc:
                log.error(f"[{agent_name}] Document download failed: {exc}")
                content = f"[document:{doc.file_name or doc.file_id} — Download fehlgeschlagen]"
        # CR-207: Photo — download highest resolution, save to workspace
        elif update.message.photo:
            photo = update.message.photo[-1]  # Highest resolution
            caption = update.message.caption or ""
            content = f"[Foto empfangen{': ' + caption if caption else ''}] Nutze analyze_image(filename=\"photo_{photo.file_id[-8:]}.jpg\") um das Bild zu analysieren."
            kind = "telegram_doc"
            if photo.file_size and photo.file_size > MAX_FILE_SIZE:
                content = "[Foto — Datei zu gross]"
                _enqueue_message(agent_name, chat_id, content, kind, thread_id=_resolve_thread_id())
                return
            try:
                tg_file = await context.bot.get_file(photo.file_id)
                # Save to public/ so other agents can access via read_public
                workspace = Path("storage") / "agents" / agent_name / "public"
                workspace.mkdir(parents=True, exist_ok=True)
                local_name = f"photo_{photo.file_id[-8:]}.jpg"
                local_path = workspace / local_name
                await tg_file.download_to_drive(str(local_path))
                file_path = str(local_path)
                # CR-thread: Also store under thread directory
                _thr_id = _resolve_thread_id()
                thread_dir = Path("storage") / "threads" / _thr_id
                thread_dir.mkdir(parents=True, exist_ok=True)
                thread_copy = thread_dir / local_name
                if not thread_copy.exists():
                    import shutil
                    shutil.copy2(str(local_path), str(thread_copy))
                log.info(f"[{agent_name}] Photo downloaded: {local_path} ({photo.width}x{photo.height})")
            except Exception as exc:
                log.error(f"[{agent_name}] Photo download failed: {exc}")
                content = "[Foto — Download fehlgeschlagen]"

        # CR-207: Location — extract GPS coordinates as text
        elif update.message.location:
            loc = update.message.location
            content = (f"[Standort empfangen] GPS: {loc.latitude:.6f}, {loc.longitude:.6f}"
                       f"{' (Genauigkeit: ' + str(int(loc.horizontal_accuracy)) + 'm)' if loc.horizontal_accuracy else ''}")
            kind = "telegram"
            log.info(f"[{agent_name}] Location: {loc.latitude}, {loc.longitude}")

        # CR-207: Venue — location with name/address
        elif update.message.venue:
            venue = update.message.venue
            content = (f"[Standort empfangen] {venue.title}, {venue.address} "
                       f"(GPS: {venue.location.latitude:.6f}, {venue.location.longitude:.6f})")
            kind = "telegram"

        # CR-207: Contact — phone number and name as text
        elif update.message.contact:
            ct = update.message.contact
            name = f"{ct.first_name or ''} {ct.last_name or ''}".strip()
            content = f"[Kontakt empfangen] {name}, Tel: {ct.phone_number}"
            kind = "telegram"
            log.info(f"[{agent_name}] Contact: {name} {ct.phone_number}")

        # CR-207: Audio — download like voice, with metadata
        elif update.message.audio:
            audio = update.message.audio
            title = audio.title or audio.file_name or f"audio_{audio.file_id[-8:]}"
            content = f"[Audio empfangen: {title}]"
            kind = "telegram_voice"
            if audio.file_size and audio.file_size > MAX_FILE_SIZE:
                content = f"[Audio: {title} — Datei zu gross]"
                _enqueue_message(agent_name, chat_id, content, kind, thread_id=_resolve_thread_id())
                return
            try:
                tg_file = await context.bot.get_file(audio.file_id)
                workspace = Path("storage") / "agents" / agent_name / "incoming"
                workspace.mkdir(parents=True, exist_ok=True)
                ext = Path(audio.file_name).suffix if audio.file_name else ".mp3"
                local_path = workspace / f"audio_{audio.file_id[-8:]}{ext}"
                await tg_file.download_to_drive(str(local_path))
                file_path = str(local_path)
                log.info(f"[{agent_name}] Audio downloaded: {local_path} ({audio.duration}s)")
            except Exception as exc:
                log.error(f"[{agent_name}] Audio download failed: {exc}")
                content = f"[Audio: {title} — Download fehlgeschlagen]"

        else:
            return
        if not content:
            return

        # FILTER 2: Deduplicate — same text from same sender within 30s
        if _is_duplicate(agent_name, chat_id, content):
            log.info(f"[{agent_name}] Ignoring duplicate message from {chat_id}")
            return

        # CR-212: Rate limiting per sender
        rate_status = _check_rate_limit(agent_name, chat_id)
        if rate_status == "blocked":
            try:
                await update.message.reply_text(
                    "Zu viele Anfragen. Bitte warten Sie 30 Minuten.")
            except Exception:
                pass
            return
        elif rate_status == "throttled":
            try:
                await update.message.reply_text(
                    "Bitte haben Sie einen Moment Geduld — Ihre Anfrage wird bearbeitet.")
            except Exception:
                pass
            # Still enqueue, but user is warned

        log.info(f"[{agent_name}] TG ← [{kind}] from {chat_id}: {content[:60]}")
        # CR-thread: Telegram thread_id = "tg:{chat_id}"
        _enqueue_message(agent_name, chat_id, content, kind, file_path,
                         thread_id=_resolve_thread_id())

    # /k command — helpdesk customer context switch
    async def _on_customer(update, context):
        chat_id = update.effective_chat.id
        query = update.message.text.replace("/k", "", 1).strip()[:200]  # max 200 chars

        if not query:
            # Show current context
            current = _helpdesk_thread_override.get(chat_id)
            if current:
                # CR-231: Check if current thread links to a customer file
                _cust_reply = None
                try:
                    import json as _json_kctx
                    _cust_dir = Path("storage/customers")
                    if _cust_dir.exists():
                        for _cf in _cust_dir.glob("*.json"):
                            _cd = _json_kctx.loads(_cf.read_text(encoding="utf-8"))
                            if current in _cd.get("thread_ids", []):
                                _parts = [f"KUNDENAKTE: {_cd.get('name', '?')}"]
                                if _cd.get("company"): _parts.append(f"  Firma: {_cd['company']}")
                                if _cd.get("email"): _parts.append(f"  Email: {_cd['email']}")
                                if _cd.get("phone"): _parts.append(f"  Tel: {_cd['phone']}")
                                if _cd.get("address"): _parts.append(f"  Adresse: {_cd['address']}")
                                if _cd.get("products"): _parts.append(f"  Produkte: {', '.join(_cd['products'])}")
                                if _cd.get("orders"):
                                    _parts.append("  Auftraege:")
                                    for _o in _cd["orders"][-5:]:
                                        _parts.append(f"    {_o}")
                                if _cd.get("notes"):
                                    _parts.append("  Notizen:")
                                    for _n in _cd["notes"][-3:]:
                                        _parts.append(f"    {_n}")
                                if _cd.get("last_contact"): _parts.append(f"  Letzter Kontakt: {_cd['last_contact']}")
                                _cust_reply = "\n".join(_parts)
                                break
                except Exception:
                    pass
                if _cust_reply:
                    await update.message.reply_text(_cust_reply)
                else:
                    _display = current.replace("customer:", "Kunde: ").split(":")[0:2]
                    await update.message.reply_text(f"Aktueller Kunde: {':'.join(_display)}")
            else:
                await update.message.reply_text(
                    "Kein Kunde ausgewaehlt.\n"
                    "Tippe /k Name um einen Kunden zu suchen."
                )
            return

        # /k reset or /k - clears the override
        if query in ("reset", "-", "ende", "end", "clear"):
            removed = _helpdesk_thread_override.pop(chat_id, None)
            _helpdesk_search_results.pop(chat_id, None)
            _helpdesk_last_query.pop(chat_id, None)
            if removed:
                await update.message.reply_text("Kunde beendet. Bereit fuer den naechsten.")
            else:
                await update.message.reply_text("Kein Kunde war aktiv.")
            return

        # Check if it's a number (selecting from previous search)
        if query.isdigit():
            idx = int(query)
            results = _helpdesk_search_results.get(chat_id, [])
            if idx == 0:
                prev_query = _helpdesk_last_query.get(chat_id, query)
                thread_id = f"customer:{prev_query}:{int(time.time())}"
                _helpdesk_thread_override[chat_id] = thread_id
                _helpdesk_search_results.pop(chat_id, None)
                await update.message.reply_text(f"Neuer Kunde \"{prev_query}\" angelegt. Loslegen!")
                return
            if 1 <= idx <= len(results):
                chosen = results[idx - 1]
                _helpdesk_thread_override[chat_id] = chosen["thread_id"]
                _helpdesk_search_results.pop(chat_id, None)

                # CR-231: Link thread_id to customer file if one exists
                _cust_file_match = chosen.get("_cust_file")
                if _cust_file_match:
                    try:
                        import json as _jl
                        _cf_path = Path("storage/customers") / _cust_file_match
                        if _cf_path.exists():
                            _cf_data = _jl.loads(_cf_path.read_text(encoding="utf-8"))
                            if chosen["thread_id"] not in _cf_data.get("thread_ids", []):
                                _cf_data.setdefault("thread_ids", []).append(chosen["thread_id"])
                                _cf_path.write_text(_jl.dumps(_cf_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass

                # CR-231: Check for structured customer file first
                _ctx_lines = []
                _showed_cust_file = False
                try:
                    import json as _json_k
                    _cust_dir = Path("storage/customers")
                    if _cust_dir.exists():
                        _cust_match = _find_customer_file(chosen["name"])
                        if _cust_match:
                            _cf = _cust_dir / _cust_match
                            _cd = _json_k.loads(_cf.read_text(encoding="utf-8"))
                            _ctx_lines.append(f"KUNDENAKTE: {_cd.get('name', '?')}")
                            if _cd.get("company"): _ctx_lines.append(f"  Firma: {_cd['company']}")
                            if _cd.get("email"): _ctx_lines.append(f"  Email: {_cd['email']}")
                            if _cd.get("phone"): _ctx_lines.append(f"  Tel: {_cd['phone']}")
                            if _cd.get("address"): _ctx_lines.append(f"  Adresse: {_cd['address']}")
                            if _cd.get("products"): _ctx_lines.append(f"  Produkte: {', '.join(_cd['products'])}")
                            if _cd.get("orders"):
                                _ctx_lines.append("  Auftraege:")
                                for _o in _cd["orders"][-5:]:
                                    _ctx_lines.append(f"    {_o}")
                            if _cd.get("notes"):
                                _ctx_lines.append("  Notizen:")
                                for _n in _cd["notes"][-3:]:
                                    _ctx_lines.append(f"    {_n}")
                            if _cd.get("last_contact"): _ctx_lines.append(f"  Letzter Kontakt: {_cd['last_contact']}")
                            _showed_cust_file = True
                            # Link thread_id
                            if chosen["thread_id"] not in _cd.get("thread_ids", []):
                                _cd.setdefault("thread_ids", []).append(chosen["thread_id"])
                                _cf.write_text(_json_k.dumps(_cd, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

                if not _showed_cust_file:
                    # Fallback: DB-based context (original behavior)
                    # Show customer file (Kundenakte) if available — compact summary
                    _ctx_lines = []
                    _akte_found = False

                    # Search customer files for this customer
                    _name_search = chosen["name"].split("<")[0].strip().split("@")[0].strip()
                    _cust_file = _find_customer_file(_name_search)
                    if _cust_file:
                        _akte_found = True
                        import json as _json_k_ctx
                        _akte = _json_k_ctx.loads(_cust_file.read_text(encoding="utf-8"))
                        _ctx_lines.append(f"KUNDENAKTE: {_akte.get('name', '?')}")
                        if _akte.get("company"):
                            _ctx_lines.append(f"Firma: {_akte['company']}")
                        if _akte.get("email"):
                            _ctx_lines.append(f"Email: {_akte['email']}")
                        if _akte.get("phone"):
                            _ctx_lines.append(f"Tel: {_akte['phone']}")
                        if _akte.get("address"):
                            _ctx_lines.append(f"Adresse: {_akte['address'][:80]}")
                        if _akte.get("products"):
                            _ctx_lines.append(f"Produkte: {', '.join(_akte['products'])}")
                        if _akte.get("orders"):
                            _ctx_lines.append("Vorgaenge:")
                            for _o in _akte["orders"][-3:]:
                                _ctx_lines.append(f"  {_o[:80]}")
                        if _akte.get("notes"):
                            _ctx_lines.append("Notizen:")
                            for _n in _akte["notes"][-3:]:
                                _ctx_lines.append(f"  {_n[:80]}")

                    if not _akte_found:
                        # Fallback: show last messages from DB
                        _ctx_lines.append(f"Kunde: {chosen['name']}")
                        _ctx_lines.append(f"Kanal: {chosen.get('channel', '?')} | Letzter Kontakt: {chosen.get('last_activity', '?')}")
                        try:
                            _ctx_conn = _db_connect()
                            with _ctx_conn.cursor() as _ctx_cur:
                                _ctx_cur.execute(
                                    "SELECT role, content, created_at FROM aimos_chat_histories "
                                    "WHERE agent_name=%s AND thread_id=%s AND role IN ('user','assistant') "
                                    "ORDER BY id DESC LIMIT 4",
                                    (agent_name, chosen["thread_id"]),
                                )
                                for h in reversed(_ctx_cur.fetchall()):
                                    _role = "Kunde" if h["role"] == "user" else "Agent"
                                    _text = (h.get("content") or "")[:100].replace("\n", " ")
                                    _ctx_lines.append(f"  {_role}: {_text}")
                            _ctx_conn.close()
                        except Exception:
                            pass

                _ctx_lines.append("\nBereit — einfach lostippen.")
                await update.message.reply_text("\n".join(_ctx_lines))
                return
            await update.message.reply_text(f"Ungueltige Auswahl. Waehle 0-{len(results)}.")
            return

        # Search for customer across all sources
        # Fuzzy: normalize umlauts so "Mueller" finds "Müller" and vice versa
        import re as _re_k
        _umlaut_map = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                        "ae": "ä", "oe": "ö", "ue": "ü"}
        def _fuzzy_variants(q):
            """Generate search variants: Mueller → [mueller, müller]"""
            variants = {q.lower()}
            ql = q.lower()
            # Forward: ü→ue
            for u, ascii in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
                if u in ql:
                    variants.add(ql.replace(u, ascii))
            # Reverse: ue→ü
            for ascii, u in [("ae", "ä"), ("oe", "ö"), ("ue", "ü")]:
                if ascii in ql:
                    variants.add(ql.replace(ascii, u))
            return list(variants)

        search_variants = _fuzzy_variants(query)
        matches = []
        try:
            conn = _db_connect()
            with conn.cursor() as cur:
                # Build OR conditions for all variants
                _conditions = " OR ".join(
                    ["(LOWER(content) LIKE LOWER(%s) OR LOWER(thread_id) LIKE LOWER(%s))"] * len(search_variants)
                )
                _params = []
                for v in search_variants:
                    _params.extend([f"%{v}%", f"%{v}%"])
                cur.execute(
                    f"SELECT DISTINCT thread_id, content, created_at FROM pending_messages "
                    f"WHERE kind='email' AND ({_conditions}) "
                    f"ORDER BY created_at DESC LIMIT 20",
                    _params,
                )
                for row in cur.fetchall():
                    from_match = _re_k.search(r'Von:\s*(.+?)[\n\r]', row.get("content", ""))
                    subj_match = _re_k.search(r'Betreff:\s*(.+?)[\n\r]', row.get("content", ""))
                    name = from_match.group(1).strip() if from_match else "?"
                    subject = subj_match.group(1).strip() if subj_match else ""
                    tid = row["thread_id"]
                    if not any(m["thread_id"] == tid for m in matches):
                        matches.append({
                            "thread_id": tid,
                            "name": name,
                            "subject": subject,
                            "channel": "Email",
                            "last_activity": row["created_at"].strftime("%d.%m. %H:%M") if row.get("created_at") else "?",
                        })

                # Search chat histories (also with fuzzy variants)
                _hist_conditions = " OR ".join(
                    ["LOWER(content) LIKE LOWER(%s)"] * len(search_variants)
                )
                _hist_params = [agent_name] + [f"%{v}%" for v in search_variants]
                cur.execute(
                    f"SELECT DISTINCT thread_id, content, created_at FROM aimos_chat_histories "
                    f"WHERE agent_name=%s AND ({_hist_conditions}) AND thread_id != '' "
                    f"ORDER BY created_at DESC LIMIT 20",
                    _hist_params,
                )
                for row in cur.fetchall():
                    tid = row["thread_id"]
                    if not any(m["thread_id"] == tid for m in matches):
                        channel = "Telegram" if tid.startswith("tg:") else "Email" if tid.startswith("email:") else "Helpdesk" if tid.startswith("customer:") else "Other"
                        # Try to extract a better name from thread_id
                        _display_name = query
                        if tid.startswith("customer:"):
                            _display_name = tid.split(":")[1] if ":" in tid else query
                        matches.append({
                            "thread_id": tid,
                            "name": _display_name,
                            "subject": "",
                            "channel": channel,
                            "last_activity": row["created_at"].strftime("%d.%m. %H:%M") if row.get("created_at") else "?",
                        })
            conn.close()
        except Exception as exc:
            log.error(f"[/k] DB search failed: {exc}")

        # Also search agent memory (SQLite)
        try:
            import sqlite3 as _sqlite3_k
            mem_path = Path("storage") / "agents" / agent_name / "memory.db"
            if mem_path.exists():
                mdb = _sqlite3_k.connect(str(mem_path), timeout=3)
                mdb.row_factory = _sqlite3_k.Row
                rows = mdb.execute(
                    "SELECT key, value FROM memories WHERE LOWER(key) LIKE LOWER(?) OR LOWER(value) LIKE LOWER(?) LIMIT 10",
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
                for row in rows:
                    # Memory entries enhance existing matches but don't create new threads
                    pass
                mdb.close()
        except Exception:
            pass

        # CR-231: Search customer files (Kundenakten)
        try:
            _cust_matches = _search_customer_files(query)
            for cm in _cust_matches:
                # Avoid duplicates (same thread_id already in matches)
                if not any(m["thread_id"] == cm["thread_id"] for m in matches):
                    matches.insert(0, cm)  # Kundenakte results first
        except Exception as exc:
            log.error(f"[/k] Customer file search failed: {exc}")

        # Dedup: group by customer name/email — keep only newest thread per customer
        _seen_customers = {}
        _deduped = []
        for m in matches:
            _cust_key = m["name"].split("<")[0].strip().lower()
            if _cust_key not in _seen_customers:
                _seen_customers[_cust_key] = m
                _deduped.append(m)
            # else: skip older thread for same customer
        matches = _deduped

        if not matches:
            await update.message.reply_text(
                f"Kein Vorgang zu \"{query}\" gefunden.\n"
                f"Tippe 0 um einen neuen Kunden \"{query}\" anzulegen."
            )
            _helpdesk_search_results[chat_id] = []
            _helpdesk_last_query[chat_id] = query
            return

        # Store results for number selection (with TTL timestamp)
        _helpdesk_search_results[chat_id] = matches
        _helpdesk_search_results[f"_ts_{chat_id}"] = time.time()
        _helpdesk_last_query[chat_id] = query

        # Format response
        lines = [f"{len(matches)} Vorgang/Vorgaenge zu \"{query}\":\n"]
        for i, m in enumerate(matches[:10], 1):
            lines.append(
                f"[{i}] {m['name']}\n"
                f"    {m.get('channel', '?')}: {m.get('subject', '')[:40]}\n"
                f"    Letzter Kontakt: {m.get('last_activity', '?')}"
            )
        if len(matches) > 10:
            lines.append(f"\n... und {len(matches) - 10} weitere")
        lines.append(f"\n[0] Neuer Kunde \"{query}\"")
        lines.append("\nNummer tippen zum Auswaehlen.")

        await update.message.reply_text("\n".join(lines))

    # /reset command — clears conversation history for this user
    async def _on_reset(update, context):
        chat_id = update.effective_chat.id
        try:
            c = _db_connect()
            with c.cursor() as cur:
                # Clear ALL agent histories (support + innendienst + any others)
                cur.execute("SELECT name FROM agents WHERE COALESCE((config->>'active')::boolean, true) = true")
                all_agents = [r["name"] for r in cur.fetchall()]
                total_history = 0
                for a in all_agents:
                    cur.execute("DELETE FROM aimos_chat_histories WHERE agent_name=%s", (a,))
                    total_history += cur.rowcount
                # Clear processed pending messages for all agents
                cur.execute("DELETE FROM pending_messages WHERE processed=TRUE")
            c.commit()
            c.close()
            # Clear memory for all agents
            import sqlite3
            total_mem = 0
            for a in all_agents:
                mem_path = Path("storage") / "agents" / a / "memory.db"
                if mem_path.exists():
                    mdb = sqlite3.connect(str(mem_path), timeout=3)
                    total_mem += mdb.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                    mdb.execute("DELETE FROM memories")
                    mdb.commit()
                    mdb.close()
            # Clear helpdesk overrides
            _helpdesk_thread_override.clear()
            _helpdesk_search_results.clear()
            _helpdesk_last_query.clear()
            log.info(f"[{agent_name}] /reset from {chat_id}: {total_history} history + {total_mem} memories cleared for {len(all_agents)} agents")
            await update.message.reply_text(
                f"System zurueckgesetzt ({len(all_agents)} Agenten). Bereit fuer eine neue Demo."
            )
        except Exception as exc:
            log.error(f"[{agent_name}] /reset failed: {exc}")
            await update.message.reply_text("Reset fehlgeschlagen. Bitte erneut versuchen.")

    # /start command — welcome message
    async def _on_start(update, context):
        await update.message.reply_text(
            "AIMOS Helpdesk bereit.\n\n"
            "Befehle:\n"
            "/k Name — Kunden-Kontext wechseln\n"
            "/k — Aktuellen Kontext anzeigen\n"
            "/k ende — Kunden-Kontext beenden\n"
            "/h — Diese Hilfe anzeigen\n\n"
            "Einfach lostippen — Sprache, Fotos und Dokumente werden unterstuetzt."
        )

    app.add_handler(CommandHandler("k", _on_customer))
    app.add_handler(CommandHandler("h", _on_start))
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _on_message))
    app.add_handler(MessageHandler(tg_filters.VOICE, _on_message))
    app.add_handler(MessageHandler(tg_filters.Document.ALL, _on_message))
    app.add_handler(MessageHandler(tg_filters.PHOTO, _on_message))
    app.add_handler(MessageHandler(tg_filters.LOCATION, _on_message))
    app.add_handler(MessageHandler(tg_filters.CONTACT, _on_message))
    app.add_handler(MessageHandler(tg_filters.AUDIO, _on_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info(f"[{agent_name}] Telegram polling active")

    await shutdown.wait()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    log.info(f"[{agent_name}] Telegram bot stopped")


# ══════════════════════════════════════════════════════════════════════════════
#  IMAP Email Polling
# ══════════════════════════════════════════════════════════════════════════════

def _decode_header(raw: str) -> str:
    """Decode RFC2047 encoded email header."""
    if not raw:
        return ""
    parts = email_lib.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _check_imap(agent_name: str, secrets: dict) -> list[dict]:
    """Check IMAP for unread emails. Returns list of parsed messages."""
    addr = secrets.get("EMAIL_ADDRESS", "")
    passwd = secrets.get("EMAIL_PASSWORD", "")
    imap_host = secrets.get("EMAIL_IMAP_HOST", "")
    imap_port = int(secrets.get("EMAIL_IMAP_PORT", "993"))

    if not addr or not passwd:
        return []

    tls_ctx = ssl.create_default_context()
    tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    messages = []

    try:
        conn = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=tls_ctx)
        conn.login(addr, passwd)
        conn.select("INBOX")

        _, msg_ids = conn.search(None, "UNSEEN")
        id_list = msg_ids[0].split()

        if not id_list:
            conn.logout()
            return []

        log.info(f"[{agent_name}] IMAP: {len(id_list)} unread email(s)")

        # Save attachments to agent workspace
        workspace = Path("storage") / "agents" / agent_name
        workspace.mkdir(parents=True, exist_ok=True)

        for msg_id in id_list[:10]:  # max 10 per poll
            _, msg_data = conn.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg_obj = email_lib.message_from_bytes(raw)

            subject = _decode_header(msg_obj.get("Subject", ""))
            from_addr = _decode_header(msg_obj.get("From", ""))
            date_str = msg_obj.get("Date", "")
            # CR-thread: Extract threading headers
            in_reply_to = msg_obj.get("In-Reply-To", "").strip()
            references = msg_obj.get("References", "").strip()
            # L6: Extract Message-ID for thread mapping
            message_id = msg_obj.get("Message-ID", "").strip()

            # Save raw .eml file for audit/compliance/debugging
            try:
                _eml_dir = workspace / "incoming" / "raw_emails"
                _eml_dir.mkdir(parents=True, exist_ok=True)
                _eml_ts = time.strftime("%Y%m%d_%H%M%S")
                _eml_safe_subj = _sanitize_filename(subject[:40] or "no_subject", fallback_prefix="email")
                _eml_path = _eml_dir / f"{_eml_ts}_{_eml_safe_subj}.eml"
                _eml_path.write_bytes(raw)
                log.info(f"[{agent_name}] Raw email saved: {_eml_path.name}")
            except Exception as _eml_exc:
                log.debug(f"[{agent_name}] Failed to save raw email: {_eml_exc}")

            body = ""
            attachments = []

            if msg_obj.is_multipart():
                for part in msg_obj.walk():
                    ct = part.get_content_type()
                    disp = str(part.get("Content-Disposition", ""))

                    if "attachment" in disp:
                        raw_filename = part.get_filename()
                        if raw_filename:
                            raw_filename = _decode_header(raw_filename)
                            filename = _sanitize_filename(raw_filename, fallback_prefix="attachment")
                            dest = workspace / filename
                            # Deduplicate
                            if dest.exists():
                                stem, suffix = dest.stem, dest.suffix
                                c = 1
                                while dest.exists():
                                    dest = workspace / f"{stem}_{c}{suffix}"
                                    c += 1
                            payload = part.get_payload(decode=True)
                            if payload:
                                dest.write_bytes(payload)
                                attachments.append(str(dest))
                                log.info(f"[{agent_name}] Attachment saved: {dest.name}"
                                         f"{' (sanitized from: ' + raw_filename + ')' if raw_filename != filename else ''}")
                    elif ct == "text/plain" and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body = payload.decode(charset, errors="replace")
            else:
                payload = msg_obj.get_payload(decode=True)
                if payload:
                    charset = msg_obj.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    # S9b: Single-part HTML — strip tags
                    if msg_obj.get_content_type() == "text/html" and body:
                        import re as _re_sp
                        body = _re_sp.sub(r'<script[^>]*>.*?</script>', '', body, flags=_re_sp.DOTALL)
                        body = _re_sp.sub(r'<style[^>]*>.*?</style>', '', body, flags=_re_sp.DOTALL)
                        body = _re_sp.sub(r'<[^>]+>', ' ', body)
                        body = _re_sp.sub(r'\s+', ' ', body).strip()

            # S9: Fallback for HTML-only emails (no text/plain part)
            if not body:
                for part in msg_obj.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            html_body = payload.decode(charset, errors="replace")
                            import re as _re_html
                            # Remove scripts and style blocks
                            html_body = _re_html.sub(r'<script[^>]*>.*?</script>', '', html_body, flags=_re_html.DOTALL)
                            html_body = _re_html.sub(r'<style[^>]*>.*?</style>', '', html_body, flags=_re_html.DOTALL)
                            # Remove HTML tags
                            html_body = _re_html.sub(r'<[^>]+>', ' ', html_body)
                            # Collapse whitespace
                            body = _re_html.sub(r'\s+', ' ', html_body).strip()
                            break

            messages.append({
                "from": from_addr,
                "subject": subject,
                "date": date_str,
                "body": body[:4000],
                "attachments": attachments,
                "in_reply_to": in_reply_to,
                "references": references,
                "message_id": message_id,
            })

        conn.logout()
    except Exception as exc:
        log.error(f"[{agent_name}] IMAP error: {exc}")

    return messages


async def _run_imap_poller(agents_with_email: list[dict], shutdown: asyncio.Event):
    """Poll IMAP for all email-enabled agents every 60s."""
    if not agents_with_email:
        return

    names = [a["name"] for a in agents_with_email]
    log.info(f"IMAP poller active for: {names} (interval={_MAIL_POLL_INTERVAL}s)")

    while not shutdown.is_set():
        # CR-157: Flush retry queue before processing new messages
        _flush_retry_queue()

        for agent in agents_with_email:
            if shutdown.is_set():
                break
            name = agent["name"]
            secrets = agent["secrets"]
            try:
                mails = await asyncio.to_thread(_check_imap, name, secrets)
                for mail in mails:
                    # S10: Skip auto-replies and bounce notifications to prevent loops
                    _auto_reply_indicators = [
                        "auto-reply", "autoreply", "auto-submitted", "out of office",
                        "abwesenheit", "automatische antwort", "delivery status",
                        "undeliverable", "mailer-daemon", "postmaster",
                        "noreply", "no-reply", "donotreply",
                    ]
                    _subj_lower = mail.get("subject", "").lower()
                    _from_lower = mail.get("from", "").lower()
                    _skip = any(ind in _subj_lower or ind in _from_lower for ind in _auto_reply_indicators)
                    if _skip:
                        log.info(f"[S10] Skipping auto-reply/bounce: From={mail['from'][:50]} Subject={mail['subject'][:50]}")
                        continue

                    # CR-215b: Sanitize email body before LLM processing
                    # Strip HTML tags, comments, invisible Unicode, and potential injection markers
                    raw_body = mail.get("body", "")
                    import re as _re_mail
                    # Remove HTML comments (can hide prompt injections)
                    raw_body = _re_mail.sub(r'<!--.*?-->', '', raw_body, flags=_re_mail.DOTALL)
                    # Remove HTML tags
                    raw_body = _re_mail.sub(r'<[^>]+>', ' ', raw_body)
                    # Decode HTML entities (&nbsp; &amp; &lt; etc.)
                    import html as _html_mod
                    raw_body = _html_mod.unescape(raw_body)
                    # Remove zero-width and invisible Unicode characters
                    raw_body = _re_mail.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff\u00ad]', '', raw_body)
                    # Remove Base64-like blocks (potential encoded injections)
                    raw_body = _re_mail.sub(r'[A-Za-z0-9+/]{50,}={0,2}', '[base64-block-removed]', raw_body)
                    # Collapse whitespace
                    raw_body = _re_mail.sub(r'\s+', ' ', raw_body).strip()
                    mail["body"] = raw_body

                    # S11: Also sanitize subject (potential injection vector)
                    raw_subject = mail.get("subject", "")
                    raw_subject = _re_mail.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff\u00ad]', '', raw_subject)
                    mail["subject"] = raw_subject

                    # Build content JSON for the agent
                    att_info = ""
                    if mail["attachments"]:
                        fnames = [Path(a).name for a in mail["attachments"]]
                        att_info = f"\nAnhaenge im Workspace: {', '.join(fnames)}"
                    # Extract bare email for clarity
                    import re as _re_from
                    _from_bare = _re_from.search(r'[\w.+-]+@[\w.-]+', mail['from'])
                    _customer_email = _from_bare.group(0) if _from_bare else mail['from']
                    content = (
                        f"[E-Mail empfangen]\n"
                        f"Von: {mail['from']}\n"
                        f"Kunden-Email: {_customer_email}\n"
                        f"Betreff: {mail['subject']}\n"
                        f"Datum: {mail['date']}\n"
                        f"Text: {raw_body[:2000]}"
                        f"{att_info}"
                    )
                    file_path = mail["attachments"][0] if mail["attachments"] else None

                    # CR-thread + L6: Determine email thread_id from In-Reply-To/References
                    # or generate from sender+subject hash.
                    # L6 fix: When a reply arrives, look up the original's thread_id
                    # from pending_messages by matching the In-Reply-To Message-ID
                    # stored in content. This prevents thread_id divergence.
                    email_thread_id = ""

                    # Helper: generate subject-based thread_id (used as fallback and for originals)
                    import re as _re
                    normalized_subj = _re.sub(
                        r'^(?:Re|Fw|Fwd|AW|WG|Antwort)\s*:\s*', '',
                        mail.get("subject", ""), flags=_re.IGNORECASE
                    ).strip().lower()
                    _from_addr = mail.get("from", "").strip()
                    subject_hash = hashlib.md5(normalized_subj.encode(), usedforsecurity=False).hexdigest()[:8]
                    _subject_thread_id = f"email:{_from_addr}:{subject_hash}"

                    if mail.get("in_reply_to") or mail.get("references"):
                        # L6: Look up existing thread_id from DB by matching Message-ID
                        _lookup_ids = []
                        if mail.get("in_reply_to"):
                            _lookup_ids.append(mail["in_reply_to"].strip())
                        if mail.get("references"):
                            _lookup_ids.extend(r.strip() for r in mail["references"].split() if r.strip())

                        _found_thread = ""
                        _lookup_conn = None
                        try:
                            _lookup_conn = _db_connect()
                            with _lookup_conn.cursor() as _cur:
                                for _mid in _lookup_ids:
                                    if not _mid:
                                        continue
                                    # Escape LIKE wildcards in Message-ID (F12 fix)
                                    _mid_safe = _mid.replace("%", r"\%").replace("_", r"\_")
                                    _cur.execute(
                                        "SELECT thread_id FROM pending_messages "
                                        "WHERE kind='email' AND content LIKE %s "
                                        "AND thread_id IS NOT NULL AND thread_id != '' "
                                        "ORDER BY id DESC LIMIT 1",
                                        (f"%Message-ID: {_mid_safe}%",),
                                    )
                                    _row = _cur.fetchone()
                                    if _row and _row["thread_id"]:
                                        _found_thread = _row["thread_id"]
                                        break
                                # Fallback: also try subject-based thread_id lookup
                                if not _found_thread:
                                    _cur.execute(
                                        "SELECT thread_id FROM pending_messages "
                                        "WHERE kind='email' AND thread_id LIKE %s "
                                        "AND thread_id IS NOT NULL AND thread_id != '' "
                                        "ORDER BY id DESC LIMIT 1",
                                        (f"email:%:{subject_hash}",),
                                    )
                                    _row = _cur.fetchone()
                                    if _row and _row["thread_id"]:
                                        _found_thread = _row["thread_id"]
                        except Exception as _exc:
                            log.debug(f"[L6] Thread lookup failed (non-critical): {_exc}")
                        finally:
                            if _lookup_conn:
                                try:
                                    _lookup_conn.close()
                                except Exception:
                                    pass

                        if _found_thread:
                            email_thread_id = _found_thread
                            log.info(f"[L6] Reply matched to existing thread: {email_thread_id}")
                        else:
                            # Fallback: use subject-based thread_id (same as original would get)
                            email_thread_id = _subject_thread_id
                            log.info(f"[L6] Reply thread not found in DB — using subject hash: {email_thread_id}")
                    else:
                        # No threading headers — generate from sender + normalized subject
                        email_thread_id = _subject_thread_id

                    # L6: Include Message-ID in content so replies can find this thread
                    _msg_id = mail.get("message_id", "")
                    if _msg_id:
                        content += f"\nMessage-ID: {_msg_id}"

                    # Copy attachments to thread directory
                    if mail["attachments"] and email_thread_id:
                        thread_dir = Path("storage") / "threads" / email_thread_id
                        thread_dir.mkdir(parents=True, exist_ok=True)
                        import shutil
                        for att_path in mail["attachments"]:
                            att_dest = thread_dir / Path(att_path).name
                            if not att_dest.exists():
                                try:
                                    shutil.copy2(att_path, str(att_dest))
                                except Exception:
                                    pass

                    _enqueue_message(name, 0, content, "email", file_path,
                                     thread_id=email_thread_id)
            except Exception as exc:
                log.error(f"[{name}] IMAP poll error: {exc}")

        # Wait for next poll or shutdown
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_MAIL_POLL_INTERVAL)
            break  # shutdown was set
        except asyncio.TimeoutError:
            pass  # normal timeout — poll again

    log.info("IMAP poller stopped")


# ══════════════════════════════════════════════════════════════════════════════
#  Outbound Sender — delivers agent replies from DB to Telegram
# ══════════════════════════════════════════════════════════════════════════════

async def _run_outbound_sender(agents: list[dict], shutdown: asyncio.Event):
    """Poll DB for outbound_telegram messages and send them via bot API."""
    global _tg_backoff  # CR-165: must declare at function scope
    # Build token map: agent_name → token (refreshed periodically)
    tokens = {}
    _last_token_refresh = 0

    def _refresh_tokens():
        nonlocal _last_token_refresh
        now = __import__("time").monotonic()
        if now - _last_token_refresh < 30:
            return
        _last_token_refresh = now
        try:
            fresh = _load_agent_configs()
            for a in fresh:
                t = a["secrets"].get("TELEGRAM_BOT_TOKEN", "")
                if t and ":" in t and a["name"] not in tokens:
                    tokens[a["name"]] = t
                    log.info(f"[Outbound] New token loaded for '{a['name']}'")
        except Exception:
            pass

    for a in agents:
        token = a["secrets"].get("TELEGRAM_BOT_TOKEN", "")
        if token and ":" in token:
            tokens[a["name"]] = token

    if not tokens:
        log.info("Outbound sender: no initial tokens — will check for new agents periodically")

    log.info(f"Outbound sender active for: {list(tokens.keys())}")

    import httpx

    while not shutdown.is_set():
        _refresh_tokens()  # pick up tokens for newly created agents
        try:
            conn = _db_connect()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, agent_name, sender_id, content, kind, file_path "
                    "FROM pending_messages "
                    "WHERE kind LIKE 'outbound_telegram%%' AND processed=FALSE "
                    "AND created_at > NOW() - INTERVAL '5 minutes' "
                    "ORDER BY id ASC LIMIT 10"
                )
                rows = cur.fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    cur.execute(
                        "UPDATE pending_messages SET processed=TRUE WHERE id = ANY(%s)",
                        (ids,),
                    )
            conn.commit()
            conn.close()

            for row in rows:
                agent = row["agent_name"]
                chat_id = row["sender_id"]
                text = row["content"]
                kind = row["kind"]
                file_path = row.get("file_path", "")
                token = tokens.get(agent)
                if not token or not chat_id:
                    continue
                # Skip demo helpdesk messages — delivered via dashboard, not Telegram
                if int(chat_id) == 9999999:
                    continue

                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        if kind == "outbound_telegram_doc" and file_path:
                            # Send document via Telegram Bot API
                            from pathlib import Path as _P
                            fp = _P(file_path)
                            if fp.is_file():
                                url = f"https://api.telegram.org/bot{token}/sendDocument"
                                with open(fp, "rb") as f:
                                    resp = await client.post(url, data={
                                        "chat_id": int(chat_id),
                                        "caption": text[:1024] if text else "",
                                    }, files={"document": (fp.name, f)})
                                if resp.status_code == 200:
                                    _tg_backoff = 1  # CR-165: reset on success
                                    log.info(f"[Relay] Document sent: [{agent}] → {chat_id} ({fp.name})")
                                elif resp.status_code == 429:
                                    _tg_backoff = min(_tg_backoff * 2, 300)
                                    log.warning(f"Telegram rate limit hit (doc). Backing off {_tg_backoff}s")
                                    await asyncio.sleep(_tg_backoff)
                                else:
                                    _tg_backoff = 1
                                    log.error(f"Outbound doc → [{agent}] HTTP {resp.status_code}: {resp.text[:100]}")
                            else:
                                log.error(f"Outbound doc → [{agent}] file not found: {file_path}")
                        else:
                            # Standard text message
                            url = f"https://api.telegram.org/bot{token}/sendMessage"
                            resp = await client.post(url, json={
                                "chat_id": int(chat_id),
                                "text": text[:4096],
                            })
                            if resp.status_code == 200:
                                _tg_backoff = 1  # CR-165: reset on success
                                log.info(f"[Relay] Text sent: [{agent}] → {chat_id} ({len(text)} chars)")
                            elif resp.status_code == 429:
                                _tg_backoff = min(_tg_backoff * 2, 300)
                                log.warning(f"Telegram rate limit hit (text). Backing off {_tg_backoff}s")
                                await asyncio.sleep(_tg_backoff)
                            else:
                                _tg_backoff = 1
                                log.error(f"Outbound → [{agent}] HTTP {resp.status_code}: {resp.text[:100]}")
                except Exception as exc:
                    # CR-165: Exponential backoff on Telegram 429 rate limit
                    if "429" in str(exc) or "RetryAfter" in str(exc):
                        _tg_backoff = min(_tg_backoff * 2, 300)  # max 5 min
                        log.warning(f"Telegram rate limit hit. Backing off {_tg_backoff}s")
                        await asyncio.sleep(_tg_backoff)
                    else:
                        _tg_backoff = 1  # reset on non-429 errors
                    log.error(f"Outbound → [{agent}] send failed: {exc}")

        except Exception as exc:
            log.debug(f"Outbound sender error: {exc}")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=2.0)
            break
        except asyncio.TimeoutError:
            pass

    log.info("Outbound sender stopped")


# ══════════════════════════════════════════════════════════════════════════════
#  Outbound Email Sender — delivers agent replies from DB via SMTP
# ══════════════════════════════════════════════════════════════════════════════

async def _run_outbound_email_sender(agents: list[dict], shutdown: asyncio.Event):
    """Poll DB for outbound_email messages and send them via SMTP."""
    # Build email credentials map: agent_name → {address, password, smtp_host, smtp_port}
    email_creds = {}
    _last_cred_refresh = 0

    def _refresh_creds():
        nonlocal _last_cred_refresh
        now = __import__("time").monotonic()
        if now - _last_cred_refresh < 60:
            return
        _last_cred_refresh = now
        try:
            fresh = _load_agent_configs()
            for a in fresh:
                addr = a["secrets"].get("EMAIL_ADDRESS", "")
                pwd = a["secrets"].get("EMAIL_PASSWORD", "")
                if addr and pwd:
                    email_creds[a["name"]] = {
                        "address": addr,
                        "password": pwd,
                        "smtp_host": a["secrets"].get("EMAIL_SMTP_HOST", a["secrets"].get("SMTP_HOST", "")),
                        "smtp_port": int(a["secrets"].get("EMAIL_SMTP_PORT", a["secrets"].get("SMTP_PORT", "587"))),
                    }
        except Exception:
            pass

    # Initial load
    for a in agents:
        addr = a["secrets"].get("EMAIL_ADDRESS", "")
        pwd = a["secrets"].get("EMAIL_PASSWORD", "")
        if addr and pwd:
            email_creds[a["name"]] = {
                "address": addr,
                "password": pwd,
                "smtp_host": a["secrets"].get("SMTP_HOST", ""),
                "smtp_port": int(a["secrets"].get("SMTP_PORT", "587")),
            }

    if not email_creds:
        log.info("Outbound email sender: no email credentials found — will check periodically")

    log.info(f"Outbound email sender active for: {list(email_creds.keys())}")

    import json as _json_outbound
    import smtplib
    from email.mime.text import MIMEText

    while not shutdown.is_set():
        _refresh_creds()
        try:
            conn = _db_connect()
            rows = []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, agent_name, content, thread_id "
                    "FROM pending_messages "
                    "WHERE kind = 'outbound_email' AND processed = FALSE "
                    "AND created_at > NOW() - INTERVAL '10 minutes' "
                    "ORDER BY id ASC LIMIT 10"
                )
                rows = cur.fetchall()
            conn.close()

            for row in rows:
                agent_name = row["agent_name"]
                creds = email_creds.get(agent_name)
                if not creds:
                    log.warning(f"[Outbound Email] No credentials for agent '{agent_name}' — will retry")
                    continue  # Don't mark processed — retry on next cycle
                try:
                    data = _json_outbound.loads(row["content"])
                    to_addr = data["to"]
                    subject = data["subject"]
                    body = data["body"]

                    mime_msg = MIMEText(body, "plain", "utf-8")
                    mime_msg["From"] = creds["address"]
                    mime_msg["To"] = to_addr
                    mime_msg["Subject"] = subject

                    def _send_smtp():
                        with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=30) as srv:
                            srv.ehlo()
                            srv.starttls()
                            srv.ehlo()
                            srv.login(creds["address"], creds["password"])
                            srv.send_message(mime_msg)

                    await asyncio.to_thread(_send_smtp)
                    log.info(f"[Outbound Email] Sent: [{agent_name}] → {to_addr} ({subject[:50]})")
                    # Mark as processed only after successful send
                    try:
                        _c = _db_connect()
                        _c.cursor().execute(
                            "UPDATE pending_messages SET processed=TRUE WHERE id=%s",
                            (row["id"],),
                        )
                        _c.commit()
                        _c.close()
                    except Exception:
                        pass
                except Exception as exc:
                    log.error(f"[Outbound Email] Send failed [{agent_name}] → {exc}")
                    # Mark as processed to avoid infinite retry on permanent errors
                    # (e.g. malformed address, auth failure)
                    try:
                        _c = _db_connect()
                        _c.cursor().execute(
                            "UPDATE pending_messages SET processed=TRUE WHERE id=%s",
                            (row["id"],),
                        )
                        _c.commit()
                        _c.close()
                    except Exception:
                        pass

        except Exception as exc:
            log.debug(f"Outbound email sender error: {exc}")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass

    log.info("Outbound email sender stopped")


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

async def _watch_new_agents(known_agents: set[str], shutdown: asyncio.Event, tasks: list):
    """Poll DB every 30s for newly created agents. Start their Telegram bots dynamically.

    Also reloads outbound sender tokens so new agents' replies get delivered.
    """
    _WATCH_INTERVAL = 30

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_WATCH_INTERVAL)
            break  # shutdown
        except asyncio.TimeoutError:
            pass  # normal timeout — check for new agents

        try:
            current = _load_agent_configs()
            for a in current:
                name = a["name"]
                if name in known_agents:
                    continue
                skills = a["config"].get("skills", a["config"].get("modules", []))
                token = a["secrets"].get("TELEGRAM_BOT_TOKEN", "")
                if "telegram" in skills and token and ":" in token:
                    log.info(f"[AgentWatcher] New agent detected: '{name}' — starting Telegram bot")
                    tasks.append(asyncio.create_task(
                        _run_telegram_bot(name, token, shutdown)
                    ))
                    known_agents.add(name)
                else:
                    known_agents.add(name)  # track even without telegram
        except Exception as exc:
            log.debug(f"AgentWatcher error: {exc}")

    log.info("Agent watcher stopped")


async def main():
    log.info("=" * 50)
    log.info("  AIMOS Shared Listener v4.2.0")
    log.info("  Telegram + IMAP Unified Relay + Agent Watcher")
    log.info("=" * 50)

    agents = _load_agent_configs()
    if not agents:
        log.error("No agents in DB")
        return

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    tasks = []
    known_agents: set[str] = set()

    # Telegram bots
    for a in agents:
        known_agents.add(a["name"])
        token = a["secrets"].get("TELEGRAM_BOT_TOKEN", "")
        if token and ":" in token:  # Any agent with a bot token gets Telegram polling
            tasks.append(asyncio.create_task(
                _run_telegram_bot(a["name"], token, shutdown)
            ))

    # IMAP poller (single task for all email agents with imap_polling enabled)
    # CR-thread: Only agents with imap_polling=true poll IMAP (e.g. Support).
    # Agents that only send (e.g. Innendienst) keep the email skill but don't poll.
    email_agents = [
        a for a in agents
        if "email" in a["config"].get("skills", a["config"].get("modules", []))
        and a["config"].get("imap_polling", False)
        and a["secrets"].get("EMAIL_ADDRESS")
        and a["secrets"].get("EMAIL_PASSWORD")
    ]
    if email_agents:
        tasks.append(asyncio.create_task(
            _run_imap_poller(email_agents, shutdown)
        ))

    # Outbound sender (Telegram)
    tasks.append(asyncio.create_task(
        _run_outbound_sender(agents, shutdown)
    ))

    # Outbound email sender (SMTP)
    tasks.append(asyncio.create_task(
        _run_outbound_email_sender(agents, shutdown)
    ))

    # Agent watcher: detects new agents and starts their bots dynamically
    tasks.append(asyncio.create_task(
        _watch_new_agents(known_agents, shutdown, tasks)
    ))

    tg_count = len(known_agents)
    log.info(f"Active: {tg_count} agent(s) known, {len(email_agents)} IMAP account(s), 1 outbound TG sender, 1 outbound email sender, 1 agent watcher")

    await asyncio.gather(*tasks)
    log.info("Shared Listener stopped.")


if __name__ == "__main__":
    asyncio.run(main())
