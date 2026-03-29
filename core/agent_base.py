"""
AIMOS Agent Base — v4.1.0 (Shard Kernel)
==========================================
Schlanker Agent-Kernel mit:
  - Zero-Config DB (_ensure_schema + _seed_default_agent)
  - 4-Strategien Tool-Parser (XML, JSON, Python-Style, Bare)
  - Output-Firewall (clean_llm_response: CJK + Thought-Leak Filter)
  - Key-Inheritance für Secrets (Agent > global_settings > .env)
  - Audit-Logging (storage/agents/{agent_id}/api_audit.log)
  - 90s Inaktivitäts-Watchdog
  - Queue-Drain beim Start (pending_messages, 120s Timeout)
  - Bug #14 Fix: hasattr(func, '__code__') Guard in _execute_tool
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import asyncpg
import httpx

from core.config import Config, SecretFilter

_log = logging.getLogger("AIMOS.Agent")

# ── Output Firewall (ported from v3.8.2 chat.py) ─────────────────────────────

# CJK Unified + Extension A + Compat Ideographs + Symbols/Punctuation + Fullwidth Forms
_CJK_RE = re.compile(
    r"[\u3000-\u303f"        # CJK Symbols and Punctuation (〇、。「」…)
    r"\u3400-\u4dbf"         # CJK Extension A
    r"\u4e00-\u9fff"         # CJK Unified Ideographs
    r"\uf900-\ufaff"         # CJK Compatibility Ideographs
    r"\uff00-\uffef]+"       # Fullwidth Forms (！＂＃＄… fullwidth ASCII variants)
)
# CR-114b: Thought-leak filter — only remove system tag leaks, not entire sentences.
# The aggressive sentence filtering (removed "Ich verstehe", "Als KI") was needed for
# Qwen 2.5:14B but causes false positives on 3.5:27B → empty response → bad fallback.
_THOUGHT_RE = re.compile(
    r"</?(?:rules|emergency[_a-z]*|system|instructions?|anweisungen?|system_core)>[^\n]*"
    r"|Gem[äa][ß]s? (?:meinen?|den) (?:Anweisungen|System-?[Pp]rompt)[^.!?\n]*[.!?\n]?",
    re.IGNORECASE | re.MULTILINE,
)
_FILTER_FALLBACK = ""  # Empty — caller handles fallback via external API or context
_CHINESE_STOP_TOKENS = ["，", "。", "！", "？", "、", "「", "」"]
STOP_SEQUENCES = ["</tool_call>", "<|im_start|>", "<|im_end|>"]

# CR-114: Phantom-action detector removed — native tool-calling API makes it obsolete.
# Tools are now called via structured API, not text tags. No more false "I'll do X" without action.

def clean_llm_response(text: str, tool_was_called: bool = False) -> str:
    """Strip CJK chars and thought-leaks. Returns fallback if result is empty."""
    if Config.CLEAN_CJK:
        text = _CJK_RE.sub("", text)
    text = _THOUGHT_RE.sub("", text)
    text = text.strip()
    if not text:
        return _FILTER_FALLBACK
    return text


# ── Tool-Call Regexes (4-Strategy Parser) ─────────────────────────────────────

_TC_XML = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TC_JSON = re.compile(
    r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*\}',
    re.DOTALL,
)


def _repair_json(s: str) -> str:
    """CR-170: Attempt to repair malformed JSON from LLM."""
    s = s.strip()
    # Count and balance braces
    open_b = s.count('{') - s.count('}')
    open_sq = s.count('[') - s.count(']')
    if open_b > 0:
        s += '}' * open_b
    if open_sq > 0:
        s += ']' * open_sq
    # Fix trailing comma before closing brace/bracket
    s = re.sub(r',\s*}', '}', s)
    s = re.sub(r',\s*]', ']', s)
    return s

# ── Schema DDL ────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    status          TEXT DEFAULT 'idle',
    config          JSONB DEFAULT '{}'::jsonb,
    env_secrets     JSONB DEFAULT '{}'::jsonb,
    wake_up_needed  BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pending_messages (
    id          SERIAL PRIMARY KEY,
    agent_name  TEXT NOT NULL,
    sender_id   BIGINT,
    content     TEXT NOT NULL,
    kind        TEXT DEFAULT 'text',
    file_path   TEXT,
    thread_id   TEXT DEFAULT '',
    processed   BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS aimos_chat_histories (
    id          SERIAL PRIMARY KEY,
    agent_name  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    session_id  TEXT DEFAULT '',
    thread_id   TEXT DEFAULT ''
);

-- CR-209: Migration for existing databases
ALTER TABLE aimos_chat_histories ADD COLUMN IF NOT EXISTS session_id TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_chat_session ON aimos_chat_histories(agent_name, session_id);

-- CR-thread: thread_id migration for existing databases
ALTER TABLE pending_messages ADD COLUMN IF NOT EXISTS thread_id TEXT DEFAULT '';
ALTER TABLE aimos_chat_histories ADD COLUMN IF NOT EXISTS thread_id TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_chat_thread ON aimos_chat_histories(agent_name, thread_id);
CREATE INDEX IF NOT EXISTS idx_pending_thread ON pending_messages(agent_name, thread_id);

CREATE TABLE IF NOT EXISTS global_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_jobs (
    id              SERIAL PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    cron_expr       TEXT,
    scheduled_time  TIMESTAMPTZ NOT NULL,
    task_prompt     TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    source          TEXT DEFAULT 'agent',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    fired_at        TIMESTAMPTZ
);
"""

_WATCHDOG_TIMEOUT = 900  # 15 minutes — CR-166: extended for long multi-tool chains
_QUEUE_MSG_TIMEOUT = 120  # seconds per pending message

# CR-091: Cognitive Balance — maps slider value to memory/predict limits
# Adjusted for Qwen 2.5:14b (~14K context, ~9GB model)
_CB_MEMORY_LIMITS = {0: 50, 1: 35, 2: 25, 3: 15, 4: 8}
_CB_NUM_PREDICT   = {0: 512, 1: 1024, 2: 1536, 3: 2048, 4: 3072}

# CR-142: Execution Rings — Trust-Level per agent
# Ring 0 = Read Only (search, recall, status)
# Ring 1 = Write (send messages, write files, set reminders)
# Ring 2 = System (external APIs, credential changes, web automation)
_TOOL_RINGS = {
    # Ring 0 — Read Only
    "recall": 0, "remember": 0, "forget": 0,
    "web_search": 0, "current_time": 0, "system_status": 0,
    "read_file": 0, "search_in_file": 0, "check_credentials": 0,
    "list_workspace": 0, "list_shared": 0, "read_shared": 0,
    "read_public": 0, "fetch_user_mail": 0, "search_mail": 0, "read_mail": 0,
    "check_gs_results": 0, "check_today": 0,
    "check_open_requests": 0,
    "add_event": 0, "list_events": 0, "complete_event": 0, "delete_event": 0,
    "find_contact": 0, "list_contacts": 0, "add_contact": 1,
    "read_public": 0,
    "analyze_beam": 0, "lookup_profile": 0, "suggest_profile": 0,
    "estimate_cost": 0, "lookup_regulation": 0, "update_profile_db": 1,
    "analyze_frame": 0, "generate_dxf": 1,
    "get_customer_balance": 0, "list_unpaid_invoices": 0,
    "search_transactions": 0, "get_daily_summary": 0,
    "remote_list_files": 0, "remote_read_file": 0,
    # Ring 1 — Write
    "send_telegram_message": 1, "send_voice_message": 1, "send_to_agent": 1,
    "send_email": 1, "write_file": 1, "send_telegram_file": 1,
    "set_reminder": 1, "list_jobs": 1,
    "write_shared": 1, "remote_write_file": 1,
    "track_request": 1, "close_request": 1,
    "convert_document": 1, "extract_pdf_text": 1,
    # Ring 2 — System
    "ask_external": 2, "update_credential": 2,
    "web_login_and_extract": 2, "web_browse": 2,
    "remote_setup_guide": 2,
}


class AIMOSAgent:
    """Core AIMOS v4.1.0 agent kernel.

    Lifecycle: start() → _drain_queue() → run_loop() → stop()
    """

    def __init__(self, agent_name: str, config: dict | None = None):
        self.agent_name: str = agent_name.lower()
        self.config: dict = config or {}
        self.logger = logging.getLogger(f"AIMOS.{self.agent_name}")

        self._pool: Optional[asyncpg.Pool] = None
        self._tools: dict[str, Callable] = {}
        self._history: list[dict] = []
        self._last_activity: float = 0.0
        self._audit_path: Optional[Path] = None
        self._memory_db_path: Optional[Path] = None
        self._recent_responses: list[str] = []  # last 2 responses for loop detection

        # Schema prefix: memory_{agent_id} (sanitized to valid PG identifier)
        _safe = re.sub(r"[^a-z0-9]", "_", self.agent_name)
        self._schema_prefix: str = f"memory_{_safe}"

        self._system_prompt: str = self.config.get("system_prompt", (
            f"You are {self.agent_name}, an AIMOS agent. "
            "Answer questions precisely. Use tools when needed."
        ))

    def _sanitize_reply(self, reply: str) -> str:
        """CR-161: Strip internal data patterns from agent replies before user delivery."""
        import re
        # Remove internal message prefixes
        reply = re.sub(r'\[Nachricht von \w+\]', '', reply)
        # CR-186: Also strip English variant
        reply = re.sub(r'\[Message from \w+\]', '', reply)
        # Remove vault placeholders that weren't de-anonymized
        reply = re.sub(r'__VAULT_\w+_\d+__', '[REDACTED]', reply)
        # Remove system prompt fragments (common patterns)
        reply = re.sub(r'TOOL_(?:START|OK|ERROR|RESULT)\b', '', reply)
        # Remove raw JSON tool outputs that leaked
        reply = re.sub(r'\{"tool_call_id":[^}]+\}', '', reply)
        # Remove raw tool-call fragments (XML and JSON variants)
        reply = re.sub(r'<tool_call>\s*\{[^}]*\}', '', reply, flags=re.DOTALL)
        reply = re.sub(r'\{"name"\s*:\s*"[^"]*"\s*,\s*"arguments"\s*:', '', reply)
        # Remove customer context hints that leaked
        reply = re.sub(r'\[Current customer:[^\]]*\]', '', reply)
        reply = re.sub(r'\[IMPORTANT: This conversation[^\]]*\]', '', reply)
        # Clean up multiple spaces/newlines from removals
        reply = re.sub(r'\n{3,}', '\n\n', reply)
        return reply.strip()

    async def dispatch_response(self, reply: str, msg: dict) -> str | None:
        """Route a reply to the correct channel based on message source.

        Args:
            reply: The agent's response text.
            msg: The original pending_message dict (kind, sender_id, etc.)

        Returns:
            Delivery status string, or None if no route found.
        """
        # CR-161: Sanitize reply before any outbound delivery
        reply = self._sanitize_reply(reply)

        kind = msg.get("kind", "")
        sender_id = msg.get("sender_id")

        # Always update heartbeat, even if dispatch fails
        if self._pool:
            try:
                await self._pool.execute(
                    "UPDATE agents SET updated_at=NOW() WHERE name=$1", self.agent_name
                )
            except Exception:
                pass

        if "telegram" in kind and sender_id and sender_id != 0:
            # All modes use DB relay — shared_listener handles Telegram delivery
            if self._pool:
                try:
                    await self._pool.execute(
                        "INSERT INTO pending_messages (agent_name, sender_id, content, kind, processed) "
                        "VALUES ($1, $2, $3, 'outbound_telegram', FALSE)",
                        self.agent_name, int(sender_id), reply,
                    )
                    self.logger.info(
                        f"[Relay] Outbound to DB: "
                        f"agent={self.agent_name} chat_id={sender_id} len={len(reply)}"
                    )
                    return f"telegram:outbound:{sender_id}"
                except Exception as exc:
                    self.logger.error(f"[Relay] DB write FAILED: {exc}")
                    return None
            self.logger.error("[Relay] No DB pool — cannot write outbound")
            return None

        if kind == "email":
            # Block raw tool-call output from being sent as email
            if any(marker in reply for marker in ['_icall_', '<tool_call>', '{"name":', '"arguments":']):
                self.logger.warning(f"[Relay] Blocking raw tool-call as email reply")
                return "email:tool_call_blocked"
            # L1: Auto-reply to email sender — queue as outbound_email for shared_listener
            # Skip internal status messages — these are not customer replies
            _status_patterns = [
                "erfolgreich gesendet", "erfolgreich versendet",
                "warte nun auf", "warte auf r",
                "nachricht an den innendienst", "an den innendienst gesendet",
                "wurde gesendet", "habe ich weitergeleitet",
                "innendienst weitergeleitet", "anfrage weitergeleitet",
                "successfully sent", "waiting for", "forwarded to",
                "die e-mail wurde", "die nachricht wurde",
                "angebot wird", "angebot erstellt", "in kuerze", "in kürze",
                "wird ihnen zugesandt", "senden es ihnen", "erhalten sie",
                "wir haben ein angebot", "within the next",
                "wird vorbereitet", "wird derzeit", "sobald es bereit",
                "being prepared", "will be prepared",
            ]
            # Block status messages (but NOT substantive customer replies)
            # Check against reply WITHOUT signature (signature inflates length)
            _reply_no_sig = reply.split("---")[0] if "---" in reply else reply
            _is_status = any(p in reply.lower() for p in _status_patterns) and len(_reply_no_sig.strip()) < 400
            _delegated_this_cycle = getattr(self, '_delegated_this_cycle', False)
            if _delegated_this_cycle:
                self._delegated_this_cycle = False
                # After delegation, allow a short confirmation to the customer
                # but block anything that promises content (attachments, offers, prices)
                _promise_patterns = [
                    "im anhang", "anhang dieses", "anhang dieser",
                    "bitte finden sie", "finden sie alle details",
                    "angebot wird ihnen", "angebot zugesandt",
                    "angebot inklusive", "angebot für",
                    "in the attachment", "please find attached",
                    "kosten", "preis", "eur", "€",
                ]
                _has_promise = any(p in reply.lower() for p in _promise_patterns)
                if _has_promise:
                    self.logger.info(f"[Relay] Blocking promise-email after delegation: {reply[:80]}")
                    return "email:post_delegation_promise_blocked"
                # Short confirmation is OK (e.g. "Ihre Anfrage wird bearbeitet")
                self.logger.info(f"[Relay] Allowing confirmation email after delegation: {reply[:80]}")
            if _is_status:
                self.logger.info(f"[Relay] Skipping internal status as email reply: {reply[:80]}")
                return "email:status_skipped"
            import re as _re_email
            msg_content = msg.get("content", "")
            from_match = _re_email.search(r'Von:\s*(.+?)[\n\r]', msg_content)
            subj_match = _re_email.search(r'Betreff:\s*(.+?)[\n\r]', msg_content)
            if from_match and self._pool:
                to_raw = from_match.group(1).strip()
                # Extract bare email address from "Name <addr>" format
                _email_bare = _re_email.search(r'[\w.+-]+@[\w.-]+', to_raw)
                to_addr = _email_bare.group(0) if _email_bare else to_raw
                # AC-08: Block auto-replies to noreply/mailer-daemon/suspicious senders
                _blocked_senders = [
                    "noreply", "no-reply", "donotreply", "mailer-daemon",
                    "postmaster", "bounce", "auto-reply", "autoreply",
                ]
                _to_lower = to_addr.lower()
                if any(b in _to_lower for b in _blocked_senders):
                    self.logger.info(f"[AC-08] Blocked auto-reply to {to_addr} (noreply/bounce address)")
                    return "email:blocked_noreply"
                # Check email_allowlist if configured
                _allowlist = self.config.get("email_allowlist", [])
                if _allowlist:
                    _allowed = any(
                        _to_lower == a.lower() or _to_lower.endswith("@" + a.lower().lstrip("@"))
                        for a in _allowlist
                    )
                    if not _allowed:
                        self.logger.warning(f"[AC-08] Blocked auto-reply to {to_addr} — not in allowlist")
                        return f"email:blocked_allowlist:{to_addr}"
                subject = "RE: " + (subj_match.group(1).strip() if subj_match else "Ihre Anfrage")
                import json as _json_email
                # AC-05: Dedup — check if we already sent an outbound for this thread recently
                _cur_thread = getattr(self, '_current_thread_id', '') or ''
                if _cur_thread:
                    _recent = await self._pool.fetchval(
                        "SELECT COUNT(*) FROM pending_messages "
                        "WHERE agent_name=$1 AND kind='outbound_email' AND thread_id=$2 "
                        "AND created_at > NOW() - INTERVAL '2 minutes'",
                        self.agent_name, _cur_thread,
                    )
                    if _recent and _recent > 0:
                        self.logger.info(
                            f"[AC-05] Skipping duplicate outbound email for thread {_cur_thread[:30]} "
                            f"({_recent} already sent in last 2 min)"
                        )
                        return f"email:dedup_skipped:{_cur_thread}"
                # Append email signature on code level (LLM can't be trusted to include it)
                _email_sig = self.config.get("email_signature", "")
                if _email_sig:
                    import re as _re_sig
                    # Strip everything after common closing phrases — the LLM often copies
                    # the customer's signature from the inbound email after the closing.
                    # We truncate at the FIRST closing phrase and then append the real signature.
                    _closing_match = _re_sig.search(
                        r'\n\s*(?:Mit freundlichen Gr[üu](?:ß|ss)en|Viele Gr[üu](?:ß|ss)e|'
                        r'Freundliche Gr[üu](?:ß|ss)e|Best regards|Kind regards|Sincerely|'
                        r'Herzliche Gr[üu](?:ß|ss)e|MfG)\s*[,.]?\s*\n',
                        reply, flags=_re_sig.IGNORECASE
                    )
                    if _closing_match:
                        # Keep text up to and including the closing phrase line
                        reply = reply[:_closing_match.end()].rstrip()
                    else:
                        # Fallback: strip trailing lines matching company suffixes
                        reply = _re_sig.sub(
                            r'\n*(?:^.{0,60}(?:GmbH|Support|Service|Kundenservice|Kundendienst)\s*$)\s*$',
                            '', reply, flags=_re_sig.IGNORECASE | _re_sig.MULTILINE
                        ).rstrip()
                    reply = reply.rstrip() + _email_sig
                email_data = _json_email.dumps({"to": to_addr, "subject": subject, "body": reply})
                try:
                    await self._pool.execute(
                        "INSERT INTO pending_messages (agent_name, sender_id, content, kind, processed, thread_id) "
                        "VALUES ($1, 0, $2, 'outbound_email', FALSE, $3)",
                        self.agent_name, email_data, _cur_thread,
                    )
                    self.logger.info(
                        f"[Relay] Outbound email queued: "
                        f"agent={self.agent_name} to={to_addr} subject={subject[:50]}"
                    )
                    return f"email:auto_reply_queued:{to_addr}"
                except Exception as exc:
                    self.logger.error(f"[Relay] Outbound email DB write FAILED: {exc}")
                    return None
            if not from_match:
                self.logger.warning("[Relay] Email reply: no sender address found in message content")
                return "email:no_sender_found"
            self.logger.error("[Relay] No DB pool — cannot write outbound email")
            return None

        if kind == "dashboard":
            # Note: reply is already persisted by _persist_message() — no extra INSERT needed
            self.logger.info(f"Reply → Dashboard: {reply[:100]}")
            return "dashboard:db_stored"

        if kind == "scheduled_job":
            # Skip if agent already sent to Telegram via send_telegram_message in this cycle
            if getattr(self, '_telegram_sent_this_cycle', False):
                self.logger.info(f"[Relay] Scheduled job: skipping Telegram (already sent this cycle)")
                return "scheduled_job:already_sent"
            # CR-115: Scheduled job replies should reach the user — find their last known chat_id
            if self._pool:
                try:
                    row = await self._pool.fetchrow(
                        "SELECT sender_id FROM pending_messages "
                        "WHERE agent_name=$1 AND kind IN ('telegram','telegram_voice','telegram_doc') "
                        "AND sender_id IS NOT NULL AND sender_id != 0 "
                        "ORDER BY id DESC LIMIT 1",
                        self.agent_name,
                    )
                    if row and row["sender_id"]:
                        await self._pool.execute(
                            "INSERT INTO pending_messages (agent_name, sender_id, content, kind, processed) "
                            "VALUES ($1, $2, $3, 'outbound_telegram', FALSE)",
                            self.agent_name, int(row["sender_id"]), reply,
                        )
                        self.logger.info(f"[Relay] Scheduled job → Telegram chat_id={row['sender_id']}")
                        return f"scheduled_job:telegram:{row['sender_id']}"
                except Exception as exc:
                    self.logger.error(f"[Relay] Scheduled job delivery failed: {exc}")
            self.logger.info(f"Reply → Scheduled job (no Telegram user found): {reply[:80]}")
            return "scheduled_job:no_route"

        if kind == "voice_local":
            # Tag the last assistant message (already saved by think/_persist_message)
            # with source=voice_local so voice_listener can find and speak it
            if self._pool:
                try:
                    await self._pool.execute(
                        "UPDATE aimos_chat_histories SET metadata = metadata || $1::jsonb "
                        "WHERE id = (SELECT id FROM aimos_chat_histories "
                        "WHERE agent_name=$2 AND role='assistant' ORDER BY id DESC LIMIT 1)",
                        json.dumps({"source": "voice_local", "msg_id": msg.get("id")}),
                        self.agent_name,
                    )
                except Exception:
                    pass
            self.logger.info(f"Reply → Voice local (TTS): {reply[:80]}")
            return "voice_local:tts_queued"

        if kind == "voice":
            self.logger.info("Reply → Voice TTS (not yet implemented)")
            return "voice:pending"

        if kind == "internal":
            # CR-115: Auto-reply to the sending agent AND forward to user's Telegram
            import re as _re
            if self._pool:
                try:
                    # Extract sender agent name from "[Nachricht von neo] ..."
                    content_str = msg.get("content", "")
                    sender_match = re.search(r"\[Nachricht von (\w+)\]", content_str)
                    if sender_match:
                        sender_agent = sender_match.group(1).lower()

                        # CR-202: Ping-Pong Limit — max 1 round-trip between two agents
                        recent_count = await self._pool.fetchval(
                            "SELECT COUNT(*) FROM pending_messages "
                            "WHERE kind='internal' "
                            "AND ((agent_name=$1 AND content LIKE $2) OR (agent_name=$3 AND content LIKE $4)) "
                            "AND created_at > NOW() - INTERVAL '10 minutes'",
                            sender_agent, f"%[Nachricht von {self.agent_name}]%",
                            self.agent_name, f"%[Nachricht von {sender_agent}]%",
                        )
                        # Forward reply to customer — but NOT if fallback already handled it
                        if msg.get("_fallback_handled"):
                            self.logger.info(
                                f"[Relay] Skipping auto-forward — external fallback already replied"
                            )
                            return "internal:fallback_handled"

                        tg_row = await self._pool.fetchrow(
                            "SELECT sender_id FROM pending_messages "
                            "WHERE agent_name=$1 AND kind IN ('telegram','telegram_voice','telegram_doc') "
                            "AND sender_id IS NOT NULL AND sender_id != 0 "
                            "ORDER BY id DESC LIMIT 1",
                            self.agent_name,
                        )
                        if tg_row and tg_row["sender_id"]:
                            await self._pool.execute(
                                "INSERT INTO pending_messages "
                                "(agent_name, sender_id, content, kind, processed) "
                                "VALUES ($1, $2, $3, 'outbound_telegram', FALSE)",
                                self.agent_name, tg_row["sender_id"], reply,
                            )
                            self.logger.info(
                                f"[Relay] Internal report from {sender_agent} "
                                f"→ forwarded to Telegram customer {tg_row['sender_id']}"
                            )
                        else:
                            # No Telegram customer — check if there's an email customer
                            # Use thread_id to find the original email
                            _thread = getattr(self, '_current_thread_id', '') or ''
                            if _thread.startswith("email:"):
                                email_row = await self._pool.fetchrow(
                                    "SELECT content FROM pending_messages "
                                    "WHERE agent_name=$1 AND kind='email' AND thread_id=$2 "
                                    "ORDER BY id ASC LIMIT 1",
                                    self.agent_name, _thread,
                                )
                                if email_row:
                                    import json as _json_relay
                                    _from = re.search(r'Von:\s*(.+?)[\n\r]', email_row["content"])
                                    _subj = re.search(r'Betreff:\s*(.+?)[\n\r]', email_row["content"])
                                    if _from:
                                        _to_raw = _from.group(1).strip()
                                        _email_bare = re.search(r'[\w.+-]+@[\w.-]+', _to_raw)
                                        _to_addr = _email_bare.group(0) if _email_bare else _to_raw
                                        _subject = "RE: " + (_subj.group(1).strip() if _subj else "Ihre Anfrage")
                                        # Apply email signature for relay emails too
                                        _relay_sig = self.config.get("email_signature", "")
                                        _relay_body = reply
                                        if _relay_sig:
                                            # Strip copied customer signatures after closing phrases
                                            _closing = re.search(
                                                r'\n\s*(?:Mit freundlichen Gr[üu](?:ß|ss)en|Viele Gr[üu](?:ß|ss)e|'
                                                r'Freundliche Gr[üu](?:ß|ss)e|Best regards|Kind regards|MfG)\s*[,.]?\s*\n',
                                                _relay_body, flags=re.IGNORECASE
                                            )
                                            if _closing:
                                                _relay_body = _relay_body[:_closing.end()].rstrip()
                                            _relay_body = _relay_body.rstrip() + _relay_sig
                                        _email_data = _json_relay.dumps({"to": _to_addr, "subject": _subject, "body": _relay_body})
                                        await self._pool.execute(
                                            "INSERT INTO pending_messages "
                                            "(agent_name, sender_id, content, kind, processed, thread_id) "
                                            "VALUES ($1, 0, $2, 'outbound_email', FALSE, $3)",
                                            self.agent_name, _email_data, _thread,
                                        )
                                        self.logger.info(
                                            f"[Relay] Internal report → email reply queued to {_to_addr}"
                                        )
                        # Do NOT relay back to the agent (no ping-pong)
                        self.logger.info(
                            f"[Relay] Internal from {sender_agent} → customer only, no relay back"
                        )

                    # CR-143: If the agent called send_telegram_message during this cycle,
                    # the user already got the message directly. Otherwise, check if the
                    # internal message was a "tell the user" request — if so, forward to Telegram.
                    if not getattr(self, '_telegram_sent_this_cycle', False):
                        # Check if the internal message asked this agent to contact a user
                        _forward_re = re.compile(
                            r'(?:sag|tell|informier|schreib|schick|send|kontaktier|bildir|söyle|yaz|ilet)'
                            r'.*(?:U[gğ]ur|user|nutzer|kullanıcı|müşteri)',
                            re.IGNORECASE
                        )
                        if _forward_re.search(content_str):
                            # Find last known Telegram user for this agent
                            tg_row = await self._pool.fetchrow(
                                "SELECT sender_id FROM pending_messages "
                                "WHERE agent_name=$1 AND kind IN ('telegram','telegram_voice','telegram_doc') "
                                "AND sender_id IS NOT NULL AND sender_id != 0 "
                                "ORDER BY id DESC LIMIT 1",
                                self.agent_name,
                            )
                            if tg_row and tg_row["sender_id"]:
                                await self._pool.execute(
                                    "INSERT INTO pending_messages (agent_name, sender_id, content, kind, processed) "
                                    "VALUES ($1, $2, $3, 'outbound_telegram', FALSE)",
                                    self.agent_name, int(tg_row["sender_id"]), reply,
                                )
                                self.logger.info(
                                    f"[Relay] Internal → Telegram forward to chat_id={tg_row['sender_id']} "
                                    f"(triggered by user-mention in internal msg)"
                                )
                                return f"internal:relayed+telegram:{tg_row['sender_id']}"

                    return f"internal:relayed"
                except Exception as exc:
                    self.logger.error(f"[Relay] Internal delivery failed: {exc}")
            self.logger.info(f"Reply → Internal (no pool): {reply[:80]}")
            return "internal:no_route"

        # CR-129: Catch-all — route to user's LAST ACTIVE CONNECTOR (not just Telegram)
        # CR-180: Sender-ID passthrough — this catch-all intentionally looks up the last
        # sender_id from pending_messages to route replies for unknown/unhandled kinds.
        # The lookup result is logged below for auditability.
        if getattr(self, '_telegram_sent_this_cycle', False):
            self.logger.info(f"[Relay] {kind}: skipping (already sent this cycle)")
            return f"{kind}:already_sent"
        if self._pool:
            try:
                # Find the last inbound user message — whatever connector it came from
                row = await self._pool.fetchrow(
                    "SELECT sender_id, kind FROM pending_messages "
                    "WHERE agent_name=$1 "
                    "AND kind NOT LIKE 'outbound_%' AND kind NOT IN ('internal','scheduled_job','text') "
                    "AND sender_id IS NOT NULL AND sender_id != 0 "
                    "ORDER BY id DESC LIMIT 1",
                    self.agent_name,
                )
                # CR-180: Log the sender-ID lookup result for audit trail
                self.logger.info(
                    f"[Relay] CR-180 sender-ID lookup: kind={kind} "
                    f"found={'yes' if row else 'no'} "
                    f"sender_id={row['sender_id'] if row else 'N/A'} "
                    f"connector={row['kind'] if row else 'N/A'}"
                )
                if row and row["sender_id"]:
                    # Determine outbound kind based on inbound connector
                    inbound_kind = row["kind"] or "telegram"
                    if "telegram" in inbound_kind:
                        outbound_kind = "outbound_telegram"
                    elif "email" in inbound_kind:
                        # Email replies handled by agent's send_email tool, not dispatch
                        self.logger.info(f"[Relay] {kind} → Last connector was email (agent must use send_email)")
                        return f"{kind}:email_hint"
                    elif "voice" in inbound_kind:
                        outbound_kind = "outbound_telegram"  # voice users also have Telegram
                    else:
                        outbound_kind = f"outbound_{inbound_kind}"

                    await self._pool.execute(
                        "INSERT INTO pending_messages (agent_name, sender_id, content, kind, processed) "
                        "VALUES ($1, $2, $3, $4, FALSE)",
                        self.agent_name, int(row["sender_id"]), reply, outbound_kind,
                    )
                    self.logger.info(f"[Relay] {kind} → {outbound_kind} (last connector: {inbound_kind})")
                    return f"{kind}:{outbound_kind}:{row['sender_id']}"
            except Exception as exc:
                self.logger.error(f"[Relay] Catch-all delivery failed: {exc}")
        self.logger.warning(f"No route for kind={kind} sender={sender_id}")
        return None

    # ══════════════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    async def start(self):
        """Boot: DB → Schema → Seed → Secrets → Audit → History → Drain Queue."""
        self.logger.info(f"[{self.agent_name}] Starting v4.1.0 …")

        self._pool = await asyncpg.create_pool(
            **Config.get_db_params(), min_size=1, max_size=5,
            command_timeout=15,  # CR-138: prevent indefinite DB hangs (root cause of agent freezes)
        )

        # CR-138: Startup with 60s total timeout — prevents infinite hang on DB issues
        try:
            await asyncio.wait_for(self._startup_sequence(), timeout=60)
        except asyncio.TimeoutError:
            self.logger.error(f"[{self.agent_name}] Startup timed out after 60s — aborting")
            await self.stop()
            raise RuntimeError(f"Agent {self.agent_name} startup timed out")

    async def _startup_sequence(self):
        """Internal: all startup DB operations (called with timeout from start())."""
        await self._ensure_schema()
        await self._seed_default_agent()
        await self._load_db_config()
        await self._load_secrets()
        self._init_audit_log()
        self._init_memory_db()
        await self._compress_history()  # Free context budget before loading history
        await self._restore_history()
        await self._drain_queue()

        # Force-claim this identity: set active + clean any stale state
        async with self._pool.acquire(timeout=5) as conn:
            await conn.execute(
                "UPDATE agents SET status='active', updated_at=NOW(), pid=$2 WHERE name=$1",
                self.agent_name, os.getpid(),
            )

        self._touch()
        self.logger.info(f"[{self.agent_name}] Agent ready (status=active).")

    async def _compress_history(self):
        """Compress old chat history to free context budget.

        - Truncates tool results older than 15 messages to 200 chars
        - Caps total history dynamically: shorter cap for agents with long system prompts
        - Preserves full content for the last 15 messages (active conversation)
        This runs at startup and shutdown — ensures the agent always starts clean.
        """
        if not self._pool:
            return
        try:
            # 1. Truncate old tool results (role='user' with tool output pattern)
            truncated = await self._pool.fetchval(
                "WITH old_tool_msgs AS ("
                "  SELECT id FROM aimos_chat_histories "
                "  WHERE agent_name=$1 AND role='user' AND LENGTH(content) > 500 "
                "  AND (content LIKE 'Tool %% returned:%' OR content LIKE '%% returned:\n%%') "
                "  AND id NOT IN ("
                "    SELECT id FROM aimos_chat_histories WHERE agent_name=$1 "
                "    ORDER BY id DESC LIMIT 15"
                "  )"
                ") "
                "UPDATE aimos_chat_histories SET content = LEFT(content, 200) || '\n[... truncated]' "
                "WHERE id IN (SELECT id FROM old_tool_msgs) "
                "RETURNING id",
                self.agent_name,
            )

            # 2. Dynamic cap based on system prompt length
            # Long prompts (>5K chars) → fewer messages to leave room for context
            prompt_len = len(self._system_prompt or "") + len(self._CORE_SYSTEM_PROMPT or "")
            if prompt_len > 8000:
                max_msgs = 15  # Very long prompt (e.g. Mühendis with 11K)
            elif prompt_len > 5000:
                max_msgs = 25
            else:
                max_msgs = 35  # Short prompt agents get more history

            deleted = await self._pool.fetchval(
                "WITH excess AS ("
                "  SELECT id FROM aimos_chat_histories "
                "  WHERE agent_name=$1 "
                "  AND id NOT IN ("
                "    SELECT id FROM aimos_chat_histories WHERE agent_name=$1 "
                "    ORDER BY id DESC LIMIT $2"
                "  )"
                ") "
                "DELETE FROM aimos_chat_histories WHERE id IN (SELECT id FROM excess) "
                "RETURNING id",
                self.agent_name, max_msgs,
            )

            if truncated or deleted:
                self.logger.info(
                    f"[{self.agent_name}] History compressed: "
                    f"{len(truncated) if truncated else 0} tool results truncated, "
                    f"{len(deleted) if deleted else 0} old messages deleted"
                )
        except Exception as exc:
            self.logger.debug(f"[{self.agent_name}] History compression failed: {exc}")

    async def stop(self):
        """Graceful shutdown — set status offline, close pool.

        Safe to call multiple times (idempotent).
        """
        if self._pool is None:
            return  # already stopped
        self.logger.info(f"[{self.agent_name}] Shutting down …")
        # CR-098: Do NOT flush VRAM on normal stop — all agents use the same model,
        # Ollama keeps it loaded for 30min (keep_alive). Next agent reuses it instantly.
        # Flush only happens on system shutdown (api_master_shutdown in routes.py).
        pool = self._pool
        self._pool = None  # mark as stopped immediately (prevents re-entry)
        try:
            await pool.execute(
                "UPDATE agents SET status='offline', updated_at=NOW(), pid=NULL WHERE name=$1",
                self.agent_name,
            )
        except Exception:
            pass
        await pool.close()
        self.logger.info(f"[{self.agent_name}] Stopped.")

    # ══════════════════════════════════════════════════════════════════════════
    #  Zero-Config DB  (Self-Healing)
    # ══════════════════════════════════════════════════════════════════════════

    async def _ensure_schema(self):
        """Create core tables + agent-specific schema if missing (idempotent).

        Also migrates v3.x aimos_chat_histories (session_id/message JSONB)
        to v4.1 schema (agent_name/role/content) if needed.
        """
        async with self._pool.acquire() as conn:
            existing = {
                row["tablename"]
                for row in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
            }

            # Migrate v3.x chat_histories → v4.1 schema
            if "aimos_chat_histories" in existing:
                cols = {
                    row["column_name"]
                    for row in await conn.fetch(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='aimos_chat_histories' AND table_schema='public'"
                    )
                }
                if "role" not in cols:
                    self.logger.info(f"[{self.agent_name}] Migrating aimos_chat_histories v3→v4 …")
                    await conn.execute(
                        "ALTER TABLE aimos_chat_histories RENAME TO aimos_chat_histories_v3"
                    )
                    # Remove from 'existing' so CREATE TABLE IF NOT EXISTS runs
                    existing.discard("aimos_chat_histories")

            needed = {"agents", "pending_messages", "aimos_chat_histories", "global_settings", "agent_jobs"}
            if not needed.issubset(existing):
                self.logger.info(f"[{self.agent_name}] Creating missing tables …")
                await conn.execute(_SCHEMA_SQL)
                self.logger.info(f"[{self.agent_name}] Schema OK.")

            # Migration: ensure kind column is TEXT (old schema had VARCHAR(16))
            await conn.execute(
                "ALTER TABLE pending_messages ALTER COLUMN kind TYPE TEXT"
            )

            # Agent-specific schema: memory_{agent_id}
            await conn.execute(
                f"CREATE SCHEMA IF NOT EXISTS {self._schema_prefix}"
            )
            await conn.execute(
                f"SET search_path TO {self._schema_prefix}, public"
            )
            self.logger.debug(
                f"[{self.agent_name}] Schema '{self._schema_prefix}' ready."
            )

    async def _seed_default_agent(self):
        """Ensure the 'neo' default agent exists in the agents table."""
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM agents WHERE name='neo'"
            )
            if not exists:
                await conn.execute(
                    "INSERT INTO agents (name, status, config) VALUES ('neo', 'idle', $1)",
                    json.dumps({"system_prompt": "You are the default AIMOS agent."}),
                )
                self.logger.info("Seeded default agent 'neo'.")

            # Register ourselves if not 'neo'
            row = await conn.fetchval(
                "SELECT 1 FROM agents WHERE name=$1", self.agent_name
            )
            if not row:
                safe_cfg = SecretFilter.redact(self.config)
                await conn.execute(
                    "INSERT INTO agents (name, status, config) VALUES ($1, 'starting', $2)",
                    self.agent_name, json.dumps(safe_cfg),
                )
            else:
                await conn.execute(
                    "UPDATE agents SET status='starting', updated_at=NOW() WHERE name=$1",
                    self.agent_name,
                )

    # ══════════════════════════════════════════════════════════════════════════
    #  Load DB Config (system_prompt, modules, character from agents.config)
    # ══════════════════════════════════════════════════════════════════════════

    async def _load_db_config(self):
        """Load config from agents table and merge into self.config / system_prompt."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT config FROM agents WHERE name=$1", self.agent_name
            )
        if not row or not row["config"]:
            return

        db_cfg = row["config"]
        if isinstance(db_cfg, str):
            db_cfg = json.loads(db_cfg)
        if not isinstance(db_cfg, dict):
            return

        # Merge ALL DB config into self.config (DB is source of truth)
        for key, val in db_cfg.items():
            if key == "system_prompt":
                continue  # handled separately below
            if key == "character":
                continue  # handled by system_prompt builder
            self.config[key] = val

        # System prompt from DB overrides the default (if non-empty)
        db_prompt = db_cfg.get("system_prompt", "").strip()
        if db_prompt:
            self._system_prompt = db_prompt
            self.logger.info(f"[{self.agent_name}] System prompt loaded from DB ({len(db_prompt)} chars)")

        # Character block — build from character dict if no explicit system_prompt
        if not db_prompt:
            char = db_cfg.get("character", {})
            if char and isinstance(char, dict):
                char_lines = "\n".join(f"- {k}: {v}" for k, v in char.items() if v)
                if char_lines:
                    self._system_prompt = (
                        f"You are {db_cfg.get('display_name', self.agent_name)}, an AIMOS agent.\n\n"
                        f"Character:\n{char_lines}\n\n"
                        "Use your tools actively."
                    )

    # ══════════════════════════════════════════════════════════════════════════
    #  Secret Key-Inheritance:  Agent DB > global_settings DB > .env
    # ══════════════════════════════════════════════════════════════════════════

    async def _load_secrets(self):
        """Load secrets with inheritance: agent env_secrets > global_settings > .env."""
        async with self._pool.acquire() as conn:
            # Layer 1: global_settings
            rows = await conn.fetch(
                "SELECT key, value FROM global_settings WHERE key LIKE 'secret.%'"
            )
            for row in rows:
                env_key = row["key"].replace("secret.", "", 1).upper()
                val = row["value"]
                if isinstance(val, str):
                    os.environ.setdefault(env_key, val)
                elif isinstance(val, dict) and "value" in val:
                    os.environ.setdefault(env_key, str(val["value"]))

            # Layer 2: agent-specific env_secrets (overrides global)
            agent_secrets = await conn.fetchval(
                "SELECT env_secrets FROM agents WHERE name=$1", self.agent_name
            )
            if agent_secrets and isinstance(agent_secrets, dict):
                for k, v in agent_secrets.items():
                    if k and v and isinstance(k, str) and isinstance(v, str):
                        os.environ[k] = v
                self.logger.info(
                    f"[{self.agent_name}] Loaded {len(agent_secrets)} agent secrets "
                    f"(keys: {list(SecretFilter.redact(agent_secrets).keys())})"
                )

    # ══════════════════════════════════════════════════════════════════════════
    #  Audit Logging
    # ══════════════════════════════════════════════════════════════════════════

    def _init_audit_log(self):
        """Set up the audit log file at storage/agents/{agent_name}/api_audit.log."""
        from core.skills.base import BaseSkill
        base = BaseSkill.workspace_path(self.agent_name)  # also creates /public
        self._audit_path = base / "api_audit.log"

    def _init_memory_db(self):
        """Initialize per-agent SQLite memory DB at storage/agents/{name}/memory.db.

        Tables:
          memories    — tiered long-term memory with relevance scoring
          skill_state — per-skill persistent state (keyed by skill_name + key)
          agent_log   — private log entries (not shared with other agents)

        See docs/MEMORY_ARCHITECTURE.md for design rationale.
        """
        import sqlite3
        from core.skills.base import BaseSkill
        db_path = BaseSkill.memory_db_path(self.agent_name)
        self._memory_db_path = db_path
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")

            # CR-188: SQLite integrity check on startup
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                if result[0] != "ok":
                    self.logger.error(f"[{self.agent_name}] SQLite integrity check FAILED: {result[0]}")
            except Exception:
                pass

            # Tiered memory table (CR-081)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    key           TEXT UNIQUE NOT NULL,
                    value         TEXT NOT NULL,
                    category      TEXT DEFAULT 'semantic',
                    importance    INTEGER DEFAULT 5,
                    access_count  INTEGER DEFAULT 0,
                    last_accessed TEXT,
                    source        TEXT DEFAULT 'user',
                    created_at    TEXT DEFAULT (datetime('now')),
                    updated_at    TEXT DEFAULT (datetime('now')),
                    embedding     BLOB
                )
            """)

            # CR-140: Add embedding column if missing (migration for existing DBs)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
            if "embedding" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
                self.logger.info(f"[{self.agent_name}] Added embedding column to memories")

            # CR-140: FTS5 full-text index on key + value
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(key, value, content='memories', content_rowid='id')
            """)
            fts_count = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
            mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if mem_count > 0 and fts_count == 0:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                self.logger.info(f"[{self.agent_name}] Built FTS5 index for {mem_count} memories")

            # Migrate old kv_store → memories (one-time)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            if "kv_store" in tables:
                existing = conn.execute("SELECT key, value, updated_at FROM kv_store").fetchall()
                for key, value, updated_at in existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO memories (key, value, category, importance, source, created_at, updated_at) "
                        "VALUES (?, ?, 'semantic', 5, 'user', ?, ?)",
                        (key, value, updated_at, updated_at),
                    )
                conn.execute("DROP TABLE kv_store")
                if existing:
                    self.logger.info(f"[{self.agent_name}] Migrated {len(existing)} kv_store entries → memories")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_state (
                    skill_name TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (skill_name, key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    level      TEXT NOT NULL,
                    message    TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()

            # CR-140: Backfill embeddings for existing memories (one-time, ~500ms for 50 memories)
            from core.embeddings import is_available as _emb_avail, embed as _embed_text
            if _emb_avail():
                nulls = conn.execute("SELECT id, key, value FROM memories WHERE embedding IS NULL").fetchall()
                if nulls:
                    for mid, mkey, mvalue in nulls:
                        emb = _embed_text(f"{mkey} {mvalue}")
                        if emb:
                            conn.execute("UPDATE memories SET embedding = ? WHERE id = ?", (emb, mid))
                    conn.commit()
                    self.logger.info(f"[{self.agent_name}] Backfilled embeddings for {len(nulls)} memories")

            conn.close()
            self.logger.info(f"[{self.agent_name}] Memory DB ready: {db_path}")
        except Exception as exc:
            self.logger.error(f"[{self.agent_name}] Memory DB init failed: {exc}")

    def _audit(self, event: str, detail: str = ""):
        """Append a timestamped line to the audit log."""
        if not self._audit_path:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{self.agent_name}] {event}"
        if detail:
            line += f" | {detail[:500]}"
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  Chat History
    # ══════════════════════════════════════════════════════════════════════════

    async def _restore_history(self):
        limit = self.config.get("history_limit", Config.HISTORY_LIMIT)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM aimos_chat_histories "
                "WHERE agent_name=$1 ORDER BY id DESC LIMIT $2",
                self.agent_name, limit,
            )
        self._history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # CR-115: Connector families — channels that belong to the same conversation context.
    # "internal" and "scheduled_job" are always included (agent relay + automated tasks).
    # Each connector family groups its variants (e.g. telegram + telegram_voice + telegram_doc).
    # New connectors just need to be added here as a set — no other code changes needed.
    _ALWAYS_VISIBLE = {"internal", "scheduled_job"}
    _CONNECTOR_FAMILIES = [
        {"telegram", "telegram_voice", "telegram_doc"},
        {"email"},
        {"voice_local"},
        # Future connectors: add a new set here, e.g. {"whatsapp", "whatsapp_voice"}
    ]

    def _filter_history_for_channel(self, current_message: str) -> list[dict]:
        """CR-115: Filter chat history to the current conversation context.

        Includes:
        - Messages from the same connector family as the current channel
        - Internal messages (agent-to-agent relay) — always visible
        - Scheduled jobs — always visible (automated follow-ups)

        Excludes:
        - Messages from other connector families (e.g. email while on Telegram)

        This is connector-agnostic: new connectors just need an entry in _CONNECTOR_FAMILIES.
        """
        import re as _re
        m = re.search(r"\[Kontext:.*?channel=(\w+)", current_message)
        if not m:
            return self._history

        current_channel = m.group(1)

        # Find which family the current channel belongs to
        allowed = set(self._ALWAYS_VISIBLE)
        for family in self._CONNECTOR_FAMILIES:
            if current_channel in family:
                allowed |= family
                break
        else:
            # Unknown channel — include it by name + always-visible
            allowed.add(current_channel)

        # Filter history: include user messages from allowed channels
        # plus their following tool/assistant responses
        filtered = []
        include_following = False
        for entry in self._history:
            role = entry.get("role", "")
            content = entry.get("content", "")

            if role == "user":
                ch_match = re.search(r"\[Kontext:.*?channel=(\w+)", content)
                entry_channel = ch_match.group(1) if ch_match else "unknown"
                if entry_channel in allowed:
                    filtered.append(entry)
                    include_following = True
                else:
                    include_following = False
            elif include_following:
                filtered.append(entry)

        if len(filtered) < 2:
            return self._history

        return filtered

    async def _persist_message(self, role: str, content: str, metadata: dict | None = None):
        # CR-122: Strip null bytes — binary content (.docx etc.) crashes PostgreSQL UTF-8
        content = content.replace('\x00', '') if content else content
        self._history.append({"role": role, "content": content})
        if self._pool and not self._pool._closed:
            try:
                # CR-209: Include session_id for multi-user isolation
                # CR-thread: Include thread_id for conversation threading
                session_id = getattr(self, '_current_session_id', None) or ''
                thread_id = getattr(self, '_current_thread_id', None) or ''
                await asyncio.wait_for(self._pool.execute(
                    "INSERT INTO aimos_chat_histories (agent_name, role, content, metadata, session_id, thread_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    self.agent_name, role, content, json.dumps(metadata or {}), session_id, thread_id,
                ), timeout=10)
            except asyncio.TimeoutError:
                self.logger.warning(f"[{self.agent_name}] _persist_message timed out (10s) — skipping DB write")
            except Exception as exc:
                self.logger.warning(f"[{self.agent_name}] _persist_message DB error: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Tool Registry + Execution  (Bug #14 Fix)
    # ══════════════════════════════════════════════════════════════════════════

    def register_tool(self, name: str, func: Callable, description: str = "",
                       parameters: dict | None = None):
        """Register a tool. Parameters dict maps param names to their types/descriptions.
        If not provided, parameters are introspected from the function signature."""
        self._tools[name] = func
        if not hasattr(self, "_tool_meta"):
            self._tool_meta = {}
        self._tool_meta[name] = {
            "description": description or (func.__doc__ or "").strip(),
            "parameters": parameters,  # None = introspect from signature
        }
        self.logger.debug(f"Tool registered: {name}")

    def _build_tool_block(self) -> str:
        """Legacy: text-based tool block for system prompt. Still used as documentation
        for the LLM, but actual tool-calling is done via Ollama's native API."""
        if not self._tools:
            return ""
        lines = ["Available tools (called via function calling):"]
        for name, func in self._tools.items():
            doc = "no description"
            if hasattr(func, "__doc__") and func.__doc__:
                doc = func.__doc__.strip()
            elif hasattr(func, "__code__"):
                doc = f"function at {func.__code__.co_filename}:{func.__code__.co_firstlineno}"
            lines.append(f"  - {name}: {doc}")
        return "\n".join(lines)

    def _build_ollama_tools(self) -> list[dict]:
        """Build Ollama-native tool definitions for the API request.
        CR-114: Uses structured tool calling instead of text-based parsing.

        Parameter sources (priority order):
        1. Explicit parameters from register_tool() or Skill.get_tools()
        2. Introspected from Python function signature
        """
        if not self._tools:
            return []
        import inspect
        meta = getattr(self, "_tool_meta", {})
        tools = []
        for name, func in self._tools.items():
            tm = meta.get(name, {})
            doc = tm.get("description") or ""
            if not doc and hasattr(func, "__doc__") and func.__doc__:
                doc = func.__doc__.strip()

            # Use explicit parameters if provided (from Skill.get_tools())
            explicit_params = tm.get("parameters")
            props = {}
            required = []
            if explicit_params:
                for pname, pinfo in explicit_params.items():
                    if isinstance(pinfo, dict):
                        props[pname] = {
                            "type": pinfo.get("type", "string"),
                            "description": pinfo.get("description", pname),
                        }
                        if pinfo.get("required", False):
                            required.append(pname)
                    else:
                        props[pname] = {"type": "string", "description": pname}
            else:
                # Fallback: introspect from function signature
                try:
                    sig = inspect.signature(func)
                    for pname, p in sig.parameters.items():
                        if pname in ("self", "kwargs"):
                            continue
                        ptype = "string"
                        if p.annotation == int:
                            ptype = "integer"
                        elif p.annotation == float:
                            ptype = "number"
                        elif p.annotation == bool:
                            ptype = "boolean"
                        props[pname] = {"type": ptype, "description": pname}
                        if p.default is inspect.Parameter.empty:
                            required.append(pname)
                except (ValueError, TypeError):
                    pass

            tool_def = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc[:500] if doc else name,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            }
            tools.append(tool_def)
        return tools

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a tool safely. Bug #14: guard introspection with hasattr(__code__)."""
        name = tool_call.get("name", "")
        args = tool_call.get("arguments", {})

        # CR-156: Tool-call budget per message
        if hasattr(self, '_tool_call_count') and hasattr(self, '_tool_call_budget'):
            self._tool_call_count += 1
            if self._tool_call_count > self._tool_call_budget:
                self.logger.warning(f"[{self.agent_name}] Tool-call budget exceeded ({self._tool_call_budget})")
                return json.dumps({"error": f"Tool-call budget exceeded ({self._tool_call_budget} calls per message). Please provide a direct answer."})

        if name not in self._tools:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # CR-142: Execution Ring policy check
        ring_required = _TOOL_RINGS.get(name, 1)  # unknown tools default to Ring 1
        agent_ring = self.config.get("max_ring", 2)  # default Ring 2 (backward-compat)
        if ring_required > agent_ring:
            ring_names = {0: "Read Only", 1: "Standard", 2: "Full Access"}
            msg = (f"Blocked: tool '{name}' requires {ring_names.get(ring_required, ring_required)} "
                   f"(ring {ring_required}), agent has {ring_names.get(agent_ring, agent_ring)} "
                   f"(ring {agent_ring})")
            self.logger.warning(f"[{self.agent_name}] {msg}")
            self._audit("TOOL_BLOCKED", f"{name} ring={ring_required} > agent_ring={agent_ring}")
            return json.dumps({"error": msg})

        func = self._tools[name]
        self._audit("TOOL_START", f"{name}({json.dumps(args, ensure_ascii=False)[:200]})")
        # Track if agent sends to Telegram directly (prevents double-send in dispatch_response)
        if name in ("send_telegram_message", "send_voice_message"):
            self._telegram_sent_this_cycle = True
        # CR-thread: Track if agent sent an email this cycle (for auto-notify)
        if name == "send_email":
            self._email_sent_this_cycle = True

        try:
            # Bug #14: never access __code__ without hasattr guard
            if hasattr(func, "__code__"):
                is_coro = asyncio.iscoroutinefunction(func)
            elif hasattr(func, "__call__"):
                is_coro = asyncio.iscoroutinefunction(func.__call__)
            else:
                is_coro = False

            # 30s timeout on all tool calls (prevents Brave/API hangs)
            if is_coro:
                result = await asyncio.wait_for(func(**args), timeout=30)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, **args), timeout=30
                )

            out = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            self._audit("TOOL_OK", f"{name} → {len(out)} chars")
            return out

        except asyncio.TimeoutError:
            self.logger.warning(f"Tool '{name}' timed out after 30s")
            self._audit("TOOL_TIMEOUT", name)
            return json.dumps({"error": f"Tool '{name}' timed out after 30s"})

        except Exception as exc:
            self.logger.error(f"Tool '{name}' failed: {exc}")
            self.logger.debug(traceback.format_exc())
            self._audit("TOOL_ERROR", f"{name}: {exc}")
            return json.dumps({"error": str(exc)})

    # ══════════════════════════════════════════════════════════════════════════
    #  4-Strategy Tool-Call Parser
    # ══════════════════════════════════════════════════════════════════════════

    def _parse_tool_calls(self, text: str) -> list[dict]:
        """Parse tool calls from LLM output. Tries 4 strategies in order."""
        calls: list[dict] = []

        # Strategy 1: <tool_call>{JSON}</tool_call>
        for m in _TC_XML.finditer(text):
            try:
                calls.append(json.loads(_repair_json(m.group(1))))  # CR-170
            except json.JSONDecodeError:
                pass
        if calls:
            return calls

        # Strategy 2: raw JSON {"name": "...", "arguments": {...}}
        m = _TC_JSON.search(text)
        if m:
            try:
                args = json.loads(_repair_json(m.group(2)))  # CR-170
            except json.JSONDecodeError:
                args = {}
            return [{"name": m.group(1), "arguments": args}]

        # Strategy 3: Python-style — tool_name(key=val, ...)
        for name in self._tools:
            pat = re.compile(rf"\b{re.escape(name)}\s*\(([^)]*)\)", re.IGNORECASE)
            pm = pat.search(text)
            if pm:
                raw_args = pm.group(1).strip()
                return [{"name": name, "arguments": self._parse_kwargs(raw_args) if raw_args else {}}]

        # Strategy 4: bare name — (tool_name) or tool_name()
        for name in self._tools:
            pat = re.compile(
                rf"(?:\(({re.escape(name)})\)|({re.escape(name)})\(\))", re.IGNORECASE
            )
            pm = pat.search(text)
            if pm:
                return [{"name": pm.group(1) or pm.group(2), "arguments": {}}]

        return []

    @staticmethod
    def _parse_kwargs(raw: str) -> dict:
        """Parse 'key=val, key2=val2' into a dict."""
        result = {}
        for part in raw.split(","):
            part = part.strip()
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            key, val = key.strip(), val.strip()
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                result[key] = val[1:-1]
            elif val.lower() == "true":
                result[key] = True
            elif val.lower() == "false":
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val
        return result

    # ══════════════════════════════════════════════════════════════════════════
    #  LLM Interaction
    # ══════════════════════════════════════════════════════════════════════════

    async def _llm_chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Call Ollama and return the full message dict (content + optional tool_calls).

        CR-114: Uses Ollama's native tool-calling API. Returns:
          {"content": "text", "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
        """
        # Heartbeat before LLM call (prevents orchestrator from killing during inference)
        if self._pool and not self._pool._closed:
            try:
                await self._pool.execute(
                    "UPDATE agents SET updated_at=NOW() WHERE name=$1", self.agent_name
                )
            except Exception:
                pass
        num_ctx = self._vram_guard(self.config.get("num_ctx", Config.DEFAULT_NUM_CTX))

        # CR-127 + Dynamic Context Balancing: num_predict adapts to actual context usage
        # Cognitive balance sets the MINIMUM output guarantee and memory limit,
        # but if context is underused, output gets all remaining tokens.
        cb = self.config.get("cognitive_balance", 0)
        min_predict = _CB_NUM_PREDICT.get(cb, 512)  # Guaranteed minimum output
        max_predict = 4096  # Hard cap — no single answer should exceed this
        safety_margin = 500  # For tool call overhead
        max_content_tokens = num_ctx - min_predict - safety_margin

        total_chars = sum(len(m.get("content", "")) for m in messages)
        est_tokens = total_chars // 4

        if est_tokens > max_content_tokens:
            # Trim from the middle (keep system prompt + last 3 messages)
            while est_tokens > max_content_tokens and len(messages) > 4:
                removed = messages.pop(1)
                total_chars = sum(len(m.get("content", "")) for m in messages)
                est_tokens = total_chars // 4
            self.logger.warning(
                f"[CR-127] Context trimmed: {est_tokens} tokens "
                f"(max {max_content_tokens}, min_predict={min_predict}), "
                f"{len(messages)} messages remaining"
            )

        # HARD SAFETY: if STILL over budget after trimming, truncate long messages
        if est_tokens > max_content_tokens and len(messages) > 1:
            for i in range(1, len(messages)):
                content = messages[i].get("content", "")
                if len(content) > 1000:
                    messages[i]["content"] = content[:800] + "\n[... truncated for context budget]"
            total_chars = sum(len(m.get("content", "")) for m in messages)
            est_tokens = total_chars // 4
            self.logger.warning(f"[CR-127] Hard truncation: {est_tokens} tokens, {len(messages)} msgs")
        # Dynamic num_predict: use all remaining context for output
        total_chars = sum(len(m.get("content", "")) for m in messages)
        est_input_tokens = total_chars // 4
        remaining = num_ctx - est_input_tokens - safety_margin
        num_predict = max(min_predict, min(remaining, max_predict))
        if num_predict > min_predict * 1.5:
            self.logger.info(
                f"[{self.agent_name}] Dynamic output: {num_predict} tokens "
                f"(input={est_input_tokens}, remaining={remaining}, min={min_predict})"
            )
        payload = {
            "model": self.config.get("model", Config.LLM_MODEL),
            "messages": messages,
            "stream": False,
            "keep_alive": Config.LLM_KEEP_ALIVE,
            "options": {
                "temperature": self.config.get("temperature", Config.TEMPERATURE),
                "num_ctx": num_ctx,
                "num_predict": num_predict,
                "num_gpu": -1,  # Force all layers to GPU (prevents CPU fallback)
            },
            "stop": _CHINESE_STOP_TOKENS,
        }
        if tools:
            payload["tools"] = tools
        # CR-160: Dynamic VRAM check before inference
        try:
            import subprocess as _sp
            # CR-160: Check if Ollama has a model loaded. If yes, VRAM is expected to be low — that's fine.
            # Only block if VRAM is low AND no model is loaded (meaning something else ate the VRAM).
            _nvidia = _sp.run(
                ["nvidia-smi", "--query-gpu=memory.free,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3
            )
            if _nvidia.returncode == 0:
                parts = _nvidia.stdout.strip().split(',')
                free_mb = int(parts[0].strip())
                used_mb = int(parts[1].strip())
                # If >10 GB is used, Ollama likely has the model loaded — proceed normally
                # Only block if <500 MB free AND <5 GB used (no model loaded, something else consumed VRAM)
                if free_mb < 500 and used_mb < 5000:
                    self.logger.warning(f"[{self.agent_name}] VRAM critically low: {free_mb}MB free, {used_mb}MB used (no model loaded). Skipping inference.")
                    return "Error: GPU memory critically low and no LLM model loaded. Check Ollama status."
        except Exception:
            pass  # nvidia-smi unavailable, proceed anyway

        timeout = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)
        _actual_model = self.config.get("model", Config.LLM_MODEL)
        self._audit("LLM_CALL", f"model={_actual_model} msgs={len(messages)}")

        _RETRY_CODES = {500, 502, 503, 529}
        _MAX_RETRIES = 2

        async with httpx.AsyncClient(timeout=timeout) as client:
          for _attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(Config.ollama_url(), json=payload)
                if resp.status_code in _RETRY_CODES and _attempt < _MAX_RETRIES:
                    wait = 3 * (_attempt + 1)
                    self.logger.warning(
                        f"[{self.agent_name}] Ollama HTTP {resp.status_code} — "
                        f"retry {_attempt+1}/{_MAX_RETRIES} in {wait}s"
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                msg = data.get("message", {})

                # CR-141: Token tracking
                in_tokens = data.get("prompt_eval_count", 0)
                out_tokens = data.get("eval_count", 0)
                prompt_ms = round(data.get("prompt_eval_duration", 0) / 1e6)
                eval_ms = round(data.get("eval_duration", 0) / 1e6)
                total_tokens = in_tokens + out_tokens
                self._audit(
                    "LLM_USAGE",
                    f"in={in_tokens} out={out_tokens} total={total_tokens} "
                    f"prompt_ms={prompt_ms} eval_ms={eval_ms} "
                    f"ctx={num_ctx} model={_actual_model}"
                )
                self.logger.info(
                    f"[{self.agent_name}] LLM: {in_tokens}→{out_tokens} tokens "
                    f"({prompt_ms}+{eval_ms}ms) ctx={num_ctx}"
                )

                # CR-172: Token-level budgeting — warn when context utilization is high
                if in_tokens > 0 and num_ctx > 0:
                    utilization = in_tokens / num_ctx * 100
                    if utilization > 90:
                        self.logger.warning(
                            f"[{self.agent_name}] Context utilization {utilization:.0f}% "
                            f"({in_tokens}/{num_ctx} tokens)"
                        )

                return {
                    "content": msg.get("content", ""),
                    "tool_calls": msg.get("tool_calls", []),
                }
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRY_CODES and _attempt < _MAX_RETRIES:
                    wait = 3 * (_attempt + 1)
                    self.logger.warning(f"[{self.agent_name}] Ollama HTTP {exc.response.status_code} — retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                self.logger.error(f"Ollama HTTP {exc.response.status_code} (after {_attempt+1} attempts)")
                self._audit("LLM_ERROR", f"HTTP {exc.response.status_code}")
                return {"content": f"[LLM Error: HTTP {exc.response.status_code}]", "tool_calls": []}
            except httpx.RequestError as exc:
                if _attempt < _MAX_RETRIES:
                    wait = 3 * (_attempt + 1)
                    self.logger.warning(f"[{self.agent_name}] Ollama connection error — retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                self.logger.error(f"Ollama request failed (after {_attempt+1} attempts): {exc}")
                self._audit("LLM_ERROR", str(exc))
                return {"content": "[LLM Error: Connection failed]", "tool_calls": []}

    async def _flush_gpu_cache(self):
        """Release GPU memory on agent shutdown.

        Called once during stop(), not after every LLM call.
        Between calls, Ollama keeps the model loaded (LLM_KEEP_ALIVE=30m).
        Strategy: torch.cuda.empty_cache() if available, else Ollama keep_alive=0.
        """
        # Try torch first (if installed in venv)
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                self.logger.debug("GPU cache flushed via torch.cuda.empty_cache()")
                return
        except ImportError:
            pass

        # Fallback: tell Ollama to release the model from VRAM briefly
        try:
            url = f"{Config.LLM_BASE_URL.rstrip('/')}/api/chat"
            payload = {"model": self.config.get("model", Config.LLM_MODEL), "messages": [], "keep_alive": 0}
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(url, json=payload)
            self.logger.debug("GPU cache flushed via Ollama keep_alive=0")
        except Exception:
            pass  # best-effort — never block on flush failure

    def _vram_guard(self, num_ctx: int) -> int:
        """Cap num_ctx if it would exceed ~95% of available VRAM budget.

        Estimates: model ~10 GB + KV-cache ~0.5 MB per 1K tokens.
        Returns (possibly capped) num_ctx. Graceful: no cap if nvidia-smi unavailable.
        """
        if not hasattr(self, "_vram_total_mb"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                    timeout=5, text=True,
                )
                self._vram_total_mb = int(out.strip().split("\n")[0])
            except Exception:
                self._vram_total_mb = 0  # unknown — no cap
        if self._vram_total_mb <= 0:
            return num_ctx
        # Dynamic model size estimate based on configured model name
        _model_name = self.config.get("model", Config.LLM_MODEL).lower()
        if "27b" in _model_name or "32b" in _model_name:
            model_mb = 17_000  # ~17 GB for 27b/32b Q4_K_M
        elif "14b" in _model_name:
            model_mb = 9_000   # ~9 GB for 14b Q4_K_M
        elif "7b" in _model_name or "8b" in _model_name:
            model_mb = 5_000   # ~5 GB for 7b/8b Q4_K_M
        else:
            model_mb = 9_000   # Safe default for unknown models
        budget_mb = self._vram_total_mb * 0.95 - model_mb
        if budget_mb <= 0:
            return num_ctx
        max_ctx = int(budget_mb / 0.5 * 1000)  # 0.5 MB per 1K tokens
        if num_ctx > max_ctx:
            self.logger.warning(
                f"VRAM guard: num_ctx {num_ctx} exceeds budget ({self._vram_total_mb}MB GPU), "
                f"capping to {max_ctx}"
            )
            return max_ctx
        return num_ctx

    # ══════════════════════════════════════════════════════════════════════════
    #  Core System Prompt (loaded from core/prompts/core_system.txt at import)
    # ══════════════════════════════════════════════════════════════════════════

    _CORE_PROMPT_PATH = Path(__file__).parent / "prompts" / "core_system.txt"
    try:
        _CORE_SYSTEM_PROMPT = _CORE_PROMPT_PATH.read_text(encoding="utf-8").strip() + "\n\n"
    except FileNotFoundError:
        _CORE_SYSTEM_PROMPT = ""

    # ══════════════════════════════════════════════════════════════════════════
    #  think() — Main reasoning loop with Output-Firewall
    # ══════════════════════════════════════════════════════════════════════════

    async def _load_active_chats(self) -> str:
        """CR-119: Build a context block showing all active conversations.

        Gives the agent awareness of who they're talking to on which channels,
        including chat_ids for proactive messaging (send_telegram_message).
        """
        if not self._pool or self._pool._closed:
            return ""
        try:
            async with self._pool.acquire(timeout=5) as conn:  # CR-138: pool acquire timeout
                # Recent Telegram conversations (unique chat_ids with last message text)
                tg_rows = await conn.fetch(
                    "SELECT DISTINCT ON (sender_id) sender_id, kind, LEFT(content, 60) as last_msg, created_at "
                    "FROM pending_messages "
                    "WHERE agent_name=$1 AND kind IN ('telegram','telegram_voice','telegram_doc') "
                    "AND sender_id IS NOT NULL AND sender_id != 0 "
                    "ORDER BY sender_id, id DESC",
                    self.agent_name,
                )
                # Recent internal agent conversations
                int_rows = await conn.fetch(
                    "SELECT DISTINCT ON (content) LEFT(content, 30) as sender, created_at "
                    "FROM pending_messages "
                    "WHERE agent_name=$1 AND kind='internal' AND processed=TRUE "
                    "AND created_at > NOW() - INTERVAL '2 hours' "
                    "ORDER BY content, id DESC LIMIT 5",
                    self.agent_name,
                )
                # Pending scheduled jobs
                job_rows = await conn.fetch(
                    "SELECT task_prompt, scheduled_time FROM agent_jobs "
                    "WHERE agent_name=$1 AND status='pending' ORDER BY scheduled_time LIMIT 3",
                    self.agent_name,
                )

            lines = []
            if tg_rows:
                lines.append("Active Telegram chats (use send_telegram_message with these chat_ids):")
                for r in tg_rows:
                    ts = r["created_at"].strftime("%H:%M") if r["created_at"] else "?"
                    last_msg = r.get("last_msg", "")[:50] or "?"
                    lines.append(f"  - chat_id={r['sender_id']} (last msg at {ts}: \"{last_msg}\")")
            if int_rows:
                lines.append("Recent agent conversations:")
                for r in int_rows:
                    import re
                    m = re.search(r"\[Nachricht von (\w+)\]", r["sender"] or "")
                    name = m.group(1) if m else "?"
                    lines.append(f"  - Agent: {name}")
            if job_rows:
                lines.append("Pending reminders:")
                for r in job_rows:
                    ts = r["scheduled_time"].strftime("%H:%M") if r["scheduled_time"] else "?"
                    lines.append(f"  - {ts}: {str(r['task_prompt'])[:80]}")

            if not lines:
                return ""
            return "\n\n<active_conversations>\n" + "\n".join(lines) + "\n</active_conversations>"
        except Exception as exc:
            self.logger.debug(f"Active chats load failed: {exc}")
            return ""

    def _load_memory_context(self) -> str:
        """Load top-scored memories into a context block for the system prompt.

        Scoring: importance * recency_weight * (1 + ln(access_count + 1))
        See docs/MEMORY_ARCHITECTURE.md for full formula.
        """
        if not self._memory_db_path or not self._memory_db_path.exists():
            return ""
        import math
        import sqlite3
        try:
            conn = sqlite3.connect(str(self._memory_db_path), timeout=3)
            rows = conn.execute(
                "SELECT key, value, category, importance, access_count, last_accessed "
                "FROM memories ORDER BY importance DESC LIMIT 80"
            ).fetchall()
            conn.close()
            if not rows:
                return ""

            now = datetime.now(timezone.utc)
            scored = []
            for key, value, category, importance, access_count, last_accessed in rows:
                # Recency weight: 1.0 for today, decays with 0.1/day
                days_ago = 0.0
                if last_accessed:
                    try:
                        la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
                        if la.tzinfo is None:
                            la = la.replace(tzinfo=timezone.utc)
                        days_ago = max(0, (now - la).total_seconds() / 86400)
                    except (ValueError, TypeError):
                        pass
                recency = 1.0 / (1.0 + days_ago * 0.1)
                freq_boost = 1.0 + math.log(max(1, access_count or 0) + 1)

                # CR-210: Session-aware boost — current customer's facts score higher
                session_boost = 1.0
                session_id = getattr(self, '_current_session_id', '')
                if session_id:
                    # Extract identifier (e.g. "123456789" from "telegram:123456789")
                    sid_parts = session_id.split(":", 1)
                    sid_val = sid_parts[1] if len(sid_parts) > 1 else session_id
                    key_lower = (key or "").lower()
                    val_lower = (value or "").lower()
                    if sid_val in key_lower or sid_val in val_lower:
                        session_boost = 3.0  # Current customer's facts boosted
                    elif category == "episodic" and "customer" in key_lower:
                        session_boost = 0.3  # Other customer's episodic facts demoted

                score = (importance or 5) * recency * freq_boost * session_boost
                scored.append((score, category or "semantic", key, value, importance or 5))

            scored.sort(key=lambda x: x[0], reverse=True)
            cb = self.config.get("cognitive_balance", 0)
            mem_limit = _CB_MEMORY_LIMITS.get(cb, 50)
            top = scored[:mem_limit]

            lines = []
            for score, cat, key, value, imp in top:
                lines.append(f"- [{cat}] {key}: {value}")

            return (
                "\n\n<langzeitgedaechtnis>\n"
                "Die folgenden Fakten sind in deinem Langzeitgedaechtnis gespeichert. "
                "Nutze sie aktiv in deinen Antworten.\n"
                + "\n".join(lines)
                + "\n</langzeitgedaechtnis>"
            )
        except Exception as exc:
            self.logger.debug(f"Memory context load failed: {exc}")
            return ""

    async def think(self, user_message: str) -> str:
        """Full loop: user msg → LLM → tool calls → clean → answer.

        CR-114: Uses Ollama native tool-calling API. Falls back to text-based
        parsing if the model doesn't return structured tool_calls.
        """
        self._touch()
        await self._persist_message("user", user_message)

        tool_block = self._build_tool_block()
        ollama_tools = self._build_ollama_tools()
        memory_block = self._load_memory_context()
        chats_block = await self._load_active_chats()
        # CR-144: Inject calendar events (overdue, today, upcoming)
        calendar_block = ""
        try:
            from core.skills.skill_calendar import get_calendar_context
            calendar_block = get_calendar_context(self.agent_name)
        except Exception:
            pass
        # CR-152: Inject project tasks (overdue, blocked, upcoming)
        project_block = ""
        try:
            from core.skills.skill_project_management import get_project_context
            project_block = get_project_context(self.agent_name)
        except Exception:
            pass
        # Build system prompt: Core → User prompt → Memory → Calendar → Projects → Active Chats → Tools
        system = self._CORE_SYSTEM_PROMPT + self._system_prompt + memory_block + calendar_block + project_block + chats_block
        if tool_block:
            system += "\n\n" + tool_block

        # CR-115: Filter history to current conversation thread (Telegram/internal/scheduled)
        thread_history = self._filter_history_for_channel(user_message)

        # CR-thread: Thread-based history isolation — load from DB if thread_id set
        # Falls back to session_id-based isolation if no thread_id
        thread_id = getattr(self, '_current_thread_id', '')
        session_id = getattr(self, '_current_session_id', '')
        if (thread_id or session_id) and self._pool:
            try:
                limit = self.config.get("history_limit", Config.HISTORY_LIMIT)
                if thread_id:
                    rows = await self._pool.fetch(
                        "SELECT role, content FROM aimos_chat_histories "
                        "WHERE agent_name=$1 AND (thread_id=$2 OR (thread_id IS NULL AND created_at > NOW() - INTERVAL '24 hours') OR (thread_id='' AND created_at > NOW() - INTERVAL '24 hours')) "
                        "ORDER BY id DESC LIMIT $3",
                        self.agent_name, thread_id, limit,
                    )
                else:
                    rows = await self._pool.fetch(
                        "SELECT role, content FROM aimos_chat_histories "
                        "WHERE agent_name=$1 AND (session_id=$2 OR session_id IS NULL OR session_id='') "
                        "ORDER BY id DESC LIMIT $3",
                        self.agent_name, session_id, limit,
                    )
                thread_history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
            except Exception:
                pass  # Fallback to channel-filtered in-memory history

        messages = [{"role": "system", "content": system}] + thread_history
        max_rounds = self.config.get("max_tool_rounds", Config.MAX_TOOL_ROUNDS)

        response_text = ""
        any_tool_called = False
        tool_results_this_cycle = []  # CR-159: collect tool outputs for confidence check
        self._telegram_sent_this_cycle = False  # CR-120: track if agent already sent to Telegram
        self._email_sent_this_cycle = False  # CR-thread: track if agent sent email (for auto-notify)
        for _ in range(max_rounds):
            llm_response = await self._llm_chat(messages, tools=ollama_tools)
            response_text = llm_response.get("content", "")
            native_tool_calls = llm_response.get("tool_calls", [])

            # CR-114: Prefer native tool calls from Ollama API
            tool_calls = []
            if native_tool_calls:
                for tc in native_tool_calls:
                    fn = tc.get("function", {})
                    tool_calls.append({
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", {}),
                    })
            else:
                # Fallback: text-based parsing (for models without native tool support)
                tool_calls = self._parse_tool_calls(response_text)

            if not tool_calls:
                break

            any_tool_called = True
            # Save the text the LLM generated WITH the tool call — this is the
            # customer-facing answer (e.g. "Ich leite das an den Vertrieb weiter")
            pre_tool_text = response_text

            # Build assistant message with tool calls for the conversation
            messages.append({"role": "assistant", "content": response_text, "tool_calls": native_tool_calls or None})
            terminal_tool_called = False
            for tc in tool_calls:
                result = await self._execute_tool(tc)
                self._touch()  # CR-166: keep watchdog alive during multi-tool chains
                tool_results_this_cycle.append(str(result))  # CR-159
                tool_msg = f"Tool '{tc.get('name')}' returned:\n{result}"
                messages.append({"role": "tool", "content": tool_msg})
                await self._persist_message("tool", tool_msg, {"tool": tc.get("name")})
                # Terminal tools: after sending a message, stop the loop.
                # Use the text from BEFORE the tool call as the final answer.
                # Terminal tools: stop the think loop after sending a message.
                # Exception: send_telegram_message is NOT terminal — the agent may need
                # to call send_to_agent afterwards (helpdesk confirms to operator, then delegates).
                if tc.get("name") in ("send_to_agent", "send_email"):
                    terminal_tool_called = True
            if terminal_tool_called:
                if pre_tool_text and pre_tool_text.strip():
                    self.logger.info(f"[{self.agent_name}] Terminal tool called — using pre-tool text as answer")
                    response_text = pre_tool_text
                else:
                    # LLM generated only the tool call with no text — do one more round
                    # to let it generate a customer-facing response
                    self.logger.info(f"[{self.agent_name}] Terminal tool called but no text — one more LLM round")
                    llm_final = await self._llm_chat(messages, tools=None)  # No tools → forces text
                    response_text = llm_final.get("content", "")
                break

        # CR-159: Confidence check (monitoring-only)
        if any_tool_called:
            response_text = self._check_confidence(response_text, tool_results_this_cycle)

        # Output-Firewall: mandatory clean step
        for seq in STOP_SEQUENCES:
            response_text = response_text.replace(seq, "")
        answer = clean_llm_response(response_text, tool_was_called=any_tool_called)

        # CR-114b: Phantom-action detection — strip false claims about actions not taken
        answer = await self._strip_phantom_actions(answer, tool_results_this_cycle)

        # Loop detection: if 2 consecutive responses are >60% similar, escalate
        answer = await self._check_loop_and_escalate(answer, user_message)

        await self._persist_message("assistant", answer)
        return answer

    # CR-114b: Phantom action claims → action keywords the agent might claim without doing
    _PHANTOM_PATTERNS = {
        "send_to_agent": re.compile(
            r'(?:habe ich (?:an|dem|weitergeleitet|kontaktiert|gesendet|informiert)|'
            r'I have (?:forwarded|contacted|sent|informed)|'
            r'(?:leite|sende|kontaktiere) ich (?:das |die |den )?(?:an |weiter)|'
            r'(?:werde|wird) (?:sich |)(?:mein Kollege|der Vertrieb|unser Innendienst)|'
            r'(?:angebot|offer) (?:wird|werde ich|fuer|für|for)[\w\s]*(?:erstellt|vorbereitet|gesendet|created|prepared|sent))',
            re.IGNORECASE),
        "remember": re.compile(
            r'(?:habe ich (?:notiert|gespeichert|gemerkt|vermerkt)|'
            r'I have (?:noted|saved|stored|remembered)|'
            r'(?:Ihre Daten|die Information) (?:wurde|habe ich) (?:gespeichert|notiert))',
            re.IGNORECASE),
        "send_email": re.compile(
            r'(?:habe ich (?:per |eine )?(?:E-?Mail|Mail) (?:gesendet|geschickt|versendet)|'
            r'I have (?:sent|emailed)|'
            r'(?:E-?Mail|Mail) (?:wurde|wird) (?:gesendet|verschickt))',
            re.IGNORECASE),
    }

    async def _strip_phantom_actions(self, answer: str, tool_results: list[str]) -> str:
        """Detect phantom actions and attempt self-correction.

        If the agent claims an action but didn't call the tool:
        1. Try to force the tool call via a corrective think() round
        2. If successful → keep the original answer (claim is now true)
        3. If still not called → strip the false claim sentence

        Zero token overhead for detection. One extra LLM call only when phantom detected.
        """
        if not answer:
            return answer

        # Determine which tools were actually called
        called_tools = set()
        for tr in tool_results:
            for tool_name in ("send_to_agent", "remember", "recall", "send_email",
                              "send_telegram_message", "write_file", "read_file",
                              "search_in_file", "brave_search", "web_search"):
                if f"Tool '{tool_name}'" in tr or f"[Tool: {tool_name}]" in tr:
                    called_tools.add(tool_name)

        # Check each phantom pattern
        for tool_name, pattern in self._PHANTOM_PATTERNS.items():
            if tool_name not in called_tools and pattern.search(answer):
                self.logger.warning(
                    f"[CR-114b] Phantom action detected: '{tool_name}' claimed but not called. "
                    f"Attempting self-correction..."
                )
                self._audit("PHANTOM_ACTION", f"claimed={tool_name} tools_called={called_tools}")

                # Attempt self-correction: ask the LLM to actually do it
                correction_ok = await self._force_phantom_tool(tool_name, answer)

                if correction_ok:
                    self.logger.info(f"[CR-114b] Self-correction succeeded: '{tool_name}' now called")
                    return answer  # Keep original answer — claim is now fulfilled

                # Self-correction failed — strip the false claim
                self.logger.warning(f"[CR-114b] Self-correction failed — stripping claim")
                sentences = re.split(r'(?<=[.!?])\s+', answer)
                cleaned = [s for s in sentences if not pattern.search(s)]
                if len(cleaned) < len(sentences):
                    answer = " ".join(cleaned)

        return answer

    async def _force_phantom_tool(self, tool_name: str, original_answer: str) -> bool:
        """Try to force a missed tool call via a corrective LLM round.

        Returns True if the tool was actually called this time.
        """
        try:
            correction_prompt = (
                f"IMPORTANT: In your previous response you said you would use {tool_name}, "
                f"but you did NOT actually call it. Your response was:\n"
                f'"{original_answer[:300]}"\n\n'
                f"Now actually call {tool_name} with the appropriate parameters. "
                f"Do NOT generate any text — ONLY call the tool."
            )

            # Build minimal messages for correction
            messages = [
                {"role": "system", "content": self._CORE_SYSTEM_PROMPT + self._system_prompt},
                {"role": "user", "content": correction_prompt},
            ]

            ollama_tools = self._build_ollama_tools() if hasattr(self, '_build_ollama_tools') else None
            llm_response = await self._llm_chat(messages, tools=ollama_tools)

            # Check if tool was called
            native_tc = llm_response.get("tool_calls", [])
            tool_calls = []
            if native_tc:
                for tc in native_tc:
                    fn = tc.get("function", {})
                    tool_calls.append({"name": fn.get("name", ""), "arguments": fn.get("arguments", {})})

            if not tool_calls:
                # Also try text-based parsing
                tool_calls = self._parse_tool_calls(llm_response.get("content", ""))

            # Execute if the expected tool was called
            for tc in tool_calls:
                if tc.get("name") == tool_name:
                    result = await self._execute_tool(tc)
                    self.logger.info(f"[CR-114b] Forced {tool_name}: {str(result)[:100]}")
                    tool_msg = f"Tool '{tc.get('name')}' returned:\n{result}"
                    await self._persist_message("tool", tool_msg, {"tool": tc.get("name"), "forced": True})
                    return True

            return False

        except Exception as exc:
            self.logger.debug(f"[CR-114b] Force tool failed: {exc}")
            return False

    def _check_confidence(self, reply: str, tool_results: list[str]) -> str:
        """CR-159: Detect potential hallucination patterns and log warnings.

        Monitoring-only — logs but does not modify the reply.
        """
        # Patterns that suggest the agent is making claims it can't verify
        uncertain_patterns = [
            r'(?:ich glaube|I believe|I think|vermutlich|wahrscheinlich|möglicherweise|possibly|probably|if I recall)',
            r'(?:soweit ich weiß|as far as I know|meines Wissens|to my knowledge)',
            r'(?:das müsste|das sollte|that should be|it should be)',
        ]

        has_uncertainty = any(re.search(p, reply, re.IGNORECASE) for p in uncertain_patterns)

        # Check if agent quotes numbers/dates that weren't in any tool result
        numbers_in_reply = set(re.findall(r'\b\d{3,}\b', reply))
        numbers_in_tools = set()
        for tr in tool_results:
            numbers_in_tools.update(re.findall(r'\b\d{3,}\b', tr))

        unverified_numbers = numbers_in_reply - numbers_in_tools

        if has_uncertainty or len(unverified_numbers) > 3:
            self.logger.info(
                f"[{self.agent_name}] CR-159 confidence check: "
                f"uncertainty={has_uncertainty}, unverified_numbers={len(unverified_numbers)}"
            )

        return reply

    async def _check_loop_and_escalate(self, answer: str, user_message: str) -> str:
        """Detect if the local LLM is stuck in a loop and escalate to external API.

        Compares the current answer against the last 2 responses using word overlap.
        If the current answer is >60% similar to the previous one, escalate immediately.
        """
        # Track recent responses (sliding window of 2)
        self._recent_responses.append(answer[:200])
        if len(self._recent_responses) > 2:
            self._recent_responses.pop(0)

        if len(self._recent_responses) < 2:
            return answer  # need at least one previous response

        # Compute word-overlap similarity against the previous response
        current_words = set(answer.lower().split())
        if not current_words:
            return answer

        prev_words = set(self._recent_responses[0].lower().split())
        if not prev_words:
            return answer

        overlap = len(current_words & prev_words) / max(len(current_words), len(prev_words))
        if overlap <= 0.6:
            return answer  # not a loop

        # Loop detected — escalate to external LLM
        self.logger.warning(
            f"[{self.agent_name}] LOOP DETECTED: {overlap:.0%} overlap with previous response. Escalating to external LLM."
        )
        self._audit("LOOP_ESCALATION", f"user_msg={user_message[:100]}")

        if "ask_external" not in self._tools:
            self.logger.warning(f"[{self.agent_name}] ask_external not available — cannot escalate")
            return answer

        try:
            ext_result = await self._tools["ask_external"](
                question=user_message,
                context=f"Der lokale Agent konnte diese Frage nicht zufriedenstellend beantworten. "
                        f"Letzte Antwort war: {answer[:200]}",
            )
            self._recent_responses.clear()  # reset loop tracker
            self.logger.info(f"[{self.agent_name}] Loop resolved via external LLM ({len(ext_result)} chars)")
            return ext_result
        except Exception as exc:
            self.logger.error(f"[{self.agent_name}] External escalation failed: {exc}")
            return answer

    # ══════════════════════════════════════════════════════════════════════════
    #  Queue Drain (v3.9.0) — process pending_messages before live loop
    # ══════════════════════════════════════════════════════════════════════════

    async def _drain_queue(self):
        """Process pending messages at startup (manual mode only).

        In orchestrator mode, this is a NO-OP — the orchestrator loop in main.py
        handles message processing WITH reply routing (sender_id → Telegram).
        _drain_queue has no reply channel, so it must not steal messages.
        """
        if self.config.get("mode") == "orchestrator":
            # Count pending for logging, but don't touch them
            if self._pool:
                async with self._pool.acquire() as conn:
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM pending_messages WHERE agent_name=$1 AND processed=FALSE",
                        self.agent_name,
                    )
                if count:
                    self.logger.info(f"[{self.agent_name}] {count} pending messages — orchestrator loop will handle them")
            return

        # Manual mode: drain and process directly (no reply routing needed — Telegram polls)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE pending_messages SET processed=TRUE "
                "WHERE agent_name=$1 AND processed=FALSE "
                "RETURNING id, sender_id, content, kind",
                self.agent_name,
            )

        if not rows:
            return

        self.logger.info(f"[{self.agent_name}] Draining {len(rows)} queued messages …")
        for row in rows:
            content = row["content"] or ""
            try:
                await asyncio.wait_for(self.think(content), timeout=_QUEUE_MSG_TIMEOUT)
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"[{self.agent_name}] Queue msg {row['id']} timed out after {_QUEUE_MSG_TIMEOUT}s"
                )
        self.logger.info(f"[{self.agent_name}] Queue drained.")

    # ══════════════════════════════════════════════════════════════════════════
    #  Watchdog (90s inactivity auto-shutdown)
    # ══════════════════════════════════════════════════════════════════════════

    def _touch(self):
        """Update last-activity timestamp."""
        self._last_activity = asyncio.get_event_loop().time()

    async def _watchdog(self):
        """Background task: shut down if idle for >90s without messages.

        Disabled in manual mode — manual agents run until explicitly stopped.
        """
        if self.config.get("mode") == "manual":
            self.logger.debug(f"[{self.agent_name}] Watchdog disabled (manual mode).")
            return  # exit immediately — no auto-shutdown
        if self.config.get("voice_mode") == "hardware" and self.config.get("execution_strategy") == "parallel":
            self.logger.info(f"[{self.agent_name}] Watchdog disabled (live voice agent in parallel mode).")
            return  # live voice agents with dedicated audio I/O stay alive for instant responses
        while True:
            await asyncio.sleep(10)
            if self._pool is None:
                return  # already stopped
            idle = asyncio.get_event_loop().time() - self._last_activity
            if idle > _WATCHDOG_TIMEOUT:
                self.logger.warning(
                    f"[{self.agent_name}] Watchdog: {idle:.0f}s idle — auto-shutdown."
                )
                await self.stop()
                return

    # ══════════════════════════════════════════════════════════════════════════
    #  Main Loop
    # ══════════════════════════════════════════════════════════════════════════

    async def poll_pending(self) -> list[dict]:
        if not self._pool:
            return []
        try:
            async with self._pool.acquire(timeout=5) as conn:  # CR-138: pool acquire timeout
                # Heartbeat: update timestamp so dashboard knows we're alive
                await conn.execute(
                    "UPDATE agents SET updated_at=NOW() WHERE name=$1",
                    self.agent_name,
                )
                rows = await conn.fetch(
                    "UPDATE pending_messages SET processed=TRUE "
                    "WHERE LOWER(agent_name)=$1 AND processed=FALSE "
                    "AND kind NOT LIKE 'outbound_%' "
                    "RETURNING id, sender_id, content, kind, file_path, created_at, thread_id",
                    self.agent_name,
                )
        except (asyncio.TimeoutError, asyncpg.InterfaceError) as exc:
            self.logger.warning(f"[{self.agent_name}] poll_pending DB error: {exc} — retrying next cycle")
            return []
        if rows:
            # CR-206b: Wait briefly for more messages (natural chat = burst of voice + photo + text)
            await asyncio.sleep(3)
            try:
                async with self._pool.acquire(timeout=5) as conn2:
                    late_rows = await conn2.fetch(
                        "UPDATE pending_messages SET processed=TRUE "
                        "WHERE LOWER(agent_name)=$1 AND processed=FALSE "
                        "AND kind NOT LIKE 'outbound_%' "
                        "RETURNING id, sender_id, content, kind, file_path, created_at, thread_id",
                        self.agent_name,
                    )
                if late_rows:
                    rows = list(rows) + list(late_rows)
                    self.logger.info(f"[{self.agent_name}] poll_pending: {len(rows)} message(s) (incl. {len(late_rows)} late)")
                else:
                    self.logger.info(f"[{self.agent_name}] poll_pending: claimed {len(rows)} message(s)")
            except Exception:
                self.logger.info(f"[{self.agent_name}] poll_pending: claimed {len(rows)} message(s)")
        return [dict(r) for r in rows]

    async def run_loop(self, poll_interval: float | None = None):
        """Main loop: poll → think → reply. Watchdog runs in parallel."""
        if poll_interval is None:
            poll_interval = self.config.get("poll_interval", Config.POLL_INTERVAL)

        try:
            await asyncio.wait_for(self._pool.execute(
                "UPDATE agents SET status='running', updated_at=NOW() WHERE name=$1",
                self.agent_name,
            ), timeout=10)
        except asyncio.TimeoutError:
            self.logger.warning(f"[{self.agent_name}] Status update timed out — continuing anyway")
        self.logger.info(f"[{self.agent_name}] Entering main loop (interval={poll_interval}s)")

        watchdog_task = asyncio.create_task(self._watchdog())

        try:
            while True:
                messages = await self.poll_pending()
                for msg in messages:
                    content = msg.get("content", "")

                    # CR-183: DB-level dedup — skip duplicate messages within 2 minutes
                    if msg.get("kind") in ("telegram", "telegram_voice") and msg.get("sender_id"):
                        try:
                            recent_dup = await self._pool.fetchval(
                                "SELECT COUNT(*) FROM pending_messages "
                                "WHERE agent_name=$1 AND sender_id=$2 AND content=$3 AND processed=TRUE "
                                "AND created_at > NOW() - INTERVAL '2 minutes' AND id < $4",
                                self.agent_name, msg["sender_id"], content, msg["id"],
                            )
                            if recent_dup and recent_dup > 0:
                                self.logger.info(f"[{self.agent_name}] Dedup: skipping duplicate message from {msg['sender_id']}")
                                continue
                        except Exception as _dedup_exc:
                            self.logger.debug(f"[{self.agent_name}] Dedup check failed: {_dedup_exc}")

                    self.logger.info(
                        f"[{self.agent_name}] Processing [{msg.get('kind')}] "
                        f"from {msg.get('sender_id')}: {content[:80]}"
                    )
                    await self.think(content)
                    self._touch()

                # Check wake_up_needed flag (CR-138: timeout protection)
                try:
                    async with self._pool.acquire(timeout=5) as conn:
                        wake = await conn.fetchval(
                            "SELECT wake_up_needed FROM agents WHERE name=$1",
                            self.agent_name,
                        )
                        if wake:
                            await conn.execute(
                                "UPDATE agents SET wake_up_needed=FALSE WHERE name=$1",
                                self.agent_name,
                            )
                            continue
                except (asyncio.TimeoutError, asyncpg.InterfaceError):
                    pass  # non-critical, retry next cycle

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            self.logger.info(f"[{self.agent_name}] Loop cancelled.")
        except Exception as exc:
            self.logger.error(f"[{self.agent_name}] Loop error: {exc}")
            self.logger.debug(traceback.format_exc())
        finally:
            watchdog_task.cancel()
