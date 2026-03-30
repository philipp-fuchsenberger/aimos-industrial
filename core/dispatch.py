"""
AIMOS Dispatch — extracted from agent_base.py (CR-221)
======================================================
Routes agent replies to the correct output channel:
  - Telegram (via DB relay)
  - Email (with signature, dedup, allowlist, noreply blocking)
  - Dashboard
  - Scheduled jobs
  - Voice (local TTS, remote)
  - Internal (agent-to-agent relay with ping-pong limit)
  - Catch-all (last active connector)

Used by AIMOSAgent via DispatchMixin.
"""

import json
import logging
import re

_log = logging.getLogger("AIMOS.Dispatch")


class DispatchMixin:
    """Mixin providing dispatch_response for AIMOSAgent."""

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
                self.logger.warning("[Relay] Blocking raw tool-call as email reply")
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
                # CR-227: Enforce formal salutation on every outbound customer email
                _salutation_re = re.compile(
                    r'^\s*(?:Sehr geehrte|Dear |Sayın |Cher |Chère |Estimado |Estimada )',
                    re.IGNORECASE
                )
                if not _salutation_re.match(reply):
                    reply = "Sehr geehrte Damen und Herren,\n\n" + reply
                    self.logger.info("[CR-227] Prepended default salutation to outbound email")
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
                self.logger.info("[Relay] Scheduled job: skipping Telegram (already sent this cycle)")
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
                                "[Relay] Skipping auto-forward — external fallback already replied"
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
                                        # CR-227: Enforce formal salutation on relay emails too
                                        if not re.match(r'^\s*(?:Sehr geehrte|Dear |Sayın |Cher |Chère |Estimado |Estimada )', _relay_body, re.IGNORECASE):
                                            _relay_body = "Sehr geehrte Damen und Herren,\n\n" + _relay_body
                                            self.logger.info("[CR-227] Prepended default salutation to relay email")
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

                    return "internal:relayed"
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
