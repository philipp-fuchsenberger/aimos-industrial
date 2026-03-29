#!/usr/bin/env python3
"""
AIMOS Open Agent Package (OAP) — Export / Import CLI
=====================================================
CR-121 Phase 1+2: Agent portability via .oap (ZIP) and .oamf.jsonl formats.

Usage:
    python scripts/agent_export.py --agent myagent --output myagent.oap
    python scripts/agent_export.py --agent myagent --memory-only --output myagent.oamf.jsonl
    python scripts/agent_export.py --import myagent.oap [--with-history]
    python scripts/agent_export.py --list
    python scripts/agent_export.py --agent myagent --anonymize --output agent_clean.oap

Requires: psycopg2, pyyaml (optional — falls back to JSON)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_STORAGE_DIR = _PROJECT_ROOT / "storage" / "agents"

# Add project root to path so we can import core.config
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from core.config import Config  # noqa: E402
except ImportError:
    Config = None  # fallback: use env vars / defaults directly

# Optional YAML support
try:
    import yaml as _yaml  # type: ignore[import-untyped]
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# PostgreSQL — psycopg2 (sync, no asyncpg needed for a CLI tool)
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oap")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AIMOS_VERSION = "4.3.0"
OAP_FORMAT_VERSION = "1.0"
OAMF_VERSION = 1

# PII patterns for --anonymize
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email",  re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")),
    ("phone",  re.compile(r"\+?\d[\d\s\-()]{7,}\d")),
    ("iban",   re.compile(r"[A-Z]{2}\d{2}[\s]?[\dA-Z]{4}[\s]?[\dA-Z]{4}[\s]?[\dA-Z]{4}[\s]?[\dA-Z]{0,}")),
]


# ═══════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════

def _pg_connect() -> "psycopg2.extensions.connection":
    """Return a psycopg2 connection using AIMOS Config or env vars."""
    if psycopg2 is None:
        log.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    if Config is not None:
        params = {
            "host": Config.PG_HOST,
            "port": Config.PG_PORT,
            "dbname": Config.PG_DB,
            "user": Config.PG_USER,
            "password": Config.PG_PASSWORD,
        }
    else:
        params = {
            "host": os.getenv("PG_HOST", "127.0.0.1"),
            "port": int(os.getenv("PG_PORT", "5432")),
            "dbname": os.getenv("PG_DB", "aimos"),
            "user": os.getenv("PG_USER", "n8n_user"),
            "password": os.getenv("PG_PASSWORD", ""),
        }

    try:
        conn = psycopg2.connect(**params)
        conn.set_client_encoding("UTF8")
        return conn
    except psycopg2.OperationalError as exc:
        log.error("PostgreSQL connection failed: %s", exc)
        sys.exit(1)


def _memory_db_path(agent_name: str) -> Path:
    """Return path to an agent's SQLite memory.db."""
    return _STORAGE_DIR / agent_name / "memory.db"


def _sqlite_connect(agent_name: str) -> sqlite3.Connection | None:
    """Open the agent's SQLite memory DB (read-only for export)."""
    db = _memory_db_path(agent_name)
    if not db.exists():
        log.warning("No memory.db found for agent '%s' at %s", agent_name, db)
        return None
    conn = sqlite3.connect(str(db), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# Anonymization
# ═══════════════════════════════════════════════════════════════════════════

def _anonymize_text(text: str) -> str:
    """Strip PII patterns from a text string."""
    result = text
    for label, pattern in _PII_PATTERNS:
        result = pattern.sub(f"[{label.upper()}_REDACTED]", result)
    return result


def _anonymize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Anonymize PII in a record's string fields (value, content)."""
    out = dict(record)
    for field in ("value", "content"):
        if field in out and isinstance(out[field], str):
            out[field] = _anonymize_text(out[field])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ISO timestamp helper
# ═══════════════════════════════════════════════════════════════════════════

def _to_iso(val: Any) -> str:
    """Convert various timestamp types to ISO 8601 UTC string."""
    if val is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat(timespec="seconds")
    # Already a string — normalise
    s = str(val).strip()
    if s and not s.endswith("Z") and "+" not in s:
        s += "Z"
    return s


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — memories to OAMF JSONL
# ═══════════════════════════════════════════════════════════════════════════

def _export_memories(agent_name: str, anonymize: bool = False) -> list[dict[str, Any]]:
    """Read memories from SQLite and return as list of OAMF dicts."""
    conn = _sqlite_connect(agent_name)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT key, value, category, importance, access_count, "
            "source, created_at, updated_at FROM memories ORDER BY importance DESC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("Could not read memories: %s", exc)
        return []
    finally:
        conn.close()

    entries: list[dict[str, Any]] = []
    for r in rows:
        entry = {
            "v": OAMF_VERSION,
            "key": r["key"],
            "value": r["value"],
            "cat": r["category"] or "semantic",
            "imp": r["importance"] if r["importance"] is not None else 5,
            "acc": r["access_count"] if r["access_count"] is not None else 0,
            "src": r["source"] or "user",
            "created": _to_iso(r["created_at"]),
            "updated": _to_iso(r["updated_at"]),
            "meta": {},
        }
        if anonymize:
            entry = _anonymize_record(entry)
        entries.append(entry)

    log.info("Exported %d memories for agent '%s'.", len(entries), agent_name)
    return entries


def _memories_to_jsonl(entries: list[dict[str, Any]]) -> str:
    """Serialize OAMF entries to JSONL text."""
    lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries]
    return "\n".join(lines) + ("\n" if lines else "")


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — chat history from PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════

def _export_history(agent_name: str, anonymize: bool = False) -> list[dict[str, Any]]:
    """Read chat history from PostgreSQL."""
    pg = _pg_connect()
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT role, content, metadata, created_at "
                "FROM aimos_chat_histories WHERE agent_name = %s "
                "ORDER BY id ASC",
                (agent_name,),
            )
            rows = cur.fetchall()
    finally:
        pg.close()

    entries: list[dict[str, Any]] = []
    for r in rows:
        meta = r["metadata"] if isinstance(r["metadata"], dict) else {}
        entry: dict[str, Any] = {
            "role": r["role"],
            "content": r["content"],
            "timestamp": _to_iso(r["created_at"]),
        }
        # Preserve channel / sender_id from metadata if present
        if meta.get("channel"):
            entry["channel"] = meta["channel"]
        if meta.get("sender_id"):
            entry["sender_id"] = str(meta["sender_id"])
        if anonymize:
            entry = _anonymize_record(entry)
        entries.append(entry)

    log.info("Exported %d history entries for agent '%s'.", len(entries), agent_name)
    return entries


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — agent config from PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════

def _load_agent_config(agent_name: str) -> dict[str, Any] | None:
    """Load agent row from PostgreSQL agents table. Returns config JSONB."""
    pg = _pg_connect()
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT name, config, created_at FROM agents WHERE name = %s",
                (agent_name,),
            )
            row = cur.fetchone()
    finally:
        pg.close()

    if row is None:
        log.error("Agent '%s' not found in PostgreSQL agents table.", agent_name)
        return None

    cfg = row["config"] if isinstance(row["config"], dict) else {}
    cfg["_pg_name"] = row["name"]
    cfg["_pg_created"] = _to_iso(row["created_at"])
    return cfg


def _config_to_ossa(agent_name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Convert AIMOS agent config JSONB to OSSA-compatible agent definition."""
    skills = config.get("skills", [])
    if isinstance(skills, dict):
        skills = list(skills.keys())
    connectors = config.get("connectors", [])
    if isinstance(connectors, dict):
        connectors = list(connectors.keys())

    return {
        "apiVersion": "oap/v1",
        "kind": "Agent",
        "metadata": {
            "name": agent_name,
            "displayName": config.get("display_name", agent_name.capitalize()),
            "language": config.get("language", "en"),
            "version": "1.0",
        },
        "spec": {
            "personality": {
                "description": config.get("description", ""),
                "style": config.get("style", ""),
                "tone": config.get("tone", ""),
            },
            "capabilities": {
                "skills": skills if isinstance(skills, list) else [],
                "connectors": connectors if isinstance(connectors, list) else [],
                "interAgent": {
                    "enabled": config.get("inter_agent", True),
                },
            },
            "security": {
                "editableCredentials": config.get("editable_credentials", []),
            },
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — skill state from SQLite
# ═══════════════════════════════════════════════════════════════════════════

def _export_skill_state(agent_name: str) -> dict[str, Any]:
    """Read skill_state from SQLite, return grouped by skill_name."""
    conn = _sqlite_connect(agent_name)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT skill_name, key, value, updated_at FROM skill_state ORDER BY skill_name, key"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("Could not read skill_state: %s", exc)
        return {}
    finally:
        conn.close()

    skills: dict[str, dict[str, Any]] = {}
    for r in rows:
        sname = r["skill_name"]
        if sname not in skills:
            skills[sname] = {}
        # Try JSON-decode value; fall back to string
        raw = r["value"]
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            val = raw
        skills[sname][r["key"]] = {
            "value": val,
            "updated": _to_iso(r["updated_at"]),
        }

    log.info("Exported skill state for %d skills of agent '%s'.", len(skills), agent_name)
    return skills


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — full .oap package
# ═══════════════════════════════════════════════════════════════════════════

def export_oap(agent_name: str, output_path: str, anonymize: bool = False) -> None:
    """Export a full Open Agent Package (.oap ZIP)."""
    config = _load_agent_config(agent_name)
    if config is None:
        sys.exit(1)

    memories = _export_memories(agent_name, anonymize=anonymize)
    history = _export_history(agent_name, anonymize=anonymize)
    skill_state = _export_skill_state(agent_name)
    ossa = _config_to_ossa(agent_name, config)

    # System prompt
    system_prompt = config.get("system_prompt", "")
    if anonymize and system_prompt:
        system_prompt = _anonymize_text(system_prompt)

    # Skills list from config
    skills_cfg = config.get("skills", [])

    # Build JSONL strings
    memory_jsonl = _memories_to_jsonl(memories)
    history_jsonl = "\n".join(
        json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in history
    ) + ("\n" if history else "")

    # Compute checksum over memory JSONL (primary data integrity)
    mem_hash = hashlib.sha256(memory_jsonl.encode("utf-8")).hexdigest()

    manifest = {
        "format": "oap",
        "version": OAP_FORMAT_VERSION,
        "agent_name": agent_name,
        "display_name": config.get("display_name", agent_name.capitalize()),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exported_from": f"AIMOS v{AIMOS_VERSION}",
        "memory_entries": len(memories),
        "history_entries": len(history),
        "language": config.get("language", "en"),
        "checksum": f"sha256:{mem_hash}",
    }
    if anonymize:
        manifest["anonymized"] = True

    # Write ZIP
    out = Path(output_path)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

        # Agent definition — prefer YAML, fall back to JSON
        if HAS_YAML:
            zf.writestr("agent.yaml", _yaml.dump(ossa, allow_unicode=True, default_flow_style=False, sort_keys=False))
        else:
            zf.writestr("agent.json", json.dumps(ossa, indent=2, ensure_ascii=False))

        zf.writestr("memory.oamf.jsonl", memory_jsonl)
        zf.writestr("history.jsonl", history_jsonl)
        zf.writestr("system_prompt.txt", system_prompt)
        zf.writestr("skills.json", json.dumps(
            {"skills_config": skills_cfg, "skill_state": skill_state},
            indent=2, ensure_ascii=False,
        ))

    size_kb = out.stat().st_size / 1024
    log.info(
        "Exported agent '%s' -> %s (%.1f KB, %d memories, %d history entries)",
        agent_name, out, size_kb, len(memories), len(history),
    )


# ═══════════════════════════════════════════════════════════════════════════
# EXPORT — memory-only OAMF JSONL
# ═══════════════════════════════════════════════════════════════════════════

def export_memory_only(agent_name: str, output_path: str, anonymize: bool = False) -> None:
    """Export agent memories as a standalone .oamf.jsonl file."""
    memories = _export_memories(agent_name, anonymize=anonymize)
    if not memories:
        log.warning("No memories to export for agent '%s'.", agent_name)
    text = _memories_to_jsonl(memories)
    Path(output_path).write_text(text, encoding="utf-8")
    log.info("Memory export -> %s (%d entries)", output_path, len(memories))


# ═══════════════════════════════════════════════════════════════════════════
# IMPORT — full .oap or memory-only OAMF
# ═══════════════════════════════════════════════════════════════════════════

def import_oap(file_path: str, with_history: bool = False) -> None:
    """Import an Open Agent Package (.oap) into AIMOS."""
    fp = Path(file_path)
    if not fp.exists():
        log.error("File not found: %s", fp)
        sys.exit(1)

    # Detect if it's a plain OAMF JSONL file (not a ZIP)
    if fp.suffix == ".jsonl" or not zipfile.is_zipfile(fp):
        _import_oamf_jsonl(fp)
        return

    with zipfile.ZipFile(fp, "r") as zf:
        names = zf.namelist()
        log.info("OAP contents: %s", ", ".join(names))

        # Read manifest
        if "manifest.json" not in names:
            log.error("Invalid .oap: missing manifest.json")
            sys.exit(1)
        manifest = json.loads(zf.read("manifest.json"))
        agent_name = manifest["agent_name"]
        log.info(
            "Importing agent '%s' (exported from %s, %d memories, %d history)",
            agent_name, manifest.get("exported_from", "unknown"),
            manifest.get("memory_entries", 0), manifest.get("history_entries", 0),
        )

        # Verify checksum if memory file present
        if "memory.oamf.jsonl" in names:
            mem_data = zf.read("memory.oamf.jsonl")
            actual_hash = hashlib.sha256(mem_data).hexdigest()
            expected = manifest.get("checksum", "")
            if expected.startswith("sha256:"):
                if actual_hash != expected[7:]:
                    log.error(
                        "Checksum mismatch! Expected %s, got sha256:%s. File may be corrupted.",
                        expected, actual_hash,
                    )
                    sys.exit(1)
                log.info("Checksum verified OK.")

        # 1. Import agent config to PostgreSQL
        agent_def = None
        for candidate in ("agent.yaml", "agent.json"):
            if candidate in names:
                raw = zf.read(candidate).decode("utf-8")
                if candidate.endswith(".yaml") and HAS_YAML:
                    agent_def = _yaml.safe_load(raw)
                else:
                    agent_def = json.loads(raw)
                break

        system_prompt = ""
        if "system_prompt.txt" in names:
            system_prompt = zf.read("system_prompt.txt").decode("utf-8")

        skills_data: dict[str, Any] = {}
        if "skills.json" in names:
            skills_data = json.loads(zf.read("skills.json"))

        _import_agent_config(agent_name, agent_def, system_prompt, skills_data, manifest)

        # 2. Import memories to SQLite
        if "memory.oamf.jsonl" in names:
            entries = _parse_oamf_jsonl(mem_data.decode("utf-8"))
            _import_memories_to_sqlite(agent_name, entries)

        # 3. Import skill state to SQLite
        if skills_data.get("skill_state"):
            _import_skill_state(agent_name, skills_data["skill_state"])

        # 4. Optionally import history
        if with_history and "history.jsonl" in names:
            hist_text = zf.read("history.jsonl").decode("utf-8")
            _import_history(agent_name, hist_text)
        elif "history.jsonl" in names and not with_history:
            log.info("Skipping history import (use --with-history to include).")

    log.info("Import of agent '%s' completed.", agent_name)


def _parse_oamf_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse OAMF JSONL text into a list of dicts."""
    entries = []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            log.warning("Skipping malformed OAMF line %d: %s", i, exc)
    return entries


def _import_oamf_jsonl(path: Path) -> None:
    """Import a standalone .oamf.jsonl file — needs --agent or filename hint."""
    text = path.read_text(encoding="utf-8")
    entries = _parse_oamf_jsonl(text)
    if not entries:
        log.warning("No memory entries found in %s", path)
        return

    # Derive agent name from filename: e.g. myagent.oamf.jsonl -> myagent
    stem = path.stem  # myagent.oamf
    agent_name = stem.split(".")[0]
    log.info("Importing %d memories for agent '%s' (from filename).", len(entries), agent_name)
    _import_memories_to_sqlite(agent_name, entries)
    log.info("Memory import completed for agent '%s'.", agent_name)


def _import_agent_config(
    agent_name: str,
    agent_def: dict[str, Any] | None,
    system_prompt: str,
    skills_data: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Upsert agent config into PostgreSQL agents table (merge mode)."""
    pg = _pg_connect()
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check if agent exists
            cur.execute("SELECT config FROM agents WHERE name = %s", (agent_name,))
            existing = cur.fetchone()

            # Build config JSONB from OSSA definition
            config: dict[str, Any] = {}
            if existing and existing["config"]:
                config = existing["config"] if isinstance(existing["config"], dict) else {}

            if agent_def:
                meta = agent_def.get("metadata", {})
                spec = agent_def.get("spec", {})
                personality = spec.get("personality", {})
                caps = spec.get("capabilities", {})

                config["display_name"] = meta.get("displayName", agent_name.capitalize())
                config["language"] = meta.get("language", config.get("language", "en"))
                config["description"] = personality.get("description", config.get("description", ""))
                config["style"] = personality.get("style", config.get("style", ""))
                config["tone"] = personality.get("tone", config.get("tone", ""))
                config["skills"] = caps.get("skills", config.get("skills", []))
                config["connectors"] = caps.get("connectors", config.get("connectors", []))
                config["inter_agent"] = caps.get("interAgent", {}).get("enabled", True)
                config["editable_credentials"] = spec.get("security", {}).get(
                    "editableCredentials", config.get("editable_credentials", [])
                )

            if system_prompt:
                config["system_prompt"] = system_prompt

            if skills_data.get("skills_config"):
                config["skills"] = skills_data["skills_config"]

            config_json = json.dumps(config, ensure_ascii=False)

            if existing:
                cur.execute(
                    "UPDATE agents SET config = %s::jsonb, updated_at = NOW() WHERE name = %s",
                    (config_json, agent_name),
                )
                log.info("Updated existing agent '%s' in PostgreSQL (merge mode).", agent_name)
            else:
                cur.execute(
                    "INSERT INTO agents (name, config, status) VALUES (%s, %s::jsonb, 'idle')",
                    (agent_name, config_json),
                )
                log.info("Created new agent '%s' in PostgreSQL.", agent_name)

            pg.commit()
    finally:
        pg.close()


def _import_memories_to_sqlite(agent_name: str, entries: list[dict[str, Any]]) -> None:
    """Write OAMF entries into the agent's SQLite memories table (upsert)."""
    db_path = _memory_db_path(agent_name)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    # Ensure table exists
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
            updated_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    imported = 0
    updated = 0
    for e in entries:
        key = e.get("key", "")
        if not key:
            continue

        value = e.get("value", "")
        category = e.get("cat", e.get("category", "semantic"))
        importance = e.get("imp", e.get("importance", 5))
        access_count = e.get("acc", e.get("access_count", 0))
        source = e.get("src", e.get("source", "import"))
        created = e.get("created", e.get("created_at", ""))
        updated_ts = e.get("updated", e.get("updated_at", ""))

        # Upsert: newer timestamp wins (merge semantics per OAMF spec sec 5)
        existing = conn.execute("SELECT updated_at, access_count FROM memories WHERE key = ?", (key,)).fetchone()
        if existing:
            existing_ts = existing[0] or ""
            if updated_ts >= existing_ts:
                conn.execute(
                    "UPDATE memories SET value=?, category=?, importance=?, "
                    "access_count=MAX(access_count, ?), source='import', updated_at=? "
                    "WHERE key=?",
                    (value, category, importance, access_count, updated_ts, key),
                )
                updated += 1
        else:
            conn.execute(
                "INSERT INTO memories (key, value, category, importance, access_count, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, value, category, importance, access_count, "import", created, updated_ts),
            )
            imported += 1

    conn.commit()
    conn.close()
    log.info("Memories: %d new, %d updated for agent '%s'.", imported, updated, agent_name)


def _import_skill_state(agent_name: str, skill_state: dict[str, dict[str, Any]]) -> None:
    """Write skill state entries to SQLite skill_state table."""
    db_path = _memory_db_path(agent_name)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_state (
            skill_name TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (skill_name, key)
        )
    """)

    count = 0
    for skill_name, kv_pairs in skill_state.items():
        for key, data in kv_pairs.items():
            val = data.get("value", data) if isinstance(data, dict) else data
            val_str = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
            updated = data.get("updated", "") if isinstance(data, dict) else ""
            conn.execute(
                "INSERT OR REPLACE INTO skill_state (skill_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (skill_name, key, val_str, updated or datetime.now(timezone.utc).isoformat()),
            )
            count += 1

    conn.commit()
    conn.close()
    log.info("Imported %d skill state entries for agent '%s'.", count, agent_name)


def _import_history(agent_name: str, history_text: str) -> None:
    """Import chat history entries into PostgreSQL aimos_chat_histories."""
    entries = []
    for line in history_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        log.info("No history entries to import.")
        return

    pg = _pg_connect()
    try:
        with pg.cursor() as cur:
            for e in entries:
                role = e.get("role", "user")
                content = e.get("content", "")
                meta: dict[str, Any] = {"imported": True}
                if e.get("channel"):
                    meta["channel"] = e["channel"]
                if e.get("sender_id"):
                    meta["sender_id"] = e["sender_id"]

                cur.execute(
                    "INSERT INTO aimos_chat_histories (agent_name, role, content, metadata) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (agent_name, role, content, json.dumps(meta, ensure_ascii=False)),
                )
            pg.commit()
        log.info("Imported %d history entries for agent '%s'.", len(entries), agent_name)
    finally:
        pg.close()


# ═══════════════════════════════════════════════════════════════════════════
# LIST — show all agents
# ═══════════════════════════════════════════════════════════════════════════

def list_agents() -> None:
    """List all agents from PostgreSQL and their memory stats."""
    pg = _pg_connect()
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT name, status, config, created_at, updated_at "
                "FROM agents ORDER BY name"
            )
            rows = cur.fetchall()
    finally:
        pg.close()

    if not rows:
        print("No agents found.")
        return

    print(f"\n{'Name':<16} {'Status':<10} {'Language':<6} {'Memories':<10} {'Skills':<8} {'Created'}")
    print("-" * 80)

    for r in rows:
        name = r["name"]
        status = r["status"] or "idle"
        cfg = r["config"] if isinstance(r["config"], dict) else {}
        lang = cfg.get("language", "?")
        skills = cfg.get("skills", [])
        skill_count = len(skills) if isinstance(skills, list) else 0

        # Count memories from SQLite
        mem_count = 0
        db = _memory_db_path(name)
        if db.exists():
            try:
                sconn = sqlite3.connect(str(db), timeout=2)
                mem_count = sconn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                sconn.close()
            except Exception:
                mem_count = -1

        created = _to_iso(r["created_at"])[:10]
        print(f"{name:<16} {status:<10} {lang:<6} {mem_count:<10} {skill_count:<8} {created}")

    print()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AIMOS Open Agent Package (OAP) — Export / Import CLI (CR-121)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --agent myagent --output myagent.oap
  %(prog)s --agent myagent --memory-only --output myagent.oamf.jsonl
  %(prog)s --import myagent.oap
  %(prog)s --import myagent.oap --with-history
  %(prog)s --agent myagent --anonymize --output agent_clean.oap
  %(prog)s --list
        """,
    )

    parser.add_argument("--agent", "-a", help="Agent name (for export)")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--import", dest="import_file", metavar="FILE",
                        help="Import .oap or .oamf.jsonl file")
    parser.add_argument("--with-history", action="store_true",
                        help="Include chat history on import")
    parser.add_argument("--memory-only", action="store_true",
                        help="Export memories only (OAMF JSONL)")
    parser.add_argument("--anonymize", action="store_true",
                        help="Strip PII patterns before export")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all agents")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug-level logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Dispatch ──────────────────────────────────────────────────────────
    if args.list:
        list_agents()
        return

    if args.import_file:
        import_oap(args.import_file, with_history=args.with_history)
        return

    if not args.agent:
        parser.error("--agent is required for export (or use --import / --list)")

    if args.memory_only:
        output = args.output or f"{args.agent}.oamf.jsonl"
        export_memory_only(args.agent, output, anonymize=args.anonymize)
    else:
        output = args.output or f"{args.agent}.oap"
        export_oap(args.agent, output, anonymize=args.anonymize)


if __name__ == "__main__":
    main()
