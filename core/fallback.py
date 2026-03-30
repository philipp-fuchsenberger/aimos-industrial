"""
AIMOS v4.1.0 — Fallback & Safety Nets
=======================================
Extracted from main.py (CR-220).

Contains:
- _external_fallback: escalate to external API when local LLM fails
- _emergency_dream: extract facts from history, store in memory, clear history
- _auto_remember: safety net for explicit "remember" requests
- _auto_followup: safety net for open action items
- merge_queued_messages: CR-206 message batching
"""

import asyncio
import logging
import re
from pathlib import Path


# ── Auto-Remember Safety Net ─────────────────────────────────────────────────

_REMEMBER_TRIGGERS = re.compile(
    r"(?:"
    # German
    r"merk\s*dir|vergiss\s*nicht|wichtig.*merken|notier\s*dir|"
    r"speicher\s*(?:dir|das)|behalte?\s*(?:das|im\s*kopf)|"
    # English
    r"remember\s+(?:this|that)|don'?t\s+forget|keep\s+in\s+mind|note\s+(?:this|that)|"
    # Turkish
    r"bunu\s*(?:not\s+et|unutma|kaydet|hat[ıi]rla)|sakla|aklında\s+tut|"
    # French
    r"souviens[\s-]*toi|n'?oublie\s*pas|note[\s-]*(?:le|ça)|retiens|"
    # Spanish
    r"recuerda\s+(?:esto|que)|no\s+olvides|anota|ten\s+en\s+cuenta"
    r")",
    re.IGNORECASE,
)


async def auto_remember(agent, user_content: str, reply: str, log):
    """Safety net: if user explicitly asked to remember something but agent didn't
    call the remember tool, extract the fact and store it automatically.

    Checks the audit log for a recent TOOL_START remember entry.
    """
    if not _REMEMBER_TRIGGERS.search(user_content):
        return  # user didn't ask to remember anything

    # Check if remember was actually called (via audit log)
    if agent._audit_path and agent._audit_path.exists():
        try:
            lines = agent._audit_path.read_text().strip().split("\n")
            recent = lines[-5:] if len(lines) >= 5 else lines
            if any("TOOL_START" in l and "remember" in l for l in recent):
                return  # tool was called — no intervention needed
        except OSError:
            pass

    # Agent didn't call remember — extract and store automatically
    # Use the user's message as the value, generate a key from content
    clean = re.sub(r"\[Kontext:[^\]]*\]\n?", "", user_content).strip()
    if len(clean) < 5:
        return

    # Simple key extraction: first meaningful words
    words = re.sub(r"[^a-zA-ZäöüßÄÖÜ0-9\s]", "", clean).split()
    key_words = [w for w in words if len(w) > 2 and w.lower() not in
                 ("merk", "dir", "dass", "merke", "bitte", "vergiss", "nicht",
                  "wichtig", "merken", "auch", "noch", "und", "der", "die",
                  "das", "ein", "eine", "wir", "haben", "ist", "sind")][:4]
    key = "_".join(w.lower() for w in key_words) if key_words else "auto_memory"

    if not agent._memory_db_path:
        return
    import sqlite3
    from core.embeddings import embed as _embed_text
    emb = _embed_text(f"{key} {clean}")
    try:
        conn = sqlite3.connect(str(agent._memory_db_path), timeout=5)
        conn.execute(
            "INSERT INTO memories (key, value, category, importance, source, last_accessed, updated_at, embedding) "
            "VALUES (?, ?, 'semantic', 7, 'auto', datetime('now'), datetime('now'), ?) "
            "ON CONFLICT(key) DO UPDATE SET value=?, importance=7, updated_at=datetime('now'), "
            "last_accessed=datetime('now'), embedding=?",
            (key, clean, emb, clean, emb),
        )
        # CR-140: Sync FTS5 index
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        except Exception:
            pass
        conn.commit()
        conn.close()
        log.info(f"[{agent.agent_name}] AUTO-REMEMBER: {key} = {clean[:60]}")
    except Exception as exc:
        log.warning(f"[{agent.agent_name}] auto-remember failed: {exc}")


# ── Auto-Followup Safety Net ────────────────────────────────────────────────

_FOLLOWUP_RE = re.compile(
    r"(?:soll ich|m[oö]chten?\s+(?:Sie|du)|warten\s+(?:Sie|wir)|"
    r"lass(?:en)?\s+(?:Sie\s+)?(?:mich|es|uns)\s+wissen|"
    r"gib(?:st|t)?\s+(?:mir|uns)\s+(?:bitte\s+)?bescheid|"
    r"ich\s+warte\s+auf|ich\s+hake\s+nach|"
    r"bei\s+(?:R[uü]ck|Interesse|Bedarf|Fragen)|"
    r"melde(?:st|t|n)?\s+(?:dich|Sie\s+sich)|"
    r"ich\s+(?:bleibe|bin)\s+dran)",
    re.IGNORECASE,
)


async def auto_followup(agent, reply: str, log):
    """Safety net: if the agent's reply implies it's waiting for a response
    or has open action items, but didn't set a reminder — create one automatically.

    Only fires if no set_reminder was called in this cycle (check audit log).
    """
    if not _FOLLOWUP_RE.search(reply):
        return  # reply doesn't imply waiting

    # Check if set_reminder was already called
    if agent._audit_path and agent._audit_path.exists():
        try:
            lines = agent._audit_path.read_text().strip().split("\n")
            recent = lines[-8:] if len(lines) >= 8 else lines
            if any("TOOL_START" in l and "set_reminder" in l for l in recent):
                return  # agent already set a reminder
        except OSError:
            pass

    # Auto-set a 30-minute followup (CR-110: max 3 auto-followups per agent)
    topic = reply[:80].replace("\n", " ").strip()
    try:
        import asyncpg
        from core.config import Config
        conn = await asyncpg.connect(**Config.get_db_params())

        # CR-110: Don't pile up auto-followups
        pending_followups = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_jobs WHERE agent_name=$1 AND source='auto_followup' AND status='pending'",
            agent.agent_name,
        )
        if pending_followups >= 3:
            await conn.close()
            log.info(f"[{agent.agent_name}] AUTO-FOLLOWUP: skipped (already {pending_followups} pending)")
            return

        from datetime import datetime, timezone, timedelta
        scheduled = datetime.now(timezone.utc) + timedelta(minutes=30)
        await conn.execute(
            "INSERT INTO agent_jobs (agent_name, scheduled_time, task_prompt, source) "
            "VALUES ($1, $2, $3, 'auto_followup')",
            agent.agent_name, scheduled,
            f"[Auto-Followup] Du hattest auf eine Antwort gewartet zum Thema: {topic}. "
            f"Pruefe ob der User geantwortet hat und hake ggf. nach.",
        )
        await conn.close()
        log.info(f"[{agent.agent_name}] AUTO-FOLLOWUP: scheduled in 30m — {topic[:50]}")
    except Exception as exc:
        log.warning(f"[{agent.agent_name}] auto-followup failed: {exc}")


# ── External Fallback ────────────────────────────────────────────────────────

async def external_fallback(agent, user_message: str, msg: dict, log,
                             reason: str = "timeout") -> str:
    """Escalate to external API when local LLM fails. Returns LLM-generated reply.

    Three phases:
    1. Dispatch an interim "working on it" message (LLM-generated in user's language)
    2. Get the actual answer from external API with natural lead-in
    3. Dream-on-demand: extract facts from history, store in memory, clear history
       → frees context for the local LLM on the next message

    All text is LLM-generated — no hardcoded language fragments.
    """
    try:
        from core.skills.skill_hybrid_reasoning import HybridReasoningSkill
        hr = HybridReasoningSkill(agent.agent_name, agent.config,
                                   secrets=getattr(agent, "_env_secrets", {}))
        if not hr.is_available():
            log.warning(f"[{agent.agent_name}] External API not available for fallback")
            return ""

        # Build conversation summary for context
        history_summary = ""
        try:
            recent = agent._history[-10:] if hasattr(agent, '_history') else []
            if recent:
                history_lines = []
                for m in recent:
                    role = m.get("role", "?")
                    content = (m.get("content", ""))[:200]
                    if content and not content.startswith("Tool "):
                        history_lines.append(f"{role}: {content}")
                history_summary = "\n".join(history_lines[-6:])
        except Exception:
            pass

        # Phase 1+2 combined: try to get the answer directly.
        # Only send "please wait" if the answer takes >5 seconds.
        answer_context = (
            f"You are answering on behalf of a specialized support agent. "
            f"The local AI could not process this question (reason: {reason}). "
            f"Provide a helpful answer. "
            f"Respond in the SAME LANGUAGE as the user's question. "
            f"Be concise (max 200 words). Do not use emojis. Be professional."
            f"\n\nRecent conversation for context:\n{history_summary}" if history_summary else
            f"You are answering on behalf of a specialized support agent. "
            f"The local AI could not process this question (reason: {reason}). "
            f"Provide a helpful answer. "
            f"Respond in the SAME LANGUAGE as the user's question. "
            f"Be concise (max 200 words). Do not use emojis. Be professional."
        )
        answer_task = asyncio.create_task(hr.execute_tool("ask_external", {
            "question": user_message[:800],
            "context": answer_context,
        }))

        # Wait up to 5s for the answer — if it's not ready, send interim
        INTERIM_DELAY = 5
        interim_sent = False
        try:
            answer = await asyncio.wait_for(asyncio.shield(answer_task), timeout=INTERIM_DELAY)
        except asyncio.TimeoutError:
            # Answer taking long — send "please wait" now
            try:
                interim = await hr.execute_tool("ask_external", {
                    "question": user_message[:200],
                    "context": (
                        "The user asked this question but the system needs a moment to research. "
                        "Generate ONLY a brief 1-sentence polite 'please wait' message in the SAME "
                        "LANGUAGE as the user's question. Do not answer the question itself yet. "
                        "Do not use emojis. Example style: 'One moment please, I am researching this for you.'"
                    ),
                })
                if interim and len(interim.strip()) > 5:
                    # Skip interim for email — "please wait" makes no sense asynchronously
                    _msg_kind = msg.get("kind", "")
                    if _msg_kind != "email":
                        await agent.dispatch_response(interim.strip(), msg)
                        interim_sent = True
                        log.info(f"[{agent.agent_name}] Fallback Phase 1: interim dispatched (answer took >{INTERIM_DELAY}s)")
                    else:
                        log.info(f"[{agent.agent_name}] Fallback Phase 1: skipped for email (async channel)")
            except Exception as exc:
                log.debug(f"[{agent.agent_name}] Interim message failed: {exc}")
            # Now wait for the actual answer
            answer = await answer_task

        if answer and len(answer.strip()) > 20:
            log.info(f"[{agent.agent_name}] Fallback Phase 2: answer received ({len(answer)} chars)")

            # Phase 3: Dream-on-demand — extract facts + clear history
            await _emergency_dream(agent, hr, log)

            return answer.strip()

    except Exception as exc:
        log.error(f"[{agent.agent_name}] External fallback failed completely: {exc}")

    return ""


async def _emergency_dream(agent, hr, log):
    """Extract key facts from conversation via external API, store in memory, clear history.

    This frees context for the local LLM. Runs after an escalation to external API.
    """
    try:
        # Collect recent conversation
        recent = agent._history[-15:] if hasattr(agent, '_history') else []
        if len(recent) < 4:
            return  # Not enough history to dream about

        conv_lines = []
        for m in recent:
            role = m.get("role", "?")
            content = (m.get("content", ""))[:300]
            if content and not content.startswith("Tool "):
                conv_lines.append(f"{role}: {content}")

        if not conv_lines:
            return

        conversation_text = "\n".join(conv_lines)

        # Ask external API to extract facts
        extraction = await hr.execute_tool("ask_external", {
            "question": "Extract key facts from this conversation.",
            "context": (
                "Extract the most important facts from this conversation as a list. "
                "Each fact should be one line in the format: KEY: VALUE\n"
                "Examples:\n"
                "customer_name: Thomas Mueller, Feuerwehr Rosenheim\n"
                "equipment: 3x CAPITANO II\n"
                "serial_BK-2024-22222: 890 operating hours\n"
                "issue_reported: error E-03, low oil pressure\n\n"
                "Only extract facts worth remembering for future conversations. "
                "Max 10 facts. No commentary, just KEY: VALUE lines.\n\n"
                f"CONVERSATION:\n{conversation_text}"
            ),
        })

        if not extraction or len(extraction.strip()) < 10:
            return

        # Parse and store facts in agent's SQLite memory
        import sqlite3
        from core.skills.base import BaseSkill
        db_path = BaseSkill.memory_db_path(agent.agent_name)
        if not db_path.exists():
            return

        conn = sqlite3.connect(str(db_path), timeout=5)
        stored = 0
        for line in extraction.strip().split("\n"):
            line = line.strip()
            if ":" not in line or len(line) < 5:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")[:100]
            value = value.strip()[:500]
            if not key or not value:
                continue
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO memories (key, value, category, importance, updated_at) "
                    "VALUES (?, ?, 'semantic', 7, datetime('now'))",
                    (f"dream_{key}", value)
                )
                stored += 1
            except Exception:
                pass
        conn.commit()
        conn.close()

        # Clear chat history to free context
        if stored > 0 and agent._pool:
            deleted = await agent._pool.fetchval(
                "WITH excess AS ("
                "  SELECT id FROM aimos_chat_histories "
                "  WHERE agent_name=$1 "
                "  AND id NOT IN ("
                "    SELECT id FROM aimos_chat_histories WHERE agent_name=$1 "
                "    ORDER BY id DESC LIMIT 4"
                "  )"
                ") "
                "DELETE FROM aimos_chat_histories WHERE id IN (SELECT id FROM excess) "
                "RETURNING id",
                agent.agent_name,
            )
            # Also clear in-memory history
            if hasattr(agent, '_history') and len(agent._history) > 4:
                agent._history = agent._history[-4:]

            log.info(
                f"[{agent.agent_name}] Emergency dream: {stored} facts stored, "
                f"{deleted or 0} history entries cleared"
            )

    except Exception as exc:
        log.warning(f"[{agent.agent_name}] Emergency dream failed (non-critical): {exc}")


# ── Message Merging ──────────────────────────────────────────────────────────

def merge_queued_messages(messages: list[dict], agent, log) -> list[list[dict]]:
    """CR-206: Merge queued messages from the same sender+channel into batches.

    Natural chat behavior: user sends "Hello" + "I need help with X" quickly.
    Instead of 2 separate LLM calls, merge into one: "Hello\nI need help with X".

    Grouping rules:
    - Same sender_id AND same kind → merge
    - Different sender or kind → separate batch
    - Max ~4000 chars per batch (leave room in context)
    - Each batch becomes one think() call

    Returns list of message groups. Each group is a list of msg dicts.
    """
    if not messages:
        return []

    # CR-211: Sort by sender wait time (oldest first = fairest)
    # Messages from different senders get interleaved fairly
    messages.sort(key=lambda m: m.get("created_at") or "")

    MAX_BATCH_CHARS = 4000  # Leave room for system prompt + memory + history
    ESCALATE_THRESHOLD = 8000  # If total queued content > this, escalate to external API

    batches = []
    current_batch = []
    current_chars = 0
    current_key = None  # (sender_id, kind)

    for msg in messages:
        sender_id = msg.get("sender_id", 0)
        kind = msg.get("kind", "text")
        content = msg.get("content", "")

        # Group Telegram family together (text, voice, doc, photo all from same user)
        kind_family = kind
        if kind in ("telegram", "telegram_voice", "telegram_doc", "telegram_photo"):
            kind_family = "telegram_family"
        # L3: Include thread_id in merge key — emails all have sender_id=0,
        # so without thread_id they'd wrongly merge into one batch
        key = (sender_id, kind_family, msg.get("thread_id", ""))

        if key != current_key or current_chars + len(content) > MAX_BATCH_CHARS:
            # Start new batch
            if current_batch:
                batches.append(current_batch)
            current_batch = [msg]
            current_chars = len(content)
            current_key = key
        else:
            # Add to current batch
            current_batch.append(msg)
            current_chars += len(content)

    if current_batch:
        batches.append(current_batch)

    # Check total volume — if too much for local LLM, mark for escalation
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars > ESCALATE_THRESHOLD:
        log.warning(
            f"[CR-206] Queue overflow: {total_chars} chars in {len(messages)} messages "
            f"→ exceeds local context budget. Marking batches for external escalation."
        )
        # Mark each batch for escalation
        for batch in batches:
            batch[0]["_escalate_to_external"] = True

    # Log merges
    for batch in batches:
        if len(batch) > 1:
            log.info(
                f"[CR-206] Merged {len(batch)} messages: "
                f"sender={batch[0].get('sender_id')}, kind={batch[0].get('kind')}, "
                f"total_chars={sum(len(m.get('content', '')) for m in batch)}"
            )

    return batches
