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


# ── CR-274: Confidentiality Scope System ───────────────────────────────────

def _resolve_scope(thread_id: str, config: dict) -> str:
    """Map a thread_id to a confidentiality scope. Pure Python, no LLM.

    The scope determines which Lagebild partition a stakeholder sees in Phase 4.
    Modes (batch_scope_pattern):
      "email_address" (default): email:foo@bar.com → scope:foo@bar.com
      "thread_id": scope = thread_id verbatim
      "config_map": explicit mapping from batch_scope_map config
    """
    mode = config.get("batch_scope_pattern", "email_address")
    if mode == "config_map":
        scope_map = config.get("batch_scope_map", {})
        return scope_map.get(thread_id, f"scope:{thread_id}")
    if mode == "email_address" and thread_id.startswith("email:"):
        return f"scope:{thread_id[6:]}"
    return f"scope:{thread_id}"


def _group_messages_by_scope(
    messages: list[dict], config: dict,
) -> dict[str, list[dict]]:
    """Group messages by confidentiality scope."""
    scopes: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        tid = msg.get("thread_id") or f"sender:{msg.get('sender_id', 0)}"
        scope = _resolve_scope(tid, config)
        scopes[scope].append(msg)
    return dict(scopes)


def _partition_lagebild(
    lagebild: str, scope_names: list[str],
) -> dict[str, str]:
    """Split a scope-tagged Lagebild into per-scope partitions.

    Expects the Lagebild to contain sections like:
      ## [SCOPE: scope:foo@bar.com]
      ...content...
      ## [SCOPE: scope:baz@bar.com]
      ...content...

    Returns {"scope:foo@bar.com": "...content...", "scope:baz@bar.com": "..."}.
    If no scope headers found, returns {"_all": lagebild} (fallback).
    """
    import re
    pattern = re.compile(r'^##\s*\[SCOPE:\s*([^\]]+)\]', re.MULTILINE)
    matches = list(pattern.finditer(lagebild))
    if not matches:
        return {"_all": lagebild}

    partitions: dict[str, str] = {}
    for i, match in enumerate(matches):
        scope_key = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(lagebild)
        partitions[scope_key] = lagebild[start:end].strip()

    return partitions


# ── CR-258: Workspace document scanning ────────────────────────────────────

_DOCUMENT_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp", ".heic", ".heif",
    ".docx", ".xlsx", ".csv", ".txt",
}
_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}


def _extract_archives(doc_dir: Path, log: logging.Logger, agent_name: str) -> int:
    """CR-258: Auto-extract archives in dokumente/ folder.

    Extracts ZIP, tar.gz, 7z, RAR into a subfolder named after the archive.
    Returns number of archives extracted. Skips already-extracted archives
    (subfolder with same name already exists).
    """
    extracted = 0
    if not doc_dir.exists():
        return 0
    for f in list(doc_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in _ARCHIVE_EXTENSIONS:
            continue
        # Skip if already extracted (folder with same stem exists)
        target_dir = doc_dir / f.stem
        if target_dir.exists():
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            suffix = f.suffix.lower()
            if suffix == ".zip":
                import zipfile
                with zipfile.ZipFile(f, 'r') as zf:
                    zf.extractall(target_dir)
            elif suffix in {".tar", ".gz", ".tgz", ".bz2"}:
                import tarfile
                with tarfile.open(f, 'r:*') as tf:
                    tf.extractall(target_dir, filter='data')
            elif suffix == ".7z":
                import subprocess
                subprocess.run(["7z", "x", str(f), f"-o{target_dir}"], capture_output=True, timeout=60)
            elif suffix == ".rar":
                import subprocess
                subprocess.run(["unrar", "x", str(f), str(target_dir)], capture_output=True, timeout=60)
            else:
                target_dir.rmdir()
                continue
            extracted += 1
            log.info(f"[{agent_name}] CR-258: Extracted archive {f.name} → {target_dir.name}/")
        except Exception as exc:
            log.warning(f"[{agent_name}] CR-258: Failed to extract {f.name}: {exc}")
            if target_dir.exists() and not any(target_dir.iterdir()):
                target_dir.rmdir()
    return extracted


def _scan_workspace_documents(ws_base: Path, log: logging.Logger, agent_name: str) -> list[dict]:
    """CR-258: Scan workspace for new/unprocessed documents.

    Auto-extracts archives first, then scans for document files.
    Returns list of document descriptors for files in the workspace 'dokumente/'
    subdirectory (or client subdirs like klient_*/dokumente/).
    A file is considered processed if its name appears in arbeitsdatei.md or state.md.
    """
    doc_dirs = [ws_base / "dokumente"]
    # Also check client subdirectories (steuerberater pattern: klient_*/dokumente/)
    if ws_base.exists():
        for d in ws_base.iterdir():
            if d.is_dir() and (d / "dokumente").is_dir():
                doc_dirs.append(d / "dokumente")

    # Auto-extract archives before scanning
    for doc_dir in doc_dirs:
        if doc_dir.exists():
            _extract_archives(doc_dir, log, agent_name)

    documents = []
    for doc_dir in doc_dirs:
        if not doc_dir.exists():
            continue
        # Recursive scan (includes extracted archive subdirectories)
        for f in sorted(doc_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in _DOCUMENT_EXTENSIONS:
                documents.append({
                    "path": f,
                    "name": f.name,
                    "size_kb": f.stat().st_size // 1024,
                    "modified": f.stat().st_mtime,
                    "client_dir": f.parent.parent.name if f.parent.name == "dokumente" else None,
                })

    if not documents:
        return []

    # Check which are already processed — ONLY check arbeitsdatei.md
    # (state.md may mention filenames in setup text without having processed them)
    processed_names = set()
    for check_file in ["arbeitsdatei.md"]:
        check_path = ws_base / check_file
        if check_path.exists():
            try:
                content = check_path.read_text(encoding="utf-8")
                for doc in documents:
                    if doc["name"] in content:
                        processed_names.add(doc["name"])
            except Exception:
                pass

    new_docs = [d for d in documents if d["name"] not in processed_names]
    if new_docs:
        log.info(
            f"[{agent_name}] CR-258 Workspace scan: {len(new_docs)} new document(s) "
            f"({len(documents)} total, {len(processed_names)} already processed)"
        )
    return new_docs


def _chunk_document_text(text: str, chunk_size: int = 2000) -> list[str]:
    """CR-258: Split document text into chunks for iterative processing.

    Splits at paragraph boundaries when possible, falls back to hard split.
    chunk_size is in characters (not tokens).
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        # Try to split at a paragraph boundary
        split_pos = remaining.rfind("\n\n", 0, chunk_size)
        if split_pos < chunk_size // 2:
            split_pos = remaining.rfind("\n", 0, chunk_size)
        if split_pos < chunk_size // 2:
            split_pos = chunk_size
        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:].lstrip("\n")

    return chunks


# ── CR-247: Activity-based timeout instead of hard timeout ─────────────────

async def _think_with_activity_check(
    agent: "AIMOSAgent", prompt: str, log: logging.Logger,
    stale_timeout: int = 120, poll_interval: int = 15,
    hard_timeout: int = 600, phase: str | None = None,
) -> str:
    """Call agent.think() with Ollama activity monitoring AND hard safety timeout.

    Instead of killing a think() call after N seconds regardless of progress,
    this checks whether Ollama is still actively generating. A call that takes
    20 minutes but is still producing tokens will NOT be killed. A call where
    Ollama has stopped responding for stale_timeout seconds WILL be killed.

    CR-270 hardening: Added hard_timeout as absolute wall-clock limit (default 600s)
    and consecutive API failure tracking. The /api/ps endpoint only tells us if a
    model is loaded, not if it's generating — so a model stuck mid-inference still
    appears "active". The hard_timeout catches this case.

    CR-273: Added phase parameter for OODA tool filtering. When set, agent.think()
    will filter the tool list to only those allowed in the given OODA phase.

    Args:
        stale_timeout: Seconds without Ollama activity before cancelling (default: 120)
        poll_interval: How often to check Ollama status (default: 15s)
        hard_timeout: Absolute max wall-clock seconds per call (default: 600)
        phase: OODA phase ("0"-"5") for tool filtering, or None to skip filtering
    """
    # CR-273: Set phase on agent so think() can filter tools
    prev_phase = getattr(agent, '_ooda_phase', None)
    agent._ooda_phase = phase

    think_task = asyncio.create_task(agent.think(prompt))
    ollama_url = f"{Config.LLM_BASE_URL}/api/ps"
    last_active = time.monotonic()
    start_time = time.monotonic()
    consecutive_api_failures = 0
    _MAX_API_FAILURES = 5  # Cancel after 5 consecutive unreachable polls (~75s)

    while not think_task.done():
        # Wait but check completion frequently
        for _ in range(poll_interval):
            if think_task.done():
                break
            await asyncio.sleep(1)

        if think_task.done():
            break

        elapsed = time.monotonic() - start_time

        # CR-270: Hard timeout — absolute safety net against system freeze.
        # Even if Ollama reports "active", no single think() call should run
        # longer than hard_timeout. The 2026-04-02 freeze showed eval times
        # escalating 23s → 83s → 107s → ∞ while the model appeared "active".
        if elapsed > hard_timeout:
            log.error(
                f"[{agent.agent_name}] HARD TIMEOUT: think() running for {elapsed:.0f}s "
                f"(limit={hard_timeout}s) — killing to prevent system freeze"
            )
            think_task.cancel()
            try:
                await think_task
            except asyncio.CancelledError:
                pass
            raise RuntimeError(
                f"Hard timeout after {elapsed:.0f}s — inference likely stuck "
                f"(VRAM exhaustion or model hang)"
            )

        # Check if Ollama still has an active model
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ollama_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    models = data.get("models", [])
                    consecutive_api_failures = 0  # Reset on success
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
            # CR-270: Track consecutive API failures instead of silently ignoring.
            # If Ollama itself is frozen/unresponsive, the old code would loop
            # forever because /api/ps kept timing out and stale_timeout never fired.
            consecutive_api_failures += 1
            log.warning(
                f"[{agent.agent_name}] Ollama API unreachable "
                f"({consecutive_api_failures}/{_MAX_API_FAILURES})"
            )
            if consecutive_api_failures >= _MAX_API_FAILURES:
                log.error(
                    f"[{agent.agent_name}] Ollama API unreachable {consecutive_api_failures}x "
                    f"in a row — system likely frozen, cancelling think()"
                )
                think_task.cancel()
                try:
                    await think_task
                except asyncio.CancelledError:
                    pass
                raise RuntimeError(
                    f"Ollama API unreachable {consecutive_api_failures}x — "
                    f"system freeze detected"
                )
        except RuntimeError:
            raise  # Re-raise our own stale/timeout error

    # CR-273: Restore previous phase (cleanup)
    agent._ooda_phase = prev_phase

    if think_task.cancelled():
        raise RuntimeError("think() was cancelled")

    return think_task.result()


# CR-273: 6-Phase OODA default parameters
# Phase numbering: 0=KONTEXT, 1=OBSERVE, 2=ORIENT, 3=DECIDE, 4=ACT, 5=PERSIST
_DEFAULT_PHASE_PARAMS = {
    "phase0": {"temperature": 0.2, "num_predict": 1024},   # KONTEXT: Factual, read state
    "phase1": {"temperature": 0.3, "num_predict": 1536},   # OBSERVE: Analytical, structure
    "phase2": {"temperature": 0.5, "num_predict": 2048},   # ORIENT: Creative, Lagebild + Chunk-Analyse
    "phase3": {"temperature": 0.4, "num_predict": 1536},   # DECIDE: Analytical, stakeholder plan
    "phase4": {"temperature": 0.3, "num_predict": 2048},   # ACT: Professional, draft responses
    "phase5": {"temperature": 0.1, "num_predict": 1024},   # PERSIST: Precise, write exact formats
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
    """CR-273: 6-Phase OODA Batch Cycle.

    Phase 0: KONTEXT  — Load workspace, sync data sources
    Phase 1: OBSERVE  — Structure inputs (messages + document inventory)
    Phase 2: ORIENT   — Build Lagebild (incl. document chunk analysis loop)
    Phase 3: DECIDE   — Identify stakeholders, create action plan
    Phase 4: ACT      — Draft responses + generate documents → Orchestrator dispatches
    Phase 5: PERSIST  — Guaranteed self-dispatch: save state (runs in finally-block)

    Architecture: LLM drafts, Orchestrator dispatches. COMMUNICATE tools are NOT
    in the LLM tool-set. Phase 5 PERSIST always runs, even if Phase 4 crashes.
    """
    from core.fallback import auto_followup

    if not messages:
        return

    log.info(f"[{agent.agent_name}] BATCH mode: processing {len(messages)} message(s)")

    # H-25 / CR-270: Clear in-memory history at batch start.
    # Without this, Phase 0-1 inherit stale history from the previous batch cycle
    # (agent is reused between cycles without restart).
    agent._history = []

    # Set session/thread context for batch-level operations
    first_msg = messages[0]
    agent._current_session_id = f"batch:{agent.agent_name}"
    agent._current_thread_id = f"batch:{first_msg.get('id', 0)}"
    agent._current_msg_kind = "batch"
    agent._tool_call_count = 0
    agent._tool_call_budget = agent.config.get("max_tool_calls_per_message", 30)

    ws_base = Path(f"storage/agents/{agent.agent_name}")

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 0: KONTEXT — Load workspace, sync data sources
    # ══════════════════════════════════════════════════════════════════════
    phase0_context = await _phase0_context(agent, ws_base, log)

    # Dropbox/SharePoint sync (part of Phase 0)
    dropbox_path = agent.config.get("batch_dropbox_path")
    if dropbox_path and hasattr(agent, '_skills'):
        try:
            dropbox_skill = agent._skills.get("dropbox")
            if dropbox_skill and dropbox_skill.is_available():
                sync_result = await dropbox_skill._sync_folder(dropbox_path, "dokumente")
                log.info(f"[{agent.agent_name}] Phase 0: Dropbox sync: {sync_result[:100]}")
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 0: Dropbox sync failed (non-critical): {exc}")

    # Workspace document scan (part of Phase 0)
    new_documents = []
    if agent.config.get("batch_workspace_scan", False):
        new_documents = _scan_workspace_documents(ws_base, log, agent.agent_name)

    # Format messages + document inventory for Phase 1
    batch_input = agent.format_batch_input(messages)
    if new_documents:
        doc_list = "\n".join(
            f"  - {d['name']} ({d['size_kb']} KB, client={d.get('client_dir', '-')})"
            for d in new_documents
        )
        batch_input += (
            f"\n\n--- NEW DOCUMENTS ON YOUR DESK ---\n"
            f"The following {len(new_documents)} document(s) arrived since your last session:\n"
            f"{doc_list}\n"
            f"These need to be analyzed during the ORIENT phase.\n"
        )

    # Context budget management
    batch_input = _context_monitor(agent, phase0_context, batch_input, messages, ws_base, log)

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 1: OBSERVE — Structure all inputs
    # ══════════════════════════════════════════════════════════════════════
    analysis = await _phase1_observe(agent, phase0_context, batch_input, log)
    if analysis is None:
        return

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 2: ORIENT — Build Lagebild (incl. document chunk analysis)
    # ══════════════════════════════════════════════════════════════════════

    # Phase 2a: Document chunk analysis loop (if documents present)
    doc_results = []
    if new_documents:
        # Preliminary Lagebild from Phase 1 analysis (before docs)
        preliminary_lagebild = analysis  # Use analysis as context for chunk processing
        doc_results = await _phase3c_documents(
            agent, new_documents, preliminary_lagebild, ws_base, log,
        )

    # CR-274: Resolve confidentiality scopes
    confidentiality = agent.config.get("batch_confidentiality", "none")
    scope_names: list[str] = []
    if confidentiality == "isolated":
        scope_groups = _group_messages_by_scope(messages, agent.config)
        scope_names = list(scope_groups.keys())
        log.info(
            f"[{agent.agent_name}] CR-274: Confidentiality=isolated, "
            f"{len(scope_names)} scope(s): {scope_names}"
        )

    # Phase 2b: Lagebild consolidation (with document findings)
    lagebild = await _phase2_orient(agent, analysis, log, scope_names=scope_names or None)
    if lagebild is None:
        return

    # H-09: Empty Lagebild guard
    if not lagebild or len(lagebild.strip()) < 20:
        log.error(
            f"[{agent.agent_name}] Phase 2 ORIENT: empty Lagebild "
            f"({len(lagebild)} chars). Aborting — would produce blind responses."
        )
        return

    # CR-274: Partition Lagebild by scope (for Phase 4 isolation)
    lagebild_partitions: dict[str, str] = {"_all": lagebild}
    if confidentiality == "isolated" and len(scope_names) > 1:
        lagebild_partitions = _partition_lagebild(lagebild, scope_names)
        if "_all" not in lagebild_partitions:
            lagebild_partitions["_all"] = lagebild  # Keep full version for DECIDE + PERSIST
        log.info(
            f"[{agent.agent_name}] CR-274: Lagebild partitioned into "
            f"{len(lagebild_partitions) - 1} scope(s) + _all"
        )

    # H-08: Human approval gate for critical agents
    await _human_approval_gate(agent, lagebild, messages, log)

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 3: DECIDE — Stakeholder plan
    # ══════════════════════════════════════════════════════════════════════
    stakeholder_plan = await _phase2b_decide(agent, lagebild, messages, log)

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 4: ACT — Draft → Validate → Dispatch (Orchestrator)
    # ══════════════════════════════════════════════════════════════════════
    threads = {}
    phase4_results = []
    try:
        threads, phase4_results = await _phase3_act(
            agent, messages, lagebild, log,
            stakeholder_plan=stakeholder_plan,
            lagebild_partitions=lagebild_partitions,
        )
    except Exception as exc:
        log.error(f"[{agent.agent_name}] Phase 4 ACT failed: {exc}")
        # Phase 5 PERSIST still runs (finally-block below)

    # ══════════════════════════════════════════════════════════════════════
    #  Phase 5: PERSIST — Guaranteed self-dispatch (always runs)
    # ══════════════════════════════════════════════════════════════════════
    if agent.config.get("batch_persist", True):
        try:
            await _phase4_persist(agent, lagebild, phase4_results + doc_results, ws_base, log, msg_count=len(messages))
        except Exception as exc:
            log.error(f"[{agent.agent_name}] Phase 5 PERSIST failed: {exc}")
    else:
        log.info(f"[{agent.agent_name}] Phase 5 PERSIST skipped (batch_persist=false)")

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
        return "First session — no prior knowledge. No files in workspace."

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
            result = await _think_with_activity_check(agent, phase0_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120), phase="0")
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
        "You received ALL new messages at once. Structure them:\n"
        "- Group related messages (same sender, same topic, same thread)\n"
        "- Identify cross-thread connections (e.g. same product, same process)\n"
        "- Compare with your current state — what is NEW vs. already known?\n"
        "- Identify individual tasks from each message\n"
        "- Prioritize by urgency (escalations > customer requests > internal)\n"
        "- Do NOT act yet — only analyze and plan.\n"
        "Output a structured task list."
    )
    log.info(f"[{agent.agent_name}] BATCH Phase 1 (OBSERVE): {len(batch_input)} chars input")
    try:
        with _PhaseParams(agent, "phase1", log):
            analysis = await _think_with_activity_check(agent, phase1_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120), phase="1")
        log.info(f"[{agent.agent_name}] BATCH Phase 1 complete: {len(analysis)} chars")
        return analysis
    except (asyncio.TimeoutError, Exception) as exc:
        log.error(f"[{agent.agent_name}] BATCH Phase 1 FAILED: {exc}")
        return None


async def _phase2_orient(
    agent: "AIMOSAgent", analysis: str, log: logging.Logger,
    scope_names: list[str] | None = None,
) -> str | None:
    """Phase 2: ORIENT — Consolidate and build Lagebild.

    CR-274: When scope_names is provided (isolated mode), the prompt instructs the
    LLM to structure the Lagebild with ## [SCOPE: ...] headers per scope. This
    enables _partition_lagebild() to split the result for Phase 4.
    """
    scope_instruction = ""
    if scope_names and len(scope_names) > 1:
        scope_list = "\n".join(f"  - {s}" for s in scope_names)
        scope_instruction = (
            f"\nIMPORTANT: This batch contains {len(scope_names)} SEPARATE confidential scopes.\n"
            f"Structure your Lagebild with one section per scope, using this exact header format:\n"
            f"## [SCOPE: scope_name]\n"
            f"The scopes are:\n{scope_list}\n"
            f"NEVER mix information between scopes. Each section must be self-contained.\n\n"
        )

    phase2_prompt = (
        f"YOUR ANALYSIS FROM PHASE 1:\n{analysis}\n\n"
        "Now consolidate and write a LAGEBILD (situation report) as TEXT.\n"
        "Based on your analysis:\n"
        "- What is the current situation?\n"
        "- What are the dependencies between tasks?\n"
        "- What does each stakeholder need to know?\n"
        "- What actions are needed per thread?\n\n"
        f"{scope_instruction}"
        "You may use tools (read_file, search) to gather additional information if needed, "
        "but your OUTPUT must be a written situation report, not just tool calls.\n"
        "Write the Lagebild NOW as structured text."
    )
    log.info(f"[{agent.agent_name}] BATCH Phase 2 (ORIENT)")
    try:
        with _PhaseParams(agent, "phase2", log):
            lagebild = await _think_with_activity_check(agent, phase2_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120), phase="2")
        log.info(f"[{agent.agent_name}] BATCH Phase 2 complete (Lagebild): {len(lagebild)} chars")
        return lagebild
    except (asyncio.TimeoutError, Exception) as exc:
        log.error(f"[{agent.agent_name}] BATCH Phase 2 FAILED: {exc}")
        return None


async def _phase2b_decide(
    agent: "AIMOSAgent", lagebild: str, messages: list[dict], log: logging.Logger,
) -> str:
    """CR-250 Phase 2b: DECIDE — Identify ALL stakeholders who need to be informed.

    This is the key difference to a chatbot: The agent doesn't just respond to
    people who wrote — it proactively identifies everyone AFFECTED by the changes
    described in the Lagebild, even if they didn't send a message.
    """
    # Build list of known senders from this batch
    senders = set()
    for msg in messages:
        tid = msg.get("thread_id", "")
        if tid:
            senders.add(tid)

    phase2b_prompt = (
        f"LAGEBILD:\n{lagebild}\n\n"
        f"MESSAGES IN THIS BATCH came from these threads: {', '.join(senders)}\n\n"
        "TASK: Based on the Lagebild, identify ALL stakeholders who need to be "
        "informed or contacted — including those who did NOT send a message but "
        "are AFFECTED by the changes.\n\n"
        "For each stakeholder, specify:\n"
        "- Thread ID or email (if known from previous conversations)\n"
        "- What they need to know\n"
        "- Priority: HIGH (must inform now) / LOW (nice to know)\n\n"
        "Think: Who is waiting for this information? Who will be impacted?\n"
        "Who needs to adjust their schedule? Who asked about this before?\n\n"
        "GOVERNANCE CHECK: Does the Lagebild contain activity that looks like an "
        "untracked project or case? Signs: multiple stakeholders, deliverables, "
        "deadlines, dependencies — but no formal project plan or case file in the "
        "workspace. If yes, add a HIGH-priority action item: 'Formalize as project/case "
        "— needs scope, plan, and responsible owner.'\n"
        "Similarly, watch for MISSION CREEP: Is the scope of existing work growing "
        "beyond what was originally planned? New requirements without a change request? "
        "If yes, flag it.\n\n"
        "Output a STAKEHOLDER ACTION PLAN."
    )

    log.info(f"[{agent.agent_name}] BATCH Phase 2b (DECIDE)")
    try:
        with _PhaseParams(agent, "phase3", log):  # Same params as Orient (analytical)
            plan = await _think_with_activity_check(
                agent, phase2b_prompt, log,
                stale_timeout=agent.config.get("batch_stale_timeout", 120),
                phase="3",
            )
        log.info(f"[{agent.agent_name}] BATCH Phase 2b complete: {len(plan)} chars")
        return plan
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning(f"[{agent.agent_name}] BATCH Phase 2b FAILED (non-critical): {exc}")
        return ""  # Fallback: Phase 3 uses only message threads (old behavior)


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


async def _phase3b_validate(
    agent: "AIMOSAgent", lagebild: str, stakeholder_plan: str,
    response: str, thread_id: str, log: logging.Logger,
) -> str:
    """CR-250 Phase 3b: VALIDATE — Check response before dispatch.

    Returns "OK" if valid, or "PROBLEM: ..." if issues found.
    Non-blocking: problems are logged as warnings, response is still dispatched
    (H-20: avoid false-positive blocking).
    """
    validate_prompt = (
        f"LAGEBILD:\n{lagebild[:500]}\n\n"
        f"STAKEHOLDER PLAN:\n{stakeholder_plan[:300]}\n\n"
        f"RESPONSE TO SEND (thread={thread_id}):\n{response[:500]}\n\n"
        "VALIDATE this response:\n"
        "1. Does it contain facts NOT in the Lagebild? (hallucination check)\n"
        "2. Does it mention people not known from the conversation?\n"
        "3. Is it consistent with the Lagebild?\n"
        "Answer ONLY: 'OK' or 'PROBLEM: [brief description]'"
    )
    try:
        with _PhaseParams(agent, "phase4", log):  # Low temperature for precise validation
            result = await _think_with_activity_check(
                agent, validate_prompt, log,
                stale_timeout=agent.config.get("batch_stale_timeout", 120),
                phase="4",  # 4b VALIDATE — minimal tools (current_time only)
            )
        result = result.strip()
        if result.upper().startswith("OK"):
            return "OK"
        else:
            log.warning(f"[{agent.agent_name}] Phase 3b VALIDATE: {result[:200]}")
            return result
    except Exception as exc:
        log.warning(f"[{agent.agent_name}] Phase 3b VALIDATE failed (non-critical): {exc}")
        return "OK"  # H-21: Don't block on validation failure


async def _phase3_act(
    agent: "AIMOSAgent", messages: list[dict], lagebild: str, log: logging.Logger,
    stakeholder_plan: str = "",
    lagebild_partitions: dict[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """Phase 3: ACT — per stakeholder (from Phase 2b plan) with Lagebild context.

    CR-250: Phase 3 now iterates over stakeholders identified in Phase 2b,
    not just threads with messages. This enables proactive notification of
    affected stakeholders who didn't send a message.
    """
    threads: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        tid = msg.get("thread_id") or f"sender:{msg.get('sender_id', 0)}"
        threads[tid].append(msg)

    # CR-250: Extract proactive stakeholders from Phase 2b plan
    # These are stakeholders who didn't send a message but need to be informed.
    proactive_threads: dict[str, str] = {}  # thread_id → what they need to know
    if stakeholder_plan:
        import re as _re
        # Look for thread IDs or bare email addresses mentioned in the plan
        # that are NOT already in the message threads.
        # The LLM may write "email:meier@x.de" (thread format) or just "meier@x.de" (bare).
        found_ids = set()
        # Strategy 1: Bare email addresses → clean, canonical thread format
        # This catches both "email:meier@x.de" and bare "meier@x.de"
        for match in _re.finditer(r'([\w.+-]+@[\w.-]+\.\w{2,})', stakeholder_plan):
            found_ids.add(f"email:{match.group(1)}")
        for tid in found_ids:
            if tid not in threads:
                # Extract the "what they need to know" context
                # Take the 200 chars around the match
                start = max(0, match.start() - 50)
                end = min(len(stakeholder_plan), match.end() + 200)
                context = stakeholder_plan[start:end].strip()
                proactive_threads[tid] = context
        if proactive_threads:
            log.info(
                f"[{agent.agent_name}] Phase 2b identified {len(proactive_threads)} "
                f"PROACTIVE stakeholder(s): {list(proactive_threads.keys())}"
            )

    total_threads = len(threads) + len(proactive_threads)
    log.info(f"[{agent.agent_name}] BATCH Phase 3 (ACT): {len(threads)} with messages + {len(proactive_threads)} proactive = {total_threads} total")

    phase3_results: list[dict] = []

    for thread_id, thread_msgs in threads.items():
        representative_msg = thread_msgs[0]

        # CR-270: History isolation — clear in-memory history before each stakeholder call.
        # Without this, agent._history accumulates messages from ALL prior stakeholder
        # calls within this Phase 3 loop, causing exponential context growth and
        # eventual VRAM exhaustion (root cause of 2026-04-02 system freeze).
        agent._history = []

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

        # S-3 + CR-274: Cross-thread leak protection with scope isolation
        scope = _resolve_scope(thread_id, agent.config)
        if (
            lagebild_partitions
            and agent.config.get("batch_confidentiality") == "isolated"
            and scope in lagebild_partitions
        ):
            # DETERMINISTIC ISOLATION: LLM only sees this scope's Lagebild
            scope_lagebild = lagebild_partitions[scope]
            phase3_prompt = (
                f"LAGEBILD:\n{scope_lagebild}\n\n"
            )
        else:
            # Fallback: full Lagebild with prompt-based isolation
            phase3_prompt = (
                f"LAGEBILD (cross-thread situation report):\n{lagebild}\n\n"
                "IMPORTANT: The Lagebild above contains information about MULTIPLE stakeholders.\n"
                "You may ONLY share information relevant to the stakeholder below.\n"
                "NEVER mention names, messages, or internal details of other stakeholders.\n\n"
            )
        if thread_history_text:
            phase3_prompt += (
                f"CONVERSATION HISTORY with this stakeholder (thread={thread_id}):\n"
                f"{thread_history_text}\n\n"
            )
        phase3_prompt += (
            f"CURRENT MESSAGE(S) from this stakeholder ({sender_info}, channel={channel_info}):\n"
            f"{msg_contents}\n\n"
            "TASK: Write your response as PLAIN TEXT (Fließtext). This text will be sent "
            "to the stakeholder by the system.\n"
            "Do NOT use send_email, send_telegram, or any communication tool.\n"
            "You MAY use tools to create attachments (create_pdf, create_excel_sheet, write_file) "
            "or to look up information (read_file, recall, find_contact).\n"
            "Use the Lagebild as context but only share what is relevant to THIS person.\n"
            "NEVER mention names, messages, or internal details of other stakeholders."
        )

        log.info(
            f"[{agent.agent_name}] BATCH Phase 4a DRAFT thread={thread_id} "
            f"({len(thread_msgs)} msg(s), {len(thread_history_text)} chars history)"
        )
        try:
            with _PhaseParams(agent, "phase4", log):
                result = await _think_with_activity_check(
                    agent, phase3_prompt, log,
                    stale_timeout=agent.config.get("batch_stale_timeout", 120),
                    phase="4",  # 4a DRAFT — READ + WRITE (no COMMUNICATE)
                )
            log.info(f"[{agent.agent_name}] BATCH Phase 4a DRAFT thread={thread_id} complete: {len(result)} chars")
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

        # CR-250 Phase 3b: Validate before dispatch
        validation = "OK"
        if agent.config.get("batch_validate_responses", True) and stakeholder_plan:
            validation = await _phase3b_validate(
                agent, lagebild, stakeholder_plan, result, thread_id, log,
            )
        if "PROBLEM" in validation:
            phase3_results[-1]["validation"] = validation
            log.warning(f"[{agent.agent_name}] Phase 3b flagged thread={thread_id}: {validation[:100]}")
            # H-20: Still dispatch (avoid false-positive blocking), but flag it
        route = await agent.dispatch_response(result, representative_msg)
        log.info(f"[{agent.agent_name}] BATCH dispatched thread={thread_id} → {route}")

    # CR-250: Process proactive stakeholders (those who didn't send a message)
    for thread_id, context in proactive_threads.items():
        # CR-270: History isolation (same as reactive loop above)
        agent._history = []

        agent._current_thread_id = thread_id
        agent._current_msg_kind = "email"  # Default to email for proactive outreach
        agent._tool_call_count = 0

        # Load conversation history for this thread
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

        # CR-274: Scope-filtered Lagebild for proactive stakeholders
        scope = _resolve_scope(thread_id, agent.config)
        if (
            lagebild_partitions
            and agent.config.get("batch_confidentiality") == "isolated"
            and scope in lagebild_partitions
        ):
            scope_lagebild = lagebild_partitions[scope]
        else:
            scope_lagebild = lagebild

        phase3_prompt = (
            f"LAGEBILD:\n{scope_lagebild}\n\n"
            f"STAKEHOLDER ACTION PLAN says this person needs to be informed:\n{context}\n\n"
        )
        if thread_history_text:
            phase3_prompt += f"CONVERSATION HISTORY (thread={thread_id}):\n{thread_history_text}\n\n"
        phase3_prompt += (
            f"This stakeholder (thread={thread_id}) did NOT send a message in this batch, "
            "but is AFFECTED by the changes in the Lagebild.\n"
            "Write a PROACTIVE notification as PLAIN TEXT. Do NOT use send_email or any "
            "communication tool. The system will dispatch this text for you.\n"
            "Be concise and only share what is relevant to them."
        )

        log.info(f"[{agent.agent_name}] BATCH Phase 4a PROACTIVE DRAFT thread={thread_id}")
        try:
            with _PhaseParams(agent, "phase4", log):
                result = await _think_with_activity_check(
                    agent, phase3_prompt, log,
                    stale_timeout=agent.config.get("batch_stale_timeout", 120),
                    phase="4",  # 4a DRAFT
                )
            log.info(f"[{agent.agent_name}] BATCH Phase 3 PROACTIVE thread={thread_id} complete: {len(result)} chars")
            phase3_results.append({
                "thread_id": thread_id, "status": "proactive", "response_len": len(result),
                "summary": result[:150],
            })

            # Validate proactive response too
            if agent.config.get("batch_validate_responses", True) and stakeholder_plan:
                validation = await _phase3b_validate(
                    agent, lagebild, stakeholder_plan, result, thread_id, log,
                )
                if "PROBLEM" in validation:
                    phase3_results[-1]["validation"] = validation
                    log.warning(f"[{agent.agent_name}] Phase 3b PROACTIVE flagged: {validation[:100]}")

            # Dispatch proactive message — extract email from thread_id
            import re as _re2
            email_match = _re2.search(r'email:([\w.+-]+@[\w.-]+\.\w+)', thread_id)
            if email_match:
                # Real email address found — dispatch
                addr = email_match.group(1)
                proactive_msg = {
                    "kind": "email",
                    "sender_id": 0,
                    "thread_id": thread_id,
                    "content": f"[E-Mail empfangen]\nVon: {addr}\nKunden-Email: {addr}\nBetreff: Proaktive Benachrichtigung\nText: (proaktiv)",
                }
                route = await agent.dispatch_response(result, proactive_msg)
                log.info(f"[{agent.agent_name}] BATCH PROACTIVE dispatched thread={thread_id} → {route}")
            else:
                # No real email (e.g. "email:installateur@...") — log as todo
                log.info(
                    f"[{agent.agent_name}] BATCH PROACTIVE thread={thread_id}: "
                    f"No valid email address — adding to open tasks instead of dispatching"
                )
                phase3_results[-1]["status"] = "no_contact"
                phase3_results[-1]["summary"] = f"Kontaktdaten fehlen für {thread_id}"

        except (asyncio.TimeoutError, Exception) as exc:
            log.error(f"[{agent.agent_name}] BATCH Phase 3 PROACTIVE thread={thread_id} FAILED: {exc}")
            phase3_results.append({
                "thread_id": thread_id, "status": "FAILED", "error": str(exc),
            })

    return threads, phase3_results


# ── CR-258: Phase 3c — Document chunk analysis ────────────────────────────


async def _phase3c_documents(
    agent: "AIMOSAgent", new_documents: list[dict], lagebild: str,
    ws_base: Path, log: logging.Logger,
) -> list[dict]:
    """CR-258: Process new documents chunk by chunk within the OODA cycle.

    Uses the same safety patterns as Phase 3 stakeholder processing:
    - History isolation per chunk (CR-270)
    - _think_with_activity_check with hard timeout
    - Validation after each chunk
    """
    max_chunks = agent.config.get("batch_max_chunks_per_cycle", 20)
    doc_results = []
    total_chunks_processed = 0

    # CR-271: Dynamic chunk size — maximize document content per LLM call.
    # Instead of a fixed chunk_size, calculate how much document text fits
    # alongside system prompt + arbeitsdatei context + phase prompt overhead.
    num_ctx = agent.config.get("num_ctx", Config.DEFAULT_NUM_CTX)
    _SYSTEM_PROMPT_TOKENS = 2500   # System prompt (estimated)
    _PHASE_PROMPT_TOKENS = 500     # Phase 3c instructions
    _OUTPUT_RESERVE_TOKENS = 4096  # Reserve for thinking + response
    _SAFETY_MARGIN = 500
    _ARBEITSDATEI_BUDGET_CHARS = 2000  # Max chars of arbeitsdatei context per chunk

    log.info(
        f"[{agent.agent_name}] BATCH Phase 3c (DOCUMENTS): "
        f"{len(new_documents)} document(s) to analyze"
    )

    # Load current arbeitsdatei for context (if exists)
    arbeitsdatei_path = ws_base / "arbeitsdatei.md"
    arbeitsdatei_context = ""
    if arbeitsdatei_path.exists():
        try:
            content = arbeitsdatei_path.read_text(encoding="utf-8")
            arbeitsdatei_context = content[-_ARBEITSDATEI_BUDGET_CHARS:] if len(content) > _ARBEITSDATEI_BUDGET_CHARS else content
        except Exception:
            pass

    # Dynamic chunk size: fill remaining context with document text
    arbeitsdatei_tokens = len(arbeitsdatei_context) // 4
    available_for_doc = num_ctx - _SYSTEM_PROMPT_TOKENS - _PHASE_PROMPT_TOKENS - arbeitsdatei_tokens - _OUTPUT_RESERVE_TOKENS - _SAFETY_MARGIN
    chunk_size = max(available_for_doc * 4, 2000)  # Token→Char, minimum 2000
    # Cap at config value if explicitly set (user override)
    config_chunk = agent.config.get("batch_chunk_size")
    if config_chunk:
        chunk_size = min(chunk_size, config_chunk)

    log.info(
        f"[{agent.agent_name}] Phase 3c: Dynamic chunk_size={chunk_size} chars "
        f"(num_ctx={num_ctx}, available={available_for_doc} tokens, "
        f"arbeitsdatei={arbeitsdatei_tokens} tokens)"
    )

    for doc in new_documents:
        if total_chunks_processed >= max_chunks:
            log.warning(
                f"[{agent.agent_name}] Phase 3c: max_chunks ({max_chunks}) reached, "
                f"remaining documents deferred to next cycle"
            )
            break

        doc_path = doc["path"]
        doc_name = doc["name"]

        # Read document content — extract text BEFORE passing to LLM
        doc_text = ""
        try:
            suffix = doc_path.suffix.lower()
            if suffix in {".txt", ".csv", ".md"}:
                doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
            elif suffix == ".pdf":
                # PDF: Try decryption first (banks send encrypted PDFs), then OCR
                pdf_path = doc_path
                try:
                    import pikepdf
                    pdf = pikepdf.open(doc_path)
                    if pdf.is_encrypted:
                        # Try empty password (common for bank statements)
                        decrypted = ws_base / f".tmp_{doc_name}"
                        pdf.save(decrypted)
                        pdf_path = decrypted
                        log.info(f"[{agent.agent_name}] Phase 3c: Decrypted {doc_name}")
                    pdf.close()
                except Exception:
                    pass  # pikepdf not available or not encrypted — proceed with original
                try:
                    from core.skills.skill_document_ocr import DocumentOCRSkill
                    ocr = DocumentOCRSkill(agent_name=agent.agent_name, config={}, workspace_base=str(ws_base))
                    doc_text = ocr._ocr_file(pdf_path)
                    log.info(f"[{agent.agent_name}] Phase 3c: OCR extracted {len(doc_text)} chars from {doc_name}")
                except Exception as ocr_exc:
                    log.warning(f"[{agent.agent_name}] Phase 3c: OCR failed for {doc_name}: {ocr_exc}")
                    doc_text = f"[PDF konnte nicht gelesen werden: {doc_name}. OCR-Fehler: {ocr_exc}]"
            elif suffix in {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp", ".heic", ".heif"}:
                # Images (incl. iPhone HEIC): OCR
                try:
                    from core.skills.skill_document_ocr import DocumentOCRSkill
                    ocr = DocumentOCRSkill(agent_name=agent.agent_name, config={}, workspace_base=str(ws_base))
                    doc_text = ocr._ocr_file(doc_path)
                    log.info(f"[{agent.agent_name}] Phase 3c: OCR extracted {len(doc_text)} chars from {doc_name}")
                except Exception as ocr_exc:
                    doc_text = f"[Bild konnte nicht gelesen werden: {doc_name}. OCR-Fehler: {ocr_exc}]"
            elif suffix == ".xlsx":
                # Excel: Extract cell values as text
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(doc_path, read_only=True, data_only=True)
                    parts = []
                    for sheet in wb.sheetnames:
                        ws = wb[sheet]
                        parts.append(f"--- Sheet: {sheet} ---")
                        for row in ws.iter_rows(values_only=True):
                            cells = [str(c) if c is not None else "" for c in row]
                            if any(cells):
                                parts.append(" | ".join(cells))
                    wb.close()
                    doc_text = "\n".join(parts)
                    log.info(f"[{agent.agent_name}] Phase 3c: Extracted {len(doc_text)} chars from {doc_name}")
                except Exception as exc:
                    doc_text = f"[Excel konnte nicht gelesen werden: {doc_name}. Fehler: {exc}]"
            elif suffix == ".docx":
                # Word: Extract paragraphs as text
                try:
                    import docx
                    d = docx.Document(doc_path)
                    doc_text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
                    log.info(f"[{agent.agent_name}] Phase 3c: Extracted {len(doc_text)} chars from {doc_name}")
                except Exception as exc:
                    doc_text = f"[Word konnte nicht gelesen werden: {doc_name}. Fehler: {exc}]"
            else:
                doc_text = f"[Unbekanntes Dateiformat: {doc_name} ({suffix})]"
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 3c: Could not read {doc_name}: {exc}")
            continue

        if not doc_text or len(doc_text.strip()) < 10:
            log.warning(f"[{agent.agent_name}] Phase 3c: {doc_name} has no extractable text, skipping")
            continue

        # Split into chunks
        chunks = _chunk_document_text(doc_text, chunk_size) if len(doc_text) > chunk_size else [doc_text]
        log.info(
            f"[{agent.agent_name}] Phase 3c: {doc_name} → {len(chunks)} chunk(s)"
        )

        for i, chunk in enumerate(chunks):
            if total_chunks_processed >= max_chunks:
                break

            # CR-270 pattern: History isolation per chunk
            agent._history = []
            agent._current_thread_id = f"doc:{doc_name}:chunk{i+1}"
            agent._tool_call_count = 0

            chunk_prompt = (
                f"LAGEBILD:\n{lagebild}\n\n"
            )
            if arbeitsdatei_context:
                chunk_prompt += (
                    f"YOUR WORK-IN-PROGRESS (last entries from arbeitsdatei.md):\n"
                    f"{arbeitsdatei_context}\n\n"
                )
            chunk_prompt += (
                f"DOCUMENT: {doc_name} (chunk {i+1}/{len(chunks)})\n"
                f"{'='*40}\n{chunk}\n{'='*40}\n\n"
                "Analyze this document chunk thoroughly.\n"
                "For EACH entry/item/line in the document, output ONE LINE in this exact format:\n"
                "| Datum | Beschreibung | Betrag | Kategorie | §EStG | Status |\n\n"
                "Example:\n"
                "| 16.02.2025 | PINKCAT Tastatur+Maus Set | 25,99€ | Werbungskosten/Arbeitsmittel | §9 | kategorisiert |\n"
                "| 23.10.2025 | Miele Backofen-Reparatur (Arbeitsanteil) | 238,37€ | Handwerkerleistung | §35a | kategorisiert |\n\n"
                "Status values: kategorisiert / unklar / privat / nachfragen\n"
                "If unclear whether private or business: set status to 'nachfragen'.\n"
                "For Handwerker invoices: SEPARATE material costs from labor costs.\n"
                "Output ONLY the table rows, no commentary. I will save them automatically."
            )

            try:
                with _PhaseParams(agent, "phase2", log):
                    result = await _think_with_activity_check(
                        agent, chunk_prompt, log,
                        stale_timeout=agent.config.get("batch_stale_timeout", 120),
                        phase="2",  # 2a ORIENT chunk loop
                    )
                total_chunks_processed += 1

                # Auto-append analysis result to arbeitsdatei.md (with dedup)
                if result and len(result.strip()) > 10:
                    header_needed = not arbeitsdatei_path.exists() or arbeitsdatei_path.stat().st_size == 0
                    # Load existing lines for dedup
                    existing_lines = set()
                    if arbeitsdatei_path.exists():
                        for el in arbeitsdatei_path.read_text(encoding="utf-8").split("\n"):
                            el = el.strip()
                            if el.startswith("|") and "Datum" not in el and "---" not in el:
                                # Normalize for comparison: strip whitespace between cells
                                existing_lines.add("|".join(c.strip() for c in el.split("|")))
                    new_lines = []
                    for line in result.strip().split("\n"):
                        line = line.strip()
                        if line.startswith("|") and "Datum" not in line and "---" not in line:
                            # Append source reference if not already present
                            if doc_name not in line:
                                line = line.rstrip("|").rstrip() + f" | {doc_name} |"
                            else:
                                line = line if line.endswith("|") else line + " |"
                            # Dedup: check if this line already exists
                            normalized = "|".join(c.strip() for c in line.split("|"))
                            if normalized not in existing_lines:
                                new_lines.append(line)
                                existing_lines.add(normalized)
                    if new_lines or header_needed:
                        with open(arbeitsdatei_path, "a", encoding="utf-8") as f:
                            if header_needed:
                                f.write("# Beleganalyse — Steuerjahr 2025\n\n")
                                f.write("| Datum | Beschreibung | Betrag | Kategorie | §EStG | Beleg-Ref | Status |\n")
                                f.write("|-------|-------------|--------|-----------|-------|-----------|--------|\n")
                            for nl in new_lines:
                                f.write(nl + "\n")
                    log.info(
                        f"[{agent.agent_name}] Phase 3c: {doc_name} chunk {i+1}/{len(chunks)} "
                        f"→ appended to arbeitsdatei.md ({len(result)} chars)"
                    )
                else:
                    log.warning(
                        f"[{agent.agent_name}] Phase 3c: {doc_name} chunk {i+1}/{len(chunks)} "
                        f"produced empty/short result ({len(result)} chars)"
                    )

                doc_results.append({
                    "thread_id": f"doc:{doc_name}:chunk{i+1}",
                    "status": "analyzed",
                    "response_len": len(result),
                })

                # Refresh arbeitsdatei context for next chunk
                if arbeitsdatei_path.exists():
                    try:
                        content = arbeitsdatei_path.read_text(encoding="utf-8")
                        arbeitsdatei_context = content[-1500:] if len(content) > 1500 else content
                    except Exception:
                        pass

            except (asyncio.TimeoutError, Exception) as exc:
                log.error(
                    f"[{agent.agent_name}] Phase 3c: {doc_name} chunk {i+1} FAILED: {exc}"
                )
                doc_results.append({
                    "thread_id": f"doc:{doc_name}:chunk{i+1}",
                    "status": "FAILED",
                    "error": str(exc),
                })
                continue

    log.info(
        f"[{agent.agent_name}] Phase 3c complete: {total_chunks_processed} chunks processed "
        f"from {len(new_documents)} document(s)"
    )
    return doc_results


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
        if pr["status"] in ("sent", "analyzed", "proactive", "no_contact"):
            label = pr["status"]
            p3_summary_lines.append(f"  ✓ {pr['thread_id']}: {label} ({pr.get('response_len', 0)} chars)")
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
        with _PhaseParams(agent, "phase5", log):
            persist_result = await _think_with_activity_check(agent, phase4_prompt, log, stale_timeout=agent.config.get("batch_stale_timeout", 120), phase="5")
        log.info(f"[{agent.agent_name}] BATCH Phase 5 (PERSIST) complete: {len(persist_result)} chars")
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning(f"[{agent.agent_name}] BATCH Phase 4 FAILED (non-critical): {exc}")
        persist_result = ""
    finally:
        # Restore history (Phase 4 messages are already persisted to DB by think())
        if saved_history is not None:
            agent._history = saved_history

    # CR-248: Parse text-based write_file calls from Phase 4 output.
    # The LLM often writes correct write_file() calls as text instead of native tool calls.
    # The orchestrator extracts and executes them.
    if persist_result and ws_base.exists():
        import re as _re
        text_calls = _re.findall(
            r'write_file\s*\(\s*(?:filename\s*=\s*)?["\']([^"\']+)["\']\s*,\s*(?:content\s*=\s*)?(?:"""(.*?)"""|["\'](.+?)["\'])',
            persist_result, _re.DOTALL,
        )
        # Also try XML format: <invoke name="write_file"><parameter name="path">...</parameter><parameter name="content">...</parameter>
        xml_calls = _re.findall(
            r'<invoke\s+name="write_file">\s*<parameter\s+name="(?:path|filename)">(.*?)</parameter>\s*<parameter\s+name="content">(.*?)</parameter>',
            persist_result, _re.DOTALL,
        )
        all_parsed_calls = [(fn, c1 or c2) for fn, c1, c2 in text_calls] + list(xml_calls)
        for filename, content in all_parsed_calls:
            if filename and content and len(content) > 10:
                # Unescape literal \n sequences that LLMs often produce
                content = content.replace("\\n", "\n").replace("\\t", "\t")
                filepath = ws_base / filename.strip()
                try:
                    filepath.write_text(content.strip(), encoding="utf-8")
                    log.info(f"[{agent.agent_name}] Phase 4 PARSED write_file: {filename} ({len(content)} chars)")
                except Exception as exc:
                    log.warning(f"[{agent.agent_name}] Phase 4 PARSED write_file FAILED {filename}: {exc}")

    # Fallback: If no state.md was written (neither by native tool call nor by parser),
    # the orchestrator writes it from the Lagebild + Phase 3 results.
    state_path = ws_base / leading_file_spec
    state_was_written = state_path.exists() and state_path.stat().st_mtime > (time.time() - 60)
    if not state_was_written and ws_base.exists():
        # state.md was not written by the LLM — write it from the orchestrator
        import datetime as _dt
        fallback_state = (
            f"# Handover — {_dt.datetime.now():%d.%m.%Y %H:%M} "
            f"({msg_count} messages processed)\n\n"
            f"## Situation Report\n{lagebild[:800]}\n\n"
            f"## Phase 3 Results\n{phase3_summary}\n\n"
        )
        if persist_result:
            fallback_state += f"## Agent Notes\n{persist_result[:500]}\n"
        try:
            state_path.write_text(fallback_state[:1500], encoding="utf-8")
            log.info(f"[{agent.agent_name}] Phase 4 FALLBACK: Wrote {leading_file_spec} ({len(fallback_state[:1500])} chars)")
        except Exception as exc:
            log.warning(f"[{agent.agent_name}] Phase 4 FALLBACK write failed: {exc}")
