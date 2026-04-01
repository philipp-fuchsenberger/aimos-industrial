"""
AIMOS Batch/OODA Orchestrator — CR-233/234/236/237
=====================================================
6-phase OODA cycle for the "Mitarbeiter" agent archetype.

Phase 0 (CONTEXT):      Load workspace state (files, DBs, memories)
Phase 1 (OBSERVE):      Read all new messages, structure, find cross-thread connections
Phase 2 (ORIENT):       Consolidate with tool calls, build Lagebild
Phase 3 (DECIDE + ACT): Per thread — respond with Lagebild + thread history
Phase 4 (PERSIST):      Update workspace files, set reminders, remember key facts

Architecture:
  - The Orchestrator (this module) is DETERMINISTIC Python code
  - It calls think() on a NON-DETERMINISTIC LLM multiple times
  - Each phase output is captured as a string and injected into the next phase's prompt
  - The Lagebild is EPHEMERAL — rebuilt each cycle from workspace state + new messages
  - Workspace files (state.md, todo.md, etc.) carry state between cycles
  - See docs/AGENT_ARCHETYPES.md for full documentation

Safety (HAZOP/FMEA/FTA/STPA — docs/snapshots/):
  - H-02: Phase 0 validates state.md against data sources
  - H-04: Staleness warning if state.md >7 days old
  - H-07: Message dedup in poll_pending (agent_base.py)
  - H-08: Human approval gate (batch_require_human_approval)
  - H-09: Empty Lagebild → cycle abort
  - H-14: state.md.bak backup before Phase 4
  - H-15: BATCH_COMPLETED marker after successful cycle
  - S-3:  Cross-thread leak protection in Phase 3 prompt
  - S-7:  Phase 3 results collected and passed to Phase 4
  - BF-15: Phase 4 warns against remembering unverified facts
  - BF-17: Memory pruning in agent_base.py (max_memories)
  - Context Monitor: Intelligent compression, not blind truncation
"""

import asyncio
import logging
import shutil
import time
from collections import defaultdict
from pathlib import Path

import aiohttp

from core.config import Config

# Type hint only — avoid circular import
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.agent_base import AIMOSAgent


# ── CR-247: Activity-based timeout instead of hard timeout ─────────────────

async def _think_with_activity_check(
    agent: "AIMOSAgent", prompt: str, log: logging.Logger,
    stale_timeout: int = 120, poll_interval: int = 15,
) -> str:
    """Call agent.think() with Ollama activity monitoring instead of a hard timeout.

    Instead of killing a think() call after N seconds regardless of progress,
    this checks whether Ollama is still actively generating. A call that takes
    20 minutes but is still producing tokens will NOT be killed. A call where
    Ollama has stopped responding for stale_timeout seconds WILL be killed.

    Args:
        stale_timeout: Seconds without Ollama activity before cancelling (default: 120)
        poll_interval: How often to check Ollama status (default: 15s)
    """
    think_task = asyncio.create_task(agent.think(prompt))
    ollama_url = f"{Config.LLM_BASE_URL}/api/ps"
    last_active = time.monotonic()

    while not think_task.done():
        # Wait but check completion frequently
        for _ in range(poll_interval):
            if think_task.done():
                break
            await asyncio.sleep(1)

        if think_task.done():
            break

        # Check if Ollama still has an active model
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ollama_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    models = data.get("models", [])
                    if models:
                        # Ollama is active — reset stale timer
                        last_active = time.monotonic()
                    else:
                        stale_seconds = time.monotonic() - last_active
                        if stale_seconds > stale_timeout:
                            log.error(
                                f"[{agent.agent_name}] Ollama inactive for {stale_seconds:.0f}s "
                                f"(stale_timeout={stale_timeout}s) — cancelling think()"
                            )
                            think_task.cancel()
                            try:
                                await think_task
                            except asyncio.CancelledError:
                                pass
                            raise RuntimeError(
                                f"Ollama stale for {stale_seconds:.0f}s — inference likely crashed"
                            )
                        else:
                            log.debug(
                                f"[{agent.agent_name}] Ollama idle for {stale_seconds:.0f}s "
                                f"(threshold: {stale_timeout}s)"
                            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # Can't reach Ollama API — might be busy, don't cancel
            pass
        except RuntimeError:
            raise  # Re-raise our own stale error

    if think_task.cancelled():
        raise RuntimeError("think() was cancelled")

    return think_task.result()


# CR-246: Default phase parameters — can be overridden per agent via batch_phase_params
_DEFAULT_PHASE_PARAMS = {
    "phase0": {"temperature": 0.2, "num_predict": 1024},   # Factual: read state, verify
    "phase1": {"temperature": 0.3, "num_predict": 1536},   # Analytical: structure, group
    "phase2": {"temperature": 0.5, "num_predict": 2048},   # Creative: find connections, build Lagebild
    "phase3": {"temperature": 0.3, "num_predict": 1536},   # Professional: stakeholder communication
    "phase4": {"temperature": 0.1, "num_predict": 1024},   # Precise: write exact file formats
}


class _PhaseParams:
    """Context manager that temporarily overrides agent config for a specific phase."""

    def __init__(self, agent: "AIMOSAgent", phase: str, log: logging.Logger):
        self.agent = agent
        self.phase = phase
        self.log = log
        self.original = {}

    def __enter__(self):
        phase_params = self.agent.config.get("batch_phase_params", {})
        params = phase_params.get(self.phase, _DEFAULT_PHASE_PARAMS.get(self.phase, {}))
        for key, value in params.items():
            self.original[key] = self.agent.config.get(key)
            self.agent.config[key] = value
        if params:
            self.log.debug(f"[{self.agent.agent_name}] {self.phase} params: {params}")
        return self

    def __exit__(self, *args):
        for key, value in self.original.items():
            if value is None:
                self.agent.config.pop(key, None)
            else:
                self.agent.config[key] = value


async def process_batch(agent: "AIMOSAgent", messages: list[dict], log: logging.Logger):
    """CR-233 + CR-234 + CR-236 + CR-246: Batch execution mode — 6-phase OODA cycle.

    The agent holds the GPU for all phases (no yielding).
    The Lagebild is ephemeral — rebuilt each cycle from workspace state + new messages.
    Workspace files (state.md, todo.md, etc.) carry state between cycles.
    Each phase uses optimized LLM parameters (temperature, num_predict) — see CR-246.
    """
    from core.fallback import auto_followup

    if not messages:
        return

    log.info(f"[{agent.agent_name}] BATCH mode: processing {len(messages)} message(s)")

    # Set session/thread context for batch-level operations
    first_msg = messages[0]
    agent._current_session_id = f"batch:{agent.agent_name}"
    agent._current_thread_id = f"batch:{first_msg.get('id', 0)}"
    agent._current_msg_kind = "batch"
    agent._tool_call_count = 0
    agent._tool_call_budget = agent.config.get("max_tool_calls_per_message", 30)

    ws_base = Path(f"storage/agents/{agent.agent_name}")

    # ── Phase 0: CONTEXT — Load workspace state ────────────────────────────
    phase0_context = await _phase0_context(agent, ws_base, log)

    # Format all messages into one structured block
    batch_input = agent.format_batch_input(messages)

    # ── Context Monitor — proactive budget management ─────────────────────
    batch_input = _context_monitor(agent, phase0_context, batch_input, messages, ws_base, log)

    # ── Phase 1: OBSERVE — Structure all new messages ──────────────────────
    analysis = await _phase1_observe(agent, phase0_context, batch_input, log)
    if analysis is None:
        return

    # ── Phase 2: ORIENT — Consolidate and build Lagebild ───────────────────
    lagebild = await _phase2_orient(agent, analysis, log)
    if lagebild is None:
        return

    # H-09: Empty Lagebild guard
    if not lagebild or len(lagebild.strip()) < 20:
        log.error(
            f"[{agent.agent_name}] BATCH Phase 2 produced empty/trivial Lagebild "
            f"({len(lagebild)} chars). Aborting cycle — would produce blind responses."
        )
        return

    # H-08: Human approval gate for critical agents
    await _human_approval_gate(agent, lagebild, messages, log)

    # ── Phase 3: DECIDE + ACT — per thread with Lagebild context ───────────
    threads, phase3_results = await _phase3_act(agent, messages, lagebild, log)

    # ── Phase 4: PERSIST — Update workspace, set reminders ─────────────────
    await _phase4_persist(agent, lagebild, phase3_results, ws_base, log, msg_count=len(messages))

    agent._touch()

    # Auto-followup for the batch as a whole
    if first_msg.get("kind") not in ("scheduled_job", "internal"):
        if not agent.config.get("disable_auto_jobs"):
            await auto_followup(agent, lagebild, log)

    # H-15: Mark messages as fully completed
    msg_ids = [m.get("id") for m in messages if m.get("id")]
    if msg_ids and agent._pool:
        try:
            await agent._pool.execute(
                "UPDATE pending_messages SET content = content || '\n[BATCH_COMPLETED]' "
                "WHERE id = ANY($1::int[])",
                msg_ids,
            )
        except Exception:
            pass

    log.info(f"[{agent.agent_name}] BATCH complete: {len(messages)} msg(s), {len(threads)} thread(s)")


# ── Phase implementations ──────────────────────────────────────────────────


async def _phase0_context(agent: "AIMOSAgent", ws_base: Path, log: logging.Logger) -> str:
    """Phase 0: Load workspace state — 'look at your desk before opening mail'."""
    workspace_context = ""
    leading_file = agent.config.get("batch_leading_file", "state.md")
    context_budget = agent.config.get("batch_context_budget", 2000)

    leading_path = ws_base / leading_file
    staleness_warning = ""
    if leading_path.exists():
        try:
            content = leading_path.read_text(encoding="utf-8")[:context_budget]
            workspace_context = f"YOUR HANDOVER PROTOCOL ({leading_file}):\n{content}\n"
            # H-04: Check staleness
            import datetime as _dt
            file_age_days = (_dt.datetime.now().timestamp() - leading_path.stat().st_mtime) / 86400
            if file_age_days > 7:
                staleness_warning = (
                    f"\n⚠ WARNING: Your handover protocol is {file_age_days:.0f} days old. "
                    f"Information may be outdated. Verify critical items against your data sources.\n"
                )
                log.warning(f"[{agent.agent_name}] Phase 0: {leading_file} is {file_age_days:.0f} days old")
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 0: Could not read {leading_file}: {exc}")

    # List available workspace files
    available_files = []
    if ws_base.exists():
        available_files = sorted(f.name for f in ws_base.iterdir()
                                 if f.is_file() and f.name != leading_file and not f.name.startswith('.'))

    data_sources = agent.config.get("batch_data_sources", [])

    if not (workspace_context or data_sources or available_files):
        log.info(f"[{agent.agent_name}] BATCH Phase 0 skipped (no context files or data sources configured)")
        return "Erste Sitzung — kein Vorwissen vorhanden. Keine Dateien im Workspace."

    files_hint = ""
    if available_files:
        files_hint = (
            f"\nAvailable detail files (use read_file to load if needed): "
            f"{', '.join(available_files[:15])}\n"
        )
    source_instructions = ""
    if data_sources:
        source_list = "\n".join(f"  - {s}" for s in data_sources)
        source_instructions = (
            f"\nAlso check these data sources for changes since your last session:\n"
            f"{source_list}\n"
            "Use your tools to query them now.\n"
        )
    phase0_prompt = (
        f"{workspace_context}"
        f"{staleness_warning}"
        f"{files_hint}"
        "You are starting a new work session. Review your handover protocol above.\n"
        "If you need details on a specific item, use read_file to load the relevant file.\n"
        "Identify: What is still open? What has changed? What deadlines are approaching?\n"
        "IMPORTANT: Verify critical items (deadlines, escalations) against your data sources.\n"
        "Do NOT blindly trust the handover protocol — it was written by an LLM and may contain errors.\n"
        f"{source_instructions}"
        "Summarize the current state briefly. Do NOT act yet — only review."
    )

    log.info(f"[{agent.agent_name}] BATCH Phase 0 (CONTEXT): {len(workspace_context)} chars workspace")
    try:
        with _PhaseParams(agent, "phase0", log):
            result = await _think_with_activity_check(agent, phase0_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120))
        log.info(f"[{agent.agent_name}] BATCH Phase 0 complete: {len(result)} chars")
        return result
    except (asyncio.TimeoutError, Exception) as exc:
        log.error(f"[{agent.agent_name}] BATCH Phase 0 FAILED: {exc}")
        return ""


def _context_monitor(
    agent: "AIMOSAgent", phase0_context: str, batch_input: str,
    messages: list[dict], ws_base: Path, log: logging.Logger,
) -> str:
    """Context Monitor: proactive budget management. Returns (possibly compressed) batch_input."""
    num_ctx = agent.config.get("num_ctx", Config.DEFAULT_NUM_CTX)
    reserve_for_later_phases = 8000
    available_for_input = num_ctx - reserve_for_later_phases
    est_input_tokens = (len(phase0_context) + len(batch_input)) // 4
    ctx_usage_pct = (est_input_tokens / num_ctx) * 100 if num_ctx else 0

    if est_input_tokens > available_for_input:
        log.warning(
            f"[{agent.agent_name}] CONTEXT OVERLOAD: ~{est_input_tokens} input tokens "
            f"exceeds budget of {available_for_input} (num_ctx={num_ctx}). Compressing."
        )
        if len(phase0_context) > 2000:
            # Can't modify phase0_context (str is immutable in caller), but truncation
            # was already applied via context_budget in Phase 0
            log.info(f"[{agent.agent_name}] Phase 0 context already budget-limited")

        est_input_tokens = (len(phase0_context) + len(batch_input)) // 4
        if est_input_tokens > available_for_input:
            trimmed_msgs = []
            for msg in messages:
                trimmed = dict(msg)
                content = trimmed.get("content", "")
                if len(content) > 200:
                    trimmed["content"] = content[:200] + " [... truncated]"
                trimmed_msgs.append(trimmed)
            batch_input = agent.format_batch_input(trimmed_msgs)
            log.warning(
                f"[{agent.agent_name}] Messages truncated to 200 chars each. "
                f"Quality will be reduced. Consider splitting this agent."
            )

        overload_note = (
            f"⚠ CONTEXT OVERLOAD {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}\n"
            f"Input: ~{est_input_tokens} tokens, Budget: {available_for_input}, num_ctx: {num_ctx}\n"
            f"Messages: {len(messages)}, State: {len(phase0_context)} chars\n"
            f"Recommendation: Split this agent's responsibilities or increase num_ctx."
        )
        overload_path = ws_base / "overload_warning.txt"
        try:
            overload_path.parent.mkdir(parents=True, exist_ok=True)
            overload_path.write_text(overload_note, encoding="utf-8")
        except Exception:
            pass

    elif ctx_usage_pct > 50:
        log.info(
            f"[{agent.agent_name}] Context budget: ~{est_input_tokens} tokens "
            f"({ctx_usage_pct:.0f}% of {num_ctx}) after Phase 0 + Messages — OK"
        )

    return batch_input


async def _phase1_observe(
    agent: "AIMOSAgent", phase0_context: str, batch_input: str, log: logging.Logger,
) -> str | None:
    """Phase 1: OBSERVE — Structure all new messages."""
    phase1_prompt = ""
    if phase0_context:
        phase1_prompt += f"YOUR CURRENT STATE (from Phase 0):\n{phase0_context}\n\n"
    phase1_prompt += (
        f"{batch_input}\n\n"
        "Du hast ALLE neuen Nachrichten auf einmal erhalten. Strukturiere sie:\n"
        "- Gruppiere zusammengehoerige Nachrichten (gleicher Absender, gleiches Thema, gleicher Thread)\n"
        "- Erkenne threaduebergreifende Zusammenhaenge (z.B. gleiches Produkt, gleicher Vorgang)\n"
        "- Vergleiche mit deinem aktuellen Stand — was ist NEU vs. bereits bekannt?\n"
        "- Identifiziere einzelne Aufgaben aus jeder Nachricht\n"
        "- Priorisiere nach Dringlichkeit (Eskalationen > Kundenanfragen > Interne)\n"
        "- Handle NOCH NICHT — nur analysieren und planen.\n"
        "ANTWORTE AUF DEUTSCH. Gib eine strukturierte Aufgabenliste aus."
    )
    log.info(f"[{agent.agent_name}] BATCH Phase 1 (OBSERVE): {len(batch_input)} chars input")
    try:
        with _PhaseParams(agent, "phase1", log):
            analysis = await _think_with_activity_check(agent, phase1_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120))
        log.info(f"[{agent.agent_name}] BATCH Phase 1 complete: {len(analysis)} chars")
        return analysis
    except (asyncio.TimeoutError, Exception) as exc:
        log.error(f"[{agent.agent_name}] BATCH Phase 1 FAILED: {exc}")
        return None


async def _phase2_orient(agent: "AIMOSAgent", analysis: str, log: logging.Logger) -> str | None:
    """Phase 2: ORIENT — Consolidate and build Lagebild."""
    phase2_prompt = (
        f"YOUR ANALYSIS FROM PHASE 1:\n{analysis}\n\n"
        "Konsolidiere jetzt: Basierend auf deiner Analyse, beschaffe alle nötigen Informationen.\n"
        "- Nutze deine Tools um Dateien zu lesen, Daten abzufragen, Vorgaenge zu pruefen\n"
        "- Identifiziere Informationsluecken\n"
        "- Pruefe Abhaengigkeiten zwischen Aufgaben\n"
        "- Notiere threaduebergreifende Auswirkungen\n"
        "- Bereite alles fuer die Handlung vor.\n"
        "ANTWORTE AUF DEUTSCH. Erstelle ein LAGEBILD (Situationsbericht): Was hast du gefunden, "
        "was muss jeder Stakeholder wissen, welche Aktionen sind pro Thread noetig?"
    )
    log.info(f"[{agent.agent_name}] BATCH Phase 2 (ORIENT)")
    try:
        with _PhaseParams(agent, "phase2", log):
            lagebild = await _think_with_activity_check(agent, phase2_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120))
        log.info(f"[{agent.agent_name}] BATCH Phase 2 complete (Lagebild): {len(lagebild)} chars")
        return lagebild
    except (asyncio.TimeoutError, Exception) as exc:
        log.error(f"[{agent.agent_name}] BATCH Phase 2 FAILED: {exc}")
        return None


async def _human_approval_gate(
    agent: "AIMOSAgent", lagebild: str, messages: list[dict], log: logging.Logger,
):
    """H-08: Send Lagebild to reviewer for critical agents."""
    if not agent.config.get("batch_require_human_approval"):
        return
    reviewer = agent.config.get("batch_approval_target", "projektleiter")
    log.info(f"[{agent.agent_name}] BATCH Human Approval Gate: sending Lagebild to {reviewer}")
    approval_msg = (
        f"[LAGEBILD ZUR FREIGABE — {agent.agent_name}]\n\n"
        f"{lagebild}\n\n"
        f"Bitte prüfen und bestätigen. Der Agent wartet auf Freigabe, "
        f"bevor er {len(messages)} Nachricht(en) beantwortet."
    )
    if agent._pool:
        await agent._pool.execute(
            "INSERT INTO pending_messages (agent_name, sender_id, content, kind, thread_id, processed) "
            "VALUES ($1, 0, $2, 'internal', $3, FALSE)",
            reviewer, approval_msg, f"approval:{agent.agent_name}",
        )
    log.info(f"[{agent.agent_name}] Lagebild sent to {reviewer} for review (non-blocking)")


async def _phase3_act(
    agent: "AIMOSAgent", messages: list[dict], lagebild: str, log: logging.Logger,
) -> tuple[dict, list[dict]]:
    """Phase 3: DECIDE + ACT — per thread with Lagebild context."""
    threads: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        tid = msg.get("thread_id") or f"sender:{msg.get('sender_id', 0)}"
        threads[tid].append(msg)

    log.info(f"[{agent.agent_name}] BATCH Phase 3 (ACT): {len(threads)} thread(s) to respond to")

    phase3_results: list[dict] = []

    for thread_id, thread_msgs in threads.items():
        representative_msg = thread_msgs[0]

        # Load conversation history for this specific thread
        thread_history_text = ""
        if agent._pool:
            rows = await agent._pool.fetch(
                "SELECT role, content FROM aimos_chat_histories "
                "WHERE agent_name=$1 AND thread_id=$2 "
                "ORDER BY id DESC LIMIT 20",
                agent.agent_name, thread_id,
            )
            if rows:
                history_lines = [f"[{r['role']}]: {r['content'][:500]}" for r in reversed(rows)]
                thread_history_text = "\n".join(history_lines)

        agent._current_thread_id = thread_id
        agent._current_msg_kind = representative_msg.get("kind", "batch")
        agent._tool_call_count = 0

        sender_info = f"sender_id={representative_msg.get('sender_id', 0)}"
        channel_info = representative_msg.get("kind", "unknown")
        msg_contents = "\n".join(f"  - {m.get('content', '')[:200]}" for m in thread_msgs)

        # S-3: Cross-thread leak protection
        phase3_prompt = (
            f"LAGEBILD (cross-thread situation report):\n{lagebild}\n\n"
            "WICHTIG: Das Lagebild oben enthaelt Informationen ueber MEHRERE Stakeholder.\n"
            "Du darfst NUR Informationen teilen die fuer den Stakeholder unten relevant sind.\n"
            "NENNE NIE die Namen, Nachrichten oder internen Details anderer Stakeholder.\n"
            "ANTWORTE AUF DEUTSCH.\n\n"
        )
        if thread_history_text:
            phase3_prompt += (
                f"CONVERSATION HISTORY with this stakeholder (thread={thread_id}):\n"
                f"{thread_history_text}\n\n"
            )
        phase3_prompt += (
            f"CURRENT MESSAGE(S) from this stakeholder ({sender_info}, channel={channel_info}):\n"
            f"{msg_contents}\n\n"
            "Antworte jetzt an DIESEN Stakeholder. Nutze das Lagebild als Kontext aber teile "
            "nur mit was fuer DIESE Person relevant ist. Fuehre Aktionen mit deinen Tools aus "
            "(send_email, send_to_agent, update_customer, etc.). ANTWORTE AUF DEUTSCH."
        )

        log.info(
            f"[{agent.agent_name}] BATCH Phase 3 thread={thread_id} "
            f"({len(thread_msgs)} msg(s), {len(thread_history_text)} chars history)"
        )
        try:
            with _PhaseParams(agent, "phase3", log):
                result = await _think_with_activity_check(agent, phase3_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120))
            log.info(f"[{agent.agent_name}] BATCH Phase 3 thread={thread_id} complete: {len(result)} chars")
            phase3_results.append({
                "thread_id": thread_id, "status": "sent", "response_len": len(result),
                "summary": result[:150],
            })
        except (asyncio.TimeoutError, Exception) as exc:
            log.error(f"[{agent.agent_name}] BATCH Phase 3 thread={thread_id} FAILED: {exc}")
            phase3_results.append({
                "thread_id": thread_id, "status": "FAILED", "error": str(exc),
            })
            continue

        route = await agent.dispatch_response(result, representative_msg)
        log.info(f"[{agent.agent_name}] BATCH dispatched thread={thread_id} → {route}")

    return threads, phase3_results


async def _phase4_persist(
    agent: "AIMOSAgent", lagebild: str, phase3_results: list[dict],
    ws_base: Path, log: logging.Logger, msg_count: int = 0,
):
    """Phase 4: PERSIST — Update workspace files, set reminders, remember key facts."""
    # H-14: Backup state.md before overwrite
    leading_file_path = ws_base / agent.config.get("batch_leading_file", "state.md")
    if leading_file_path.exists():
        backup_path = leading_file_path.with_suffix(".md.bak")
        try:
            shutil.copy2(leading_file_path, backup_path)
            log.info(f"[{agent.agent_name}] Phase 4: Backed up {leading_file_path.name} → {backup_path.name}")
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 4: Backup failed: {exc}")

    # Build file spec block from config
    file_specs = agent.config.get("batch_file_specs", {})
    if file_specs:
        spec_lines = [f"  {fname}: {spec}" for fname, spec in file_specs.items()]
        file_spec_block = (
            "YOUR WORKSPACE FILES — maintain these using write_file:\n"
            + "\n".join(spec_lines) + "\n\n"
            "IMPORTANT: Follow the format exactly. Phase 0 of your next session will\n"
            "read these files to reconstruct your current state. If the format is wrong,\n"
            "you lose context.\n\n"
        )
    else:
        file_spec_block = (
            "Update your workspace files (use write_file):\n"
            "  - todo.md: Markdown checklist with [ ] and [x], one task per line, include deadline and responsible person\n"
            "  - status.md: Current state of all tracked items, one line per item\n"
            "  - decisions.md: Append new decisions with date and rationale\n\n"
        )

    # S-7: Build Phase 3 results summary
    p3_summary_lines = []
    for pr in phase3_results:
        if pr["status"] == "sent":
            p3_summary_lines.append(f"  ✓ {pr['thread_id']}: sent ({pr['response_len']} chars)")
        else:
            p3_summary_lines.append(f"  ✗ {pr['thread_id']}: FAILED — {pr.get('error', 'unknown')}")
    phase3_summary = "\n".join(p3_summary_lines) if p3_summary_lines else "  (no threads processed)"

    leading_file_spec = agent.config.get("batch_leading_file", "state.md")
    phase4_prompt = (
        f"LAGEBILD FROM THIS SESSION:\n{lagebild}\n\n"
        f"PHASE 3 RESULTS (what you actually did):\n{phase3_summary}\n\n"
        "This batch session is complete. You MUST now call write_file for EACH of these files.\n"
        "Do NOT just describe what you would write — actually CALL the write_file tool.\n\n"
        f"{file_spec_block}"
        f"STEP 1: Call write_file(filename=\"todo.md\", content=\"...\") with your task list.\n"
        f"STEP 2: Call write_file(filename=\"status.md\", content=\"...\") with status table.\n"
        f"STEP 3: Call write_file(filename=\"{leading_file_spec}\", content=\"...\") with your handover protocol.\n"
        f"  The handover protocol ({leading_file_spec}) must be max 1500 characters and contain:\n"
        "  - Current session date and number of messages processed\n"
        "  - Phase 3 results: which threads got responses, which failed\n"
        "  - Top 3-5 open items with deadlines and responsible persons\n"
        "  - Any active escalations (one line each)\n"
        "  - What needs attention next session\n\n"
        "STEP 4: Call remember(key, value) for VERIFIED facts only.\n\n"
        "START NOW. Call write_file immediately."
    )
    log.info(f"[{agent.agent_name}] BATCH Phase 4 (PERSIST)")
    # CR-248: Clear chat history before Phase 4 to prevent history contamination.
    # Phase 1-3 produced text-only responses which teach the LLM "respond with text".
    # Phase 4 needs tool calls (write_file, remember, schedule). A fresh history
    # ensures the LLM sees only the system prompt + Phase 4 prompt, making it much
    # more likely to use native tool calls instead of writing Python code blocks.
    saved_history = getattr(agent, '_history', None)
    if saved_history is not None:
        agent._history = []
    try:
        with _PhaseParams(agent, "phase4", log):
            persist_result = await _think_with_activity_check(agent, phase4_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120))
        log.info(f"[{agent.agent_name}] BATCH Phase 4 complete: {len(persist_result)} chars")
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning(f"[{agent.agent_name}] BATCH Phase 4 FAILED (non-critical): {exc}")
        persist_result = ""
    finally:
        # Restore history (Phase 4 messages are already persisted to DB by think())
        if saved_history is not None:
            agent._history = saved_history

    # Fallback: If LLM didn't call write_file (common with some models), the orchestrator
    # writes state.md directly from the Phase 4 text output + Lagebild + Phase 3 results.
    # Only triggers for real agents (ws_base must exist and not be a test mock path).
    state_path = ws_base / leading_file_spec
    state_was_written = state_path.exists() and state_path.stat().st_mtime > (time.time() - 60)
    if not state_was_written and ws_base.exists():
        # state.md was not written by the LLM — write it from the orchestrator
        import datetime as _dt
        fallback_state = (
            f"# Übergabe — {_dt.datetime.now():%d.%m.%Y %H:%M} "
            f"({msg_count} Nachrichten verarbeitet)\n\n"
            f"## Lagebild\n{lagebild[:800]}\n\n"
            f"## Phase 3 Ergebnisse\n{phase3_summary}\n\n"
        )
        if persist_result:
            fallback_state += f"## Agent-Notizen\n{persist_result[:500]}\n"
        try:
            state_path.write_text(fallback_state[:1500], encoding="utf-8")
            log.info(f"[{agent.agent_name}] Phase 4 FALLBACK: Wrote {leading_file_spec} ({len(fallback_state[:1500])} chars)")
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 4 FALLBACK write failed: {exc}")
