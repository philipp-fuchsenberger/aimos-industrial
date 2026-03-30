#!/usr/bin/env python3
"""
AIMOS v4.1.0 — Shard Entrypoint
==================================
Orchestriert den AIMOSAgent-Kernel und seine Connectors.

Usage:
  python main.py                        # default agent, orchestrator mode
  python main.py --id researcher        # start agent 'researcher'
  python main.py --mode manual          # manual Telegram polling (no orchestrator)
  python main.py --debug                # verbose logging
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import psutil

from core.config import Config, SecretLogFilter, make_rotating_handler
from core.agent_base import AIMOSAgent
from core.tools import load_tools, transcribe_voice
from core.fallback import (
    external_fallback,
    auto_followup,
    merge_queued_messages,
)


# ── Logging Setup ─────────────────────────────────────────────────────────────

def setup_logging(agent_id: str, debug: bool = False) -> logging.Logger:
    """Configure dual logging: terminal + logs/{agent_id}.log (rotating, 10MB x 5)."""
    level = logging.DEBUG if debug else logging.INFO

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{agent_id}.log"

    # File handler — rotating (10MB, 5 backups)
    fh = make_rotating_handler(log_file)
    fh.setLevel(level)

    # Console handler
    fmt = logging.Formatter(
        "[%(asctime)s] %(name)-24s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    ch.addFilter(SecretLogFilter())

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)

    return logging.getLogger("AIMOS.main")


# ── Banner ────────────────────────────────────────────────────────────────────

def print_banner(agent_id: str, mode: str):
    log = logging.getLogger("AIMOS.main")
    log.info("=" * 60)
    log.info("  AIMOS v4.1.0 — Shard Kernel")
    log.info("=" * 60)
    log.info(f"  Agent:    {agent_id}")
    log.info(f"  Mode:     {mode}")
    log.info(f"  DB:       {Config.PG_USER}@{Config.PG_HOST}:{Config.PG_PORT}/{Config.PG_DB}")
    log.info(f"  LLM:      {Config.LLM_BASE_URL} ({Config.LLM_MODEL})")
    log.info("=" * 60)


# ── Argument Parser ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AIMOS v4.1.0 — Shard Entrypoint")
    p.add_argument(
        "--id", default="agent1",
        help="Agent identifier (default: agent1)",
    )
    p.add_argument(
        "--mode", choices=["manual", "orchestrator"], default="orchestrator",
        help="manual = Telegram polling in-process; orchestrator = DB queue only (default)",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def _acquire_pidfile(agent_id: str) -> Path:
    """Singleton: create PID file or abort if agent already running."""
    pidfile = Path(f"/tmp/aimos_agent_{agent_id}.pid")
    if pidfile.exists():
        try:
            old_pid = int(pidfile.read_text().strip())
            if psutil.pid_exists(old_pid):
                # Check it's actually an AIMOS process, not a recycled PID
                try:
                    proc = psutil.Process(old_pid)
                    cmdline = " ".join(proc.cmdline())
                    if "main.py" in cmdline and agent_id in cmdline:
                        print(f"ABORT: Agent '{agent_id}' already running (PID={old_pid})")
                        sys.exit(1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (ValueError, OSError):
            pass
        # Stale PID file — remove it
        pidfile.unlink(missing_ok=True)
    pidfile.write_text(str(os.getpid()))
    return pidfile


def _release_pidfile(agent_id: str):
    """Remove PID file on exit."""
    Path(f"/tmp/aimos_agent_{agent_id}.pid").unlink(missing_ok=True)


async def main():
    args = parse_args()
    agent_id = args.id.lower()

    # Singleton guard
    import atexit
    pidfile = _acquire_pidfile(agent_id)
    atexit.register(_release_pidfile, agent_id)

    log = setup_logging(agent_id, debug=args.debug)
    print_banner(agent_id, args.mode)

    # ── Build agent config ────────────────────────────────────────────────
    agent_config = {
        "mode": args.mode,
        "temperature": Config.TEMPERATURE,
        "num_ctx": Config.DEFAULT_NUM_CTX,
        "history_limit": Config.HISTORY_LIMIT,
        "poll_interval": Config.POLL_INTERVAL,
    }

    agent = AIMOSAgent(agent_name=agent_id, config=agent_config)
    shutdown_event = asyncio.Event()

    # ── Signal handlers (SIGTERM + SIGINT) ────────────────────────────────
    loop = asyncio.get_running_loop()

    def _signal_handler():
        log.info("Shutdown signal received.")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        # 1. Bootstrap: DB + Schema + Seed + Secrets + Audit + History + Queue
        await agent.start()
        log.info(f"[{agent_id}] Agent bootstrapped.")

        # 2. Load tools (Brave Search etc.)
        load_tools(agent)

        # Both modes use DB relay for Telegram (shared_listener handles sending)
        # No direct Telegram polling — prevents 409 Conflict with shared_listener
        await _run_orchestrator(agent, shutdown_event)

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down.")
    except Exception as exc:
        log.error(f"Fatal error: {exc}", exc_info=True)
    finally:
        try:
            await agent.stop()
            log.info(f"[{agent_id}] Agent stopped.")
        except Exception as exc:
            log.warning(f"Agent stop error: {exc}")


# ── Mode: Orchestrator (DB queue polling, replies via DB relay) ────────────────

async def _run_orchestrator(agent: AIMOSAgent, shutdown_event: asyncio.Event):
    """Orchestrator mode: process pending_messages, route replies via dispatch_response."""
    log = logging.getLogger("AIMOS.main")
    log.info(f"[{agent.agent_name}] Orchestrator mode — DB queue, dispatch_response routing.")

    async def _process_loop():
        poll_interval = agent.config.get("poll_interval", Config.POLL_INTERVAL)
        if agent._pool:
            await agent._pool.execute(
                "UPDATE agents SET status='running', updated_at=NOW() WHERE name=$1",
                agent.agent_name,
            )

        watchdog_task = asyncio.create_task(agent._watchdog())
        try:
            while not shutdown_event.is_set():
                messages = await agent.poll_pending()
                if messages:
                    agent._touch()  # reset watchdog on ANY activity
                    if agent._pool:
                        await agent._pool.execute(
                            "UPDATE agents SET status='active', updated_at=NOW() WHERE name=$1",
                            agent.agent_name,
                        )
                else:
                    # No messages — set status to 'idle' (watchdog will fire after 600s)
                    if agent._pool:
                        await agent._pool.execute(
                            "UPDATE agents SET status='idle', updated_at=NOW() WHERE name=$1",
                            agent.agent_name,
                        )
                # CR-206: Merge queued messages from same sender+channel into one request
                # This matches natural chat behavior (user sends multiple messages quickly)
                merged_messages = merge_queued_messages(messages, agent, log)

                for msg_group in merged_messages:
                    msg = msg_group[0]  # Primary message (for routing metadata)
                    content = msg.get("content", "")
                    sender_id = msg.get("sender_id", 0)
                    kind = msg.get("kind", "text")

                    # CR-203: Skip scheduled jobs if agent has disable_auto_jobs
                    if kind == "scheduled_job" and agent.config.get("disable_auto_jobs"):
                        log.info(f"[{agent.agent_name}] Skipping scheduled_job (disable_auto_jobs=true)")
                        continue
                    voice_path = msg.get("file_path", "")

                    # If multiple messages were merged, combine their content
                    if len(msg_group) > 1:
                        combined_parts = []
                        for m in msg_group:
                            mc = m.get("content", "")
                            mk = m.get("kind", "text")
                            mvp = m.get("file_path", "")
                            # Voice transcription
                            if mk == "telegram_voice" and mvp:
                                mc = await transcribe_voice(agent, mvp, log)
                            elif mk == "telegram_doc" and mvp:
                                from pathlib import Path as _P
                                fname = _P(mvp).name
                                mc = f"[Dokument empfangen: {fname}] Nutze read_file(filename=\"{fname}\") um sie zu lesen."
                            if mc.strip():
                                combined_parts.append(mc.strip())
                        content = "\n".join(combined_parts)
                        log.info(
                            f"[{agent.agent_name}] MERGED {len(msg_group)} messages from "
                            f"sender={sender_id} kind={kind}: {len(content)} chars total"
                        )
                    else:
                        log.info(
                            f"[{agent.agent_name}] MSG id={msg.get('id')} "
                            f"sender={sender_id} kind={kind} len={len(content)}"
                        )
                        # Voice transcription: download → Whisper → text
                        if kind == "telegram_voice" and voice_path:
                            content = await transcribe_voice(agent, voice_path, log)
                        # CR-118: Document received
                        if kind == "telegram_doc" and voice_path:
                            from pathlib import Path as _P
                            fname = _P(voice_path).name
                            content = f"[Dokument empfangen: {fname}] Nutze read_file(filename=\"{fname}\") um sie zu lesen."

                    # Inject sender context + timestamp
                    ts = msg.get("created_at")
                    if ts:
                        from datetime import timezone as _tz
                        if hasattr(ts, "astimezone"):
                            local_ts = ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            local_ts = str(ts)[:19]
                    else:
                        from datetime import datetime as _dt
                        local_ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

                    # CR-123: Conversation Partner Awareness
                    if kind == "telegram":
                        sender_name = "Helpdesk"
                    elif kind == "internal":
                        sender_name = "Agent"
                    elif kind == "scheduled_job":
                        sender_name = "System (Cronjob)"
                    else:
                        sender_name = "System"

                    # Skip empty content (e.g. emoji-only message after CJK filter)
                    if not content or not content.strip():
                        log.info(f"[{agent.agent_name}] Skipping empty message (id={msg.get('id')})")
                        continue

                    # CR-153: Track message kind so tools can check context
                    agent._current_msg_kind = kind
                    agent._last_msg_content = content  # For send_to_agent reply-to extraction

                    # CR-209: Set session_id for multi-user history isolation
                    # CR-thread: Set thread_id from message (propagated from shared_listener)
                    # IMPORTANT: Must be set BEFORE customer context injection below
                    msg_thread_id = msg.get("thread_id", "") or ""
                    if kind in ("telegram", "telegram_voice", "telegram_doc"):
                        agent._current_session_id = f"telegram:{sender_id}"
                        agent._current_thread_id = msg_thread_id or f"tg:{sender_id}"
                    elif kind == "email":
                        agent._current_session_id = f"email:{sender_id}"
                        agent._current_thread_id = msg_thread_id or f"email:{sender_id}"
                    elif kind == "internal":
                        agent._current_session_id = f"internal:{sender_id}"
                        agent._current_thread_id = msg_thread_id or f"internal:{sender_id}"
                    else:
                        agent._current_session_id = f"dashboard:{msg.get('id', 0)}"
                        agent._current_thread_id = msg_thread_id or f"dashboard:{msg.get('id', 0)}"

                    # Inject current customer context from Kundenakte if available
                    _customer_hint = ""
                    _cur_thread = getattr(agent, '_current_thread_id', '') or ''
                    if _cur_thread:
                        import json as _json_ctx
                        _cust_dir = Path("storage/customers")
                        if _cust_dir.exists():
                            for _cf in _cust_dir.glob("*.json"):
                                try:
                                    _cd = _json_ctx.loads(_cf.read_text(encoding="utf-8"))
                                    if _cur_thread in _cd.get("thread_ids", []):
                                        _customer_hint = (
                                            f"\n[Current customer: {_cd.get('name', '?')}"
                                            f" | {_cd.get('company', '')}"
                                            f" | Email: {_cd.get('email', '?')}"
                                            f" | Products: {', '.join(_cd.get('products', []))}"
                                            f" | Orders: {'; '.join(_cd.get('orders', [])[-2:])}"
                                            f"]\n[IMPORTANT: This conversation is ONLY about this customer. "
                                            f"Ignore any memory entries about other customers.]"
                                        )
                                        break
                                except Exception:
                                    pass

                    content_with_ctx = (
                        f"[Von: {sender_name} | Kanal: {kind} | Zeit: {local_ts}]"
                        f"{_customer_hint}\n{content}"
                    )

                    # CR-156: Per-message tool-call budget
                    agent._tool_call_count = 0
                    agent._tool_call_budget = agent.config.get("max_tool_calls_per_message", 10)

                    # CR-206: If marked for escalation (queue overflow), go directly to external API
                    if msg.get("_escalate_to_external"):
                        log.info(f"[{agent.agent_name}] CR-206: Queue overflow → direct external API escalation")
                        reply = await external_fallback(agent, content, msg, log,
                                                        reason="queue_overflow")
                        if reply:
                            # Skip think() entirely — external API handled it
                            agent._touch()
                            if not agent.config.get("disable_auto_jobs"):
                                await auto_followup(agent, reply, log)
                            route = await agent.dispatch_response(reply, msg)
                            log.info(f"[{agent.agent_name}] Dispatched (external) → {route}")
                            continue

                    # CR-207b removed: Image analysis is now handled by the agent's
                    # analyze_image tool directly (no code-level bypass needed).

                    try:
                        reply = await asyncio.wait_for(agent.think(content_with_ctx), timeout=300)
                        log.info(f"[{agent.agent_name}] think() → {len(reply)} chars")
                        # If response filter stripped everything → log but don't escalate
                        if not reply or not reply.strip():
                            log.warning(f"[{agent.agent_name}] think() returned empty — no fallback configured")
                            reply = "Ihre Anfrage wird bearbeitet."
                    except asyncio.TimeoutError:
                        log.error(f"[{agent.agent_name}] think() TIMED OUT — escalating to external API")
                        reply = await external_fallback(agent, content, msg, log,
                                                        reason="timeout")
                        msg["_fallback_handled"] = True
                    except Exception as exc:
                        log.error(f"[{agent.agent_name}] think() FAILED: {exc}")
                        reply = await external_fallback(agent, content, msg, log,
                                                        reason=f"error: {exc}")
                        msg["_fallback_handled"] = True

                    agent._touch()

                    # CR-213: Check-before-Send (config: check_before_send=true)
                    # After think(), check if more messages arrived from same sender.
                    # If so, discard reply, merge everything, re-process.
                    # Config: image_delegate — agent name to route images to first.
                    if (
                        agent.config.get("check_before_send")
                        and kind not in ("internal", "scheduled_job")
                    ):
                        late_msgs = await agent.poll_pending()
                        if not late_msgs:
                            log.info(f"[CR-213] Check-before-Send: no late messages — sending reply")
                        if late_msgs:
                            # L4: For email kind, filter by thread_id instead of sender_id
                            # (sender_id=0 for all emails would match everything)
                            if kind == "email":
                                _current_thread = getattr(agent, '_current_thread_id', '')
                                related_msgs = [m for m in late_msgs if m.get("thread_id", "") == _current_thread]
                                other_msgs = [m for m in late_msgs if m.get("thread_id", "") != _current_thread]
                            else:
                                related_msgs = [m for m in late_msgs if m.get("sender_id") == sender_id]
                                other_msgs = [m for m in late_msgs if m.get("sender_id") != sender_id]
                            # Alias for backward compat in the block below
                            same_sender = related_msgs
                            if same_sender:
                                log.info(
                                    f"[CR-213] Check-before-Send: {len(same_sender)} late message(s) "
                                    f"from sender={sender_id} — discarding reply, will re-process"
                                )
                                # Merge original + late messages, re-process as one
                                all_reprocess = list(msg_group) + same_sender
                                combined_parts = []
                                for m in all_reprocess:
                                    mc = m.get("content", "")
                                    mk = m.get("kind", "text")
                                    mvp = m.get("file_path", "")
                                    if mk == "telegram_voice" and mvp:
                                        mc = await transcribe_voice(agent, mvp, log)
                                    elif mk == "telegram_doc" and mvp:
                                        from pathlib import Path as _P
                                        fname = _P(mvp).name
                                        mc = f"[Dokument empfangen: {fname}] Nutze read_file(filename=\"{fname}\") um sie zu lesen."
                                    if mc and mc.strip():
                                        combined_parts.append(mc.strip())
                                merged_content = "\n".join(combined_parts)
                                content_with_ctx = (
                                    f"[Von: {sender_name} | Kanal: {kind} | Zeit: {local_ts}]\n"
                                    f"{merged_content}"
                                )
                                log.info(
                                    f"[CR-213] Re-processing {len(all_reprocess)} messages "
                                    f"as one ({len(merged_content)} chars)"
                                )
                                try:
                                    reply = await asyncio.wait_for(
                                        agent.think(content_with_ctx), timeout=120
                                    )
                                    log.info(f"[{agent.agent_name}] think() retry → {len(reply)} chars")
                                    if not reply or not reply.strip():
                                        reply = await external_fallback(
                                            agent, merged_content, msg, log,
                                            reason="empty_response_retry"
                                        )
                                except (asyncio.TimeoutError, Exception) as exc:
                                    log.error(f"[{agent.agent_name}] think() retry failed: {exc}")
                                    reply = await external_fallback(
                                        agent, merged_content, msg, log,
                                        reason=f"retry_error: {exc}"
                                    )

                            # Re-queue messages from other senders
                            if other_msgs and agent._pool:
                                for om in other_msgs:
                                    await agent._pool.execute(
                                        "INSERT INTO pending_messages "
                                        "(agent_name, sender_id, content, kind, thread_id) "
                                        "VALUES ($1, $2, $3, $4, $5)",
                                        agent.agent_name,
                                        om.get("sender_id", 0),
                                        om.get("content", ""),
                                        om.get("kind", "text"),
                                        om.get("thread_id", ""),
                                    )
                                log.info(
                                    f"[CR-213] Re-queued {len(other_msgs)} message(s) "
                                    f"from other senders"
                                )
                    # CR-145: Update contact's last_interaction timestamp
                    try:
                        from core.skills.skill_contacts import auto_update_contact_from_message
                        auto_update_contact_from_message(agent.agent_name, msg.get("sender_id", 0), kind)
                    except Exception:
                        pass

                    # Auto-remember: REMOVED — dreaming Phase 0 (LLM-powered) handles
                    # memory extraction from conversations much more precisely than
                    # regex triggers, which caused false positives.

                    # Auto-followup: if agent's reply implies waiting, set a reminder
                    # BUT NOT for scheduled_job replies — that creates infinite loops
                    # (cronjob fires → agent says "waiting" → auto-followup → new cronjob → repeat)
                    if kind not in ("scheduled_job", "internal"):
                        if not agent.config.get("disable_auto_jobs"):
                            await auto_followup(agent, reply, log)

                    # Route reply via agent's dispatch_response (decoupled from main.py)
                    route = await agent.dispatch_response(reply, msg)
                    log.info(f"[{agent.agent_name}] Dispatched → {route}")

                    # CR-thread: Auto-notify after email send
                    # If the agent called send_email during this think cycle,
                    # notify the configured agent (via notify_on_email config)
                    notify_target = agent.config.get("notify_on_email", "")
                    if (notify_target
                            and getattr(agent, '_email_sent_this_cycle', False)
                            and agent._pool
                            and agent.agent_name != notify_target):
                        try:
                            _notify_thread_id = getattr(agent, '_current_thread_id', '') or ''
                            await agent._pool.execute(
                                "INSERT INTO pending_messages "
                                "(agent_name, sender_id, content, kind, thread_id) "
                                "VALUES ($1, 0, $2, 'internal', $3)",
                                notify_target,
                                f"[Nachricht von {agent.agent_name}] "
                                f"E-Mail wurde gesendet. Antwort des Agenten: {reply[:500]}",
                                _notify_thread_id,
                            )
                            await agent._pool.execute(
                                "UPDATE agents SET wake_up_needed=TRUE WHERE LOWER(name)=$1",
                                notify_target,
                            )
                            log.info(
                                f"[CR-thread] Auto-notify: {agent.agent_name} sent email "
                                f"→ notified {notify_target} (thread={_notify_thread_id})"
                            )
                        except Exception as exc:
                            log.warning(f"[CR-thread] Auto-notify failed: {exc}")

                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            watchdog_task.cancel()

    loop_task = asyncio.create_task(_process_loop())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [loop_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
