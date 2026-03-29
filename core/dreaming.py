#!/usr/bin/env python3
"""
CR-090: Agent Dreaming — Memory Consolidation
==============================================
Called by the Orchestrator when an agent is idle and has accumulated history.
Runs at low CPU priority (os.nice(10)).

Phases:
  0. LLM-based fact extraction from conversation history (uses local Ollama)
     → MEM:{...} lines stored as memories, FILE:{...} lines written to workspace
  1. Consolidate similar memories (Jaccard >= 0.6)
  2. Resolve contradictions (same key-prefix, Jaccard < 0.2)
  3. Decay stale memories (30d+ unused, low importance)
  4. Clean hallucination fragments (garbage, noise)
"""

import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("AIMOS.dreaming")


def dream(agent_name: str, db_path: str | Path, pool=None) -> dict:
    """Run all dream phases on an agent's memory.db.

    Returns summary dict with counts and duration.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        log.info(f"[Dream] No memory.db for '{agent_name}' — skipping")
        return {"consolidated": 0, "contradictions_resolved": 0,
                "decayed": 0, "hallucinations_cleaned": 0, "duration_ms": 0}

    # CR-167: Check for pending user messages — they have priority over dreaming
    try:
        import psycopg2
        from core.config import Config
        pg = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD, connect_timeout=5,
        )
        cur = pg.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM pending_messages WHERE agent_name=%s "
            "AND processed=FALSE AND kind NOT IN ('scheduled_job','internal')",
            (agent_name,),
        )
        pending = cur.fetchone()[0]
        pg.close()
        if pending > 0:
            log.info(f"[Dream] Skipping dream — {pending} user messages pending for '{agent_name}'")
            return {"consolidated": 0, "contradictions_resolved": 0,
                    "decayed": 0, "hallucinations_cleaned": 0, "duration_ms": 0,
                    "skipped": "pending_messages"}
    except Exception as exc:
        log.debug(f"[Dream] Pending message check failed: {exc}")

    t0 = time.monotonic()

    # Lower CPU priority (best-effort, non-root may fail)
    old_nice = None
    try:
        old_nice = os.nice(0)
        os.nice(10)
    except OSError:
        pass

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        extracted = _extract_facts_from_history(conn, agent_name)
        consolidated = _consolidate_similar(conn)
        contradictions = _resolve_contradictions(conn)
        decayed = _decay_stale(conn)
        cleaned = _clean_hallucinations(conn)
        weekly_report = _maybe_write_weekly_report(conn, agent_name)

        conn.commit()

        summary = {
            "extracted_from_history": extracted,
            "consolidated": consolidated,
            "contradictions_resolved": contradictions,
            "decayed": decayed,
            "hallucinations_cleaned": cleaned,
            "weekly_report": weekly_report,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

        _log_dream_summary(conn, agent_name, summary)
        conn.commit()
        conn.close()

        log.info(
            f"[Dream] '{agent_name}' done: "
            f"{consolidated} merged, {contradictions} contradictions, "
            f"{decayed} decayed, {cleaned} cleaned "
            f"({summary['duration_ms']}ms)"
        )
        return summary

    except Exception as exc:
        log.error(f"[Dream] '{agent_name}' failed: {exc}")
        return {"extracted_from_history": 0, "consolidated": 0,
                "contradictions_resolved": 0, "decayed": 0,
                "hallucinations_cleaned": 0,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": str(exc)}
    finally:
        # Restore niceness (best-effort)
        if old_nice is not None:
            try:
                os.nice(-10)
            except OSError:
                pass


# ── Tokenization ─────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Extract lowercase word tokens (>= 3 chars, incl. umlauts)."""
    return set(re.findall(r'[a-zäöüß]{3,}', text.lower()))


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Phase 0: Extract Facts from Chat History (LLM-powered) ──────────────────

_EXTRACTION_PROMPT = """You are an agent's "dreaming" system. While the agent sleeps, you review its recent conversations and organize its knowledge.

You have TWO tasks:

## TASK 1: Extract memories (quick facts for instant recall)
For each fact, output one JSON line starting with "MEM:":
MEM:{{"key": "short_snake_case_key", "value": "the fact", "category": "semantic|episodic|procedural", "importance": 1-10}}

Categories: semantic (facts, names, companies), episodic (events, decisions, corrections), procedural (rules, how-to)
Importance: 10=critical (corrections, identity), 7=important (decisions, preferences), 5=normal

## TASK 2: Write workspace notes (structured information for the agent's desk)
For each note, output a JSON line starting with "FILE:":
FILE:{{"path": "notes/topic_name.txt", "content": "structured note content"}}

Use workspace notes for:
- Summaries of complex discussions (too long for a memory value)
- Todo lists and open action items → path "todo.txt"
- Project plans, research findings, meeting notes
- Customer-specific information files → path "notes/customer_name.txt"

## RULES:
- Write in the SAME LANGUAGE as the conversation
- Memories = short facts (one sentence). Notes = structured longer content.
- If the user corrected the agent, extract the CORRECT information (importance=10)
- Update todo.txt if there are open tasks, completed tasks, or new deadlines
- Skip greetings, small talk, raw tool outputs
- Output ONLY MEM: and FILE: lines, nothing else

## EXISTING WORKSPACE FILES:
{existing_files}

## EXISTING MEMORIES (do not duplicate):
{existing_memories}

## RECENT CONVERSATION:
{conversation}

Extract and organize (MEM: and FILE: lines only):"""


def _extract_facts_from_history(conn: sqlite3.Connection, agent_name: str) -> int:
    """Phase 0: Use LLM to extract memories AND write workspace notes from chat history.

    Reads conversation from PostgreSQL, existing workspace files, and memories.
    Sends to local LLM which returns:
      - MEM:{...} lines → stored as memories
      - FILE:{...} lines → written to agent workspace

    Runs during idle dreaming — GPU is free, agent is not active.
    """
    import json as _json
    extracted = 0

    try:
        import psycopg2
        import httpx
        from core.config import Config
        from core.skills.base import BaseSkill

        # Workspace path for this agent
        workspace = BaseSkill.workspace_path(agent_name)
        workspace.mkdir(parents=True, exist_ok=True)

        pg = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD, connect_timeout=5,
        )
        cur = pg.cursor()

        # Get last dream timestamp to avoid re-processing
        last_dream = conn.execute(
            "SELECT message FROM agent_log WHERE level='DREAM_EXTRACTION' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        since_clause = "AND created_at > NOW() - INTERVAL '24 hours'"
        if last_dream:
            since_clause = f"AND created_at > '{last_dream[0]}'"

        cur.execute(
            f"SELECT role, content FROM aimos_chat_histories "
            f"WHERE agent_name=%s {since_clause} "
            f"AND content NOT LIKE '%%[Scheduled Task]%%' "
            f"ORDER BY id ASC LIMIT 40",
            (agent_name,)
        )
        rows = cur.fetchall()
        pg.close()

        if len(rows) < 4:
            return 0

        # Build conversation text
        conversation_lines = []
        for role, content in rows:
            clean = re.sub(r"\[(?:Von|From|Kontext):[^\]]*\]\s*", "", content or "").strip()
            if clean and len(clean) > 5:
                label = "User" if role == "user" else "Agent"
                conversation_lines.append(f"{label}: {clean[:300]}")

        if len(conversation_lines) < 4:
            return 0

        # Gather existing workspace files (so LLM can update rather than duplicate)
        existing_files = []
        for f in sorted(workspace.rglob("*.txt")):
            rel = f.relative_to(workspace)
            try:
                preview = f.read_text(errors="replace")[:200]
                existing_files.append(f"{rel}: {preview}")
            except Exception:
                existing_files.append(f"{rel}: (unreadable)")
        existing_files_text = "\n".join(existing_files[:15]) if existing_files else "(empty workspace)"

        # Gather existing memories (so LLM doesn't duplicate)
        mem_rows = conn.execute(
            "SELECT key, substr(value, 1, 60), category FROM memories ORDER BY importance DESC LIMIT 20"
        ).fetchall()
        existing_mems = "\n".join(f"[{cat}] {key}: {val}" for key, val, cat in mem_rows) if mem_rows else "(no memories yet)"

        conversation_text = "\n".join(conversation_lines[-30:])
        prompt = _EXTRACTION_PROMPT.format(
            conversation=conversation_text,
            existing_files=existing_files_text,
            existing_memories=existing_mems,
        )

        # CR-167: Call local LLM with 120s timeout — yields VRAM if stuck
        try:
            resp = httpx.post(
                Config.ollama_url(),
                json={
                    "model": Config.LLM_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.1, "num_predict": 2048},
                },
                timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            log.warning(f"[Dream] LLM extraction timed out after 120s for '{agent_name}' — yielding VRAM")
            return 0
        llm_response = resp.json().get("message", {}).get("content", "")

        if not llm_response:
            return 0

        # Parse MEM: and FILE: lines from LLM response
        existing_keys = {r[0] for r in conn.execute("SELECT key FROM memories").fetchall()}
        files_written = 0

        for line in llm_response.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            # --- MEM: lines → store as memories ---
            if line.startswith("MEM:"):
                json_str = line[4:].strip()
                try:
                    fact = _json.loads(json_str)
                    key = fact.get("key", "").strip()
                    value = fact.get("value", "").strip()
                    category = fact.get("category", "semantic").strip()
                    importance = int(fact.get("importance", 5))

                    if not key or not value or len(key) < 3 or len(value) < 5:
                        continue
                    if category not in ("semantic", "episodic", "procedural"):
                        category = "semantic"
                    importance = max(1, min(10, importance))

                    if key not in existing_keys:
                        emb = None
                        try:
                            from core.embeddings import embed
                            emb = embed(f"{key} {value}")
                        except Exception:
                            pass

                        conn.execute(
                            "INSERT OR IGNORE INTO memories (key, value, category, importance, "
                            "source, last_accessed, updated_at, embedding) "
                            "VALUES (?, ?, ?, ?, 'dream', datetime('now'), datetime('now'), ?)",
                            (key, value[:500], category, importance, emb),
                        )
                        existing_keys.add(key)
                        extracted += 1
                        log.info(f"[Dream] Memory: [{category}|imp={importance}] {key}: {value[:60]}")
                    else:
                        # CR-177: Memory confidence scoring — don't overwrite high-confidence memories
                        existing_row = conn.execute(
                            "SELECT importance FROM memories WHERE key=?", (key,)
                        ).fetchone()
                        if existing_row:
                            existing_imp = existing_row[0] if isinstance(existing_row, (tuple, list)) else existing_row["importance"]
                            if existing_imp >= 7 and importance < existing_imp:
                                log.info(
                                    f"[Dream] Skipping memory '{key}' — existing memory has "
                                    f"higher confidence ({existing_imp} >= {importance})"
                                )
                                continue
                            # Update if new importance is higher or equal
                            if importance >= existing_imp:
                                conn.execute(
                                    "UPDATE memories SET value=?, importance=?, "
                                    "updated_at=datetime('now') WHERE key=?",
                                    (value[:500], importance, key),
                                )
                                log.info(f"[Dream] Updated memory '{key}': imp {existing_imp}→{importance}")

                except (_json.JSONDecodeError, ValueError, TypeError):
                    continue

            # --- FILE: lines → write to workspace ---
            elif line.startswith("FILE:"):
                json_str = line[5:].strip()
                try:
                    file_op = _json.loads(json_str)
                    fpath = file_op.get("path", "").strip()
                    content = file_op.get("content", "").strip()

                    if not fpath or not content or len(content) < 10:
                        continue

                    # Security: prevent path traversal
                    fpath = fpath.replace("..", "").lstrip("/")
                    if not fpath:
                        continue

                    target = workspace / fpath
                    target.parent.mkdir(parents=True, exist_ok=True)

                    # Append to existing file or create new
                    if target.exists() and fpath == "todo.txt":
                        # Todo: LLM sends the full updated content
                        target.write_text(content, encoding="utf-8")
                    elif target.exists():
                        # Notes: append new content with timestamp separator
                        existing = target.read_text(encoding="utf-8", errors="replace")
                        timestamp = time.strftime("%Y-%m-%d %H:%M")
                        target.write_text(
                            existing.rstrip() + f"\n\n--- Dream update {timestamp} ---\n{content}",
                            encoding="utf-8",
                        )
                    else:
                        target.write_text(content, encoding="utf-8")

                    files_written += 1
                    log.info(f"[Dream] File: {fpath} ({len(content)} chars)")

                except (_json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
                    log.debug(f"[Dream] File write failed: {exc}")
                    continue

        # Rebuild FTS index if we added memories
        if extracted > 0:
            try:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            except Exception:
                pass

        if files_written > 0:
            log.info(f"[Dream] Wrote {files_written} workspace file(s) for '{agent_name}'")

        # Log extraction timestamp so we don't re-process
        conn.execute(
            "INSERT INTO agent_log (level, message) VALUES ('DREAM_EXTRACTION', datetime('now'))"
        )

        log.info(f"[Dream] LLM extraction for '{agent_name}': {extracted} new memories from {len(rows)} messages")

    except ImportError:
        log.debug("[Dream] Dependencies not available — skipping LLM extraction")
    except Exception as exc:
        log.warning(f"[Dream] LLM extraction failed for '{agent_name}': {exc}")

    return extracted


# ── Phase 5: Weekly Report ───────────────────────────────────────────────────

_WEEKLY_REPORT_PROMPT = """You are writing a weekly status report for an AI agent.

Based on the agent's memories and workspace files, write a concise weekly report.

## Structure:
1. **Completed this week** — what was accomplished
2. **Open tasks** — what still needs to be done
3. **Waiting for** — what is blocked on external input
4. **Recommendations** — suggested next steps

Write in the same language as the agent's memories. Be concise and actionable.
One paragraph per section. No filler.

## Agent Memories (most important):
{memories}

## Agent Workspace Files:
{files}

## Write the weekly report:"""


def _maybe_write_weekly_report(conn: sqlite3.Connection, agent_name: str) -> bool:
    """CR-146: Write a weekly report if 7+ days since the last one.

    Uses LLM to summarize memories and workspace into a structured report.
    Saves to workspace/reports/weekly_YYYY-MM-DD.txt
    """
    try:
        from core.skills.base import BaseSkill
        workspace = BaseSkill.workspace_path(agent_name)
        reports_dir = workspace / "reports"

        # Check if we already wrote a report this week
        if reports_dir.exists():
            existing = sorted(reports_dir.glob("weekly_*.txt"), reverse=True)
            if existing:
                last_date_str = existing[0].stem.replace("weekly_", "")
                try:
                    from datetime import datetime as _dt
                    last_date = _dt.strptime(last_date_str, "%Y-%m-%d")
                    days_since = (datetime.now() - last_date).days
                    if days_since < 7:
                        return False  # Too recent, skip
                except ValueError:
                    pass

        # Gather data for the report
        mem_rows = conn.execute(
            "SELECT key, value, category, importance FROM memories "
            "ORDER BY importance DESC, updated_at DESC LIMIT 25"
        ).fetchall()
        if not mem_rows:
            return False  # No memories = nothing to report

        memories_text = "\n".join(
            f"[{cat}|imp={imp}] {key}: {val}" for key, val, cat, imp in mem_rows
        )

        files_text = "(empty)"
        if workspace.exists():
            file_lines = []
            for f in sorted(workspace.rglob("*.txt")):
                if "reports/" in str(f):
                    continue  # Don't include old reports
                rel = f.relative_to(workspace)
                try:
                    preview = f.read_text(errors="replace")[:150]
                    file_lines.append(f"{rel}: {preview}")
                except Exception:
                    pass
            if file_lines:
                files_text = "\n".join(file_lines[:10])

        prompt = _WEEKLY_REPORT_PROMPT.format(
            memories=memories_text,
            files=files_text,
        )

        # CR-167: LLM call with 120s timeout — yields VRAM if stuck
        import httpx
        from core.config import Config
        try:
            resp = httpx.post(
                Config.ollama_url(),
                json={
                    "model": Config.LLM_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.2, "num_predict": 1024},
                },
                timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            log.warning(f"[Dream] Weekly report timed out after 120s for '{agent_name}' — yielding VRAM")
            return False
        report = resp.json().get("message", {}).get("content", "")

        if not report or len(report) < 50:
            return False

        # Save report
        reports_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        report_path = reports_dir / f"weekly_{date_str}.txt"
        report_path.write_text(report, encoding="utf-8")

        log.info(f"[Dream] Weekly report for '{agent_name}': {len(report)} chars → {report_path.name}")
        return True

    except Exception as exc:
        log.debug(f"[Dream] Weekly report failed for '{agent_name}': {exc}")
        return False


# ── Phase 1: Consolidate Similar ─────────────────────────────────────────────

def _consolidate_similar(conn: sqlite3.Connection) -> int:
    """Merge memories with Jaccard >= 0.6 within the same category.

    One merge pass per dream — gradual consolidation over idle periods.
    """
    rows = conn.execute(
        "SELECT id, key, value, category, importance, access_count, "
        "created_at, last_accessed FROM memories ORDER BY category, id"
    ).fetchall()

    # Group by category
    by_cat: dict[str, list] = {}
    for r in rows:
        cat = r["category"] or "semantic"
        by_cat.setdefault(cat, []).append(r)

    merged_ids: set[int] = set()
    merge_count = 0

    for cat, mems in by_cat.items():
        # Pre-tokenize
        tokens = []
        for m in mems:
            t = _tokenize(f"{m['key']} {m['value']}")
            tokens.append(t)

        # Pairwise comparison (skip already-merged)
        for i in range(len(mems)):
            if mems[i]["id"] in merged_ids:
                continue
            for j in range(i + 1, len(mems)):
                if mems[j]["id"] in merged_ids:
                    continue
                sim = _jaccard(tokens[i], tokens[j])
                if sim >= 0.6:
                    # Determine winner: higher importance, then access_count, then newer
                    a, b = mems[i], mems[j]
                    if (a["importance"], a["access_count"], a["created_at"] or "") >= \
                       (b["importance"], b["access_count"], b["created_at"] or ""):
                        winner, loser = a, b
                    else:
                        winner, loser = b, a

                    new_imp = min(10, max(winner["importance"], loser["importance"]) + 1)
                    new_ac = (winner["access_count"] or 0) + (loser["access_count"] or 0)

                    conn.execute(
                        "UPDATE memories SET importance=?, access_count=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (new_imp, new_ac, winner["id"]),
                    )
                    conn.execute("DELETE FROM memories WHERE id=?", (loser["id"],))
                    merged_ids.add(loser["id"])
                    merge_count += 1

    return merge_count


# ── Phase 2: Resolve Contradictions ──────────────────────────────────────────

def _key_prefix(key: str) -> str:
    """Extract key prefix: part before first '_' or first 10 chars."""
    idx = key.find("_")
    if idx > 0:
        return key[:idx]
    return key[:10]


def _resolve_contradictions(conn: sqlite3.Connection) -> int:
    """Find memories with same key-prefix but very different values (Jaccard < 0.2).

    Keep the one with higher access_count (tie: newer created_at).
    """
    rows = conn.execute(
        "SELECT id, key, value, importance, access_count, created_at "
        "FROM memories ORDER BY key"
    ).fetchall()

    # Group by key prefix
    by_prefix: dict[str, list] = {}
    for r in rows:
        prefix = _key_prefix(r["key"])
        by_prefix.setdefault(prefix, []).append(r)

    resolved = 0
    deleted_ids: set[int] = set()

    for prefix, mems in by_prefix.items():
        if len(mems) < 2:
            continue
        for i in range(len(mems)):
            if mems[i]["id"] in deleted_ids:
                continue
            val_tokens_i = _tokenize(mems[i]["value"])
            for j in range(i + 1, len(mems)):
                if mems[j]["id"] in deleted_ids:
                    continue
                val_tokens_j = _tokenize(mems[j]["value"])
                sim = _jaccard(val_tokens_i, val_tokens_j)
                if sim < 0.2:
                    a, b = mems[i], mems[j]
                    # Keep higher access_count, tie → newer
                    if (a["access_count"] or 0, a["created_at"] or "") >= \
                       (b["access_count"] or 0, b["created_at"] or ""):
                        loser = b
                    else:
                        loser = a
                    conn.execute("DELETE FROM memories WHERE id=?", (loser["id"],))
                    deleted_ids.add(loser["id"])
                    resolved += 1

    return resolved


# ── Phase 3: Decay Stale ─────────────────────────────────────────────────────

def _decay_stale(conn: sqlite3.Connection) -> int:
    """Reduce importance of old, low-priority memories. Delete very stale ones."""
    now = datetime.utcnow()
    threshold_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    threshold_90d = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")

    count = 0

    # Delete: 90d+ stale AND importance <= 2
    cur = conn.execute(
        "SELECT id FROM memories WHERE "
        "(last_accessed < ? OR (last_accessed IS NULL AND created_at < ?)) "
        "AND importance <= 2",
        (threshold_90d, threshold_90d),
    )
    ids_to_delete = [r["id"] for r in cur.fetchall()]
    for mid in ids_to_delete:
        # Safety: never delete high-importance or frequently accessed
        conn.execute("DELETE FROM memories WHERE id=? AND importance <= 2 AND access_count < 3", (mid,))
        if conn.total_changes:
            count += 1

    # Decay: 30d+ stale AND importance <= 4 → importance - 1
    cur = conn.execute(
        "SELECT id, importance FROM memories WHERE "
        "(last_accessed < ? OR (last_accessed IS NULL AND created_at < ?)) "
        "AND importance <= 4 AND importance > 1",
        (threshold_30d, threshold_30d),
    )
    for r in cur.fetchall():
        conn.execute(
            "UPDATE memories SET importance = MAX(1, importance - 1), "
            "updated_at=datetime('now') WHERE id=?",
            (r["id"],),
        )
        count += 1

    return count


# ── Phase 4: Clean Hallucinations ────────────────────────────────────────────

_GARBAGE_EXACT = {"none", "null", "ok", "ja", "nein", "fehler:", "error:"}


def _clean_hallucinations(conn: sqlite3.Connection) -> int:
    """Remove fragment/garbage memories using heuristics.

    Safety: memories with importance >= 7 OR access_count >= 3 are NEVER deleted.
    """
    now = datetime.utcnow()
    threshold_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT id, key, value, importance, access_count, source, created_at "
        "FROM memories"
    ).fetchall()

    cleaned = 0
    for r in rows:
        # Safety net: never touch high-importance or frequently accessed
        if (r["importance"] or 0) >= 7 or (r["access_count"] or 0) >= 3:
            continue

        val = (r["value"] or "").strip()
        delete = False

        # Too short
        if len(val) < 5:
            delete = True
        # Only punctuation/symbols (no alphanumeric)
        elif not re.search(r'[a-zA-Z0-9äöüÄÖÜß]', val):
            delete = True
        # Known garbage (exact match, case-insensitive)
        elif val.lower() in _GARBAGE_EXACT:
            delete = True
        # Repeated characters (5+ same char in a row)
        elif re.search(r'(.)\1{4,}', val):
            delete = True
        # Self-generated noise: source='self', low importance, never accessed, older than 7d
        elif (r["source"] == "self"
              and (r["importance"] or 0) <= 2
              and (r["access_count"] or 0) == 0
              and r["created_at"] and r["created_at"] < threshold_7d):
            delete = True

        if delete:
            conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
            cleaned += 1

    return cleaned


# ── Dream Summary Log ────────────────────────────────────────────────────────

def _log_dream_summary(conn: sqlite3.Connection, agent_name: str, summary: dict):
    """Write dream results to agent_log table."""
    msg = (
        f"Dream complete: "
        f"{summary['consolidated']} merged, "
        f"{summary['contradictions_resolved']} contradictions resolved, "
        f"{summary['decayed']} decayed, "
        f"{summary['hallucinations_cleaned']} hallucinations cleaned "
        f"({summary['duration_ms']}ms)"
    )
    try:
        conn.execute(
            "INSERT INTO agent_log (level, message) VALUES (?, ?)",
            ("INFO", msg),
        )
    except Exception:
        pass  # agent_log table might not exist yet
