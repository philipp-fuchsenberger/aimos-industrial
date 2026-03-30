#!/usr/bin/env python3
"""
AIMOS CR Validation Script — End-to-End Pipeline Test
=======================================================
Tests the full message pipeline:
  1. Inject a test message into pending_messages
  2. Verify DB write succeeded
  3. Check orchestrator reaction (agent spawned or already running)
  4. Wait for agent to process the message (processed=TRUE)
  5. Verify outbound reply written to DB

Usage:
  python scripts/validate_cr.py                    # test with agent 'agent1'
  python scripts/validate_cr.py --agent agent2      # test with specific agent
  python scripts/validate_cr.py --dry-run           # only check DB connectivity
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("FAIL: psycopg2 not installed")
    sys.exit(1)

from core.config import Config

# ── Helpers ──────────────────────────────────────────────────────────────────

_PASS = "\033[92mPASS\033[0m"
_FAIL = "\033[91mFAIL\033[0m"
_SKIP = "\033[93mSKIP\033[0m"
_INFO = "\033[94mINFO\033[0m"


def _db():
    """Connect to AIMOS database."""
    return psycopg2.connect(
        host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
        user=Config.PG_USER, password=Config.PG_PASSWORD,
        connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _print(tag: str, msg: str):
    print(f"  [{tag}] {msg}")


# ── Test Steps ───────────────────────────────────────────────────────────────

def test_db_connection() -> bool:
    """Step 0: Verify database connectivity."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        _print(_PASS, "DB connection OK")
        return True
    except Exception as exc:
        _print(_FAIL, f"DB connection failed: {exc}")
        return False


def test_tables_exist() -> bool:
    """Step 1: Verify required tables exist."""
    required = {"agents", "pending_messages", "aimos_chat_histories", "global_settings"}
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            existing = {r["tablename"] for r in cur.fetchall()}
        conn.close()
        missing = required - existing
        if missing:
            _print(_FAIL, f"Missing tables: {missing}")
            return False
        _print(_PASS, f"All required tables present ({len(required)})")
        return True
    except Exception as exc:
        _print(_FAIL, f"Table check failed: {exc}")
        return False


def test_agent_exists(agent_name: str) -> bool:
    """Step 2: Verify agent is registered in DB."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT name, status, config FROM agents WHERE name=%s", (agent_name,))
            row = cur.fetchone()
        conn.close()
        if not row:
            _print(_FAIL, f"Agent '{agent_name}' not found in DB")
            return False
        cfg = row["config"] or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        skills = cfg.get("skills", cfg.get("modules", []))
        _print(_PASS, f"Agent '{agent_name}' found (status={row['status']}, skills={skills})")
        return True
    except Exception as exc:
        _print(_FAIL, f"Agent check failed: {exc}")
        return False


def test_skill_registry() -> bool:
    """Step 3: Verify SKILL_REGISTRY loads without errors."""
    try:
        from core.skills import SKILL_REGISTRY
        names = list(SKILL_REGISTRY.keys())
        _print(_PASS, f"SKILL_REGISTRY loaded: {names}")
        return True
    except Exception as exc:
        _print(_FAIL, f"SKILL_REGISTRY import failed: {exc}")
        return False


def test_workspace(agent_name: str) -> bool:
    """Step 4: Verify agent workspace structure."""
    base = Path("storage") / "agents" / agent_name
    public = base / "public"
    ok = True
    if not base.exists():
        _print(_FAIL, f"Workspace missing: {base}")
        ok = False
    else:
        _print(_PASS, f"Workspace exists: {base}")
    if not public.exists():
        _print(_FAIL, f"Public folder missing: {public}")
        ok = False
    else:
        _print(_PASS, f"Public folder exists: {public}")
    return ok


def test_inject_message(agent_name: str) -> int | None:
    """Step 5: Inject test message into pending_messages."""
    content = f"[VALIDATE_CR] Test message at {time.strftime('%H:%M:%S')}"
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_messages (agent_name, sender_id, content, kind) "
                "VALUES (%s, 0, %s, 'dashboard') RETURNING id",
                (agent_name, content),
            )
            msg_id = cur.fetchone()["id"]
        conn.commit()
        conn.close()
        _print(_PASS, f"Test message injected: id={msg_id}")
        return msg_id
    except Exception as exc:
        _print(_FAIL, f"Message injection failed: {exc}")
        return None


def test_message_processed(msg_id: int, timeout: int = 30) -> bool:
    """Step 6: Wait for message to be processed by agent."""
    _print(_INFO, f"Waiting up to {timeout}s for message #{msg_id} to be processed...")
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            conn = _db()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT processed FROM pending_messages WHERE id=%s", (msg_id,)
                )
                row = cur.fetchone()
            conn.close()
            if row and row["processed"]:
                elapsed = time.monotonic() - start
                _print(_PASS, f"Message #{msg_id} processed in {elapsed:.1f}s")
                return True
        except Exception:
            pass
        time.sleep(2)
    _print(_FAIL, f"Message #{msg_id} NOT processed within {timeout}s")
    return False


def test_outbound_exists(agent_name: str, after_id: int) -> bool:
    """Step 7: Check if agent wrote an outbound reply."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content FROM pending_messages "
                "WHERE agent_name=%s AND kind='outbound_telegram' AND id > %s "
                "ORDER BY id DESC LIMIT 1",
                (agent_name, after_id),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            preview = row["content"][:80] if row["content"] else "(empty)"
            _print(_PASS, f"Outbound reply found: id={row['id']} — {preview}")
            return True
        # Dashboard messages don't produce outbound_telegram — check chat_histories
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content FROM aimos_chat_histories "
                "WHERE agent_name=%s AND role='assistant' "
                "ORDER BY id DESC LIMIT 1",
                (agent_name,),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            preview = row["content"][:80] if row["content"] else "(empty)"
            _print(_PASS, f"Agent reply in chat_histories: id={row['id']} — {preview}")
            return True
        _print(_FAIL, "No outbound reply or chat_history entry found")
        return False
    except Exception as exc:
        _print(_FAIL, f"Outbound check failed: {exc}")
        return False


def test_agent_jobs_table() -> bool:
    """Step 8: Verify agent_jobs table exists."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM agent_jobs LIMIT 0")
        conn.close()
        _print(_PASS, "agent_jobs table exists")
        return True
    except Exception:
        _print(_FAIL, "agent_jobs table missing")
        return False


def test_vault_anonymization(agent_name: str) -> bool:
    """Step 9: Verify Vault anonymizes PII and deanonymizes correctly."""
    try:
        from core.skills.skill_hybrid_reasoning import Vault
        from core.skills.base import BaseSkill
        db_path = BaseSkill.memory_db_path(agent_name)

        vault = Vault(db_path, level="strict")
        session = "test_validate_cr"

        test_text = (
            "Bitte kontaktiere philipp@fuchsenberger.de oder ruf +49 171 1234567 an. "
            "Der API-Key ist sk-abc123456789012345678901234567890."
        )

        anonymized = vault.anonymize(test_text, session, extra_secrets={})

        # Verify PII is gone
        if "philipp@fuchsenberger.de" in anonymized:
            _print(_FAIL, "Vault: email NOT anonymized")
            return False
        if "+49 171 1234567" in anonymized:
            _print(_FAIL, "Vault: phone NOT anonymized")
            return False
        if "__VAULT_" not in anonymized:
            _print(_FAIL, "Vault: no placeholders found in anonymized text")
            return False

        # Verify deanonymization
        restored = vault.deanonymize(anonymized, session)
        if "philipp@fuchsenberger.de" not in restored:
            _print(_FAIL, "Vault: email NOT restored after deanonymization")
            return False

        _print(_PASS, f"Vault: anonymized {anonymized.count('__VAULT_')} items, deanonymization OK")

        # Cleanup test session
        import sqlite3
        c = sqlite3.connect(str(db_path))
        c.execute("DELETE FROM vault_mappings WHERE session_id=?", (session,))
        c.commit()
        c.close()

        return True
    except Exception as exc:
        _print(_FAIL, f"Vault test failed: {exc}")
        return False


def cleanup_test_messages(agent_name: str):
    """Remove test messages from DB."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pending_messages WHERE agent_name=%s AND content LIKE '%%VALIDATE_CR%%'",
                (agent_name,),
            )
            deleted = cur.rowcount
        conn.commit()
        conn.close()
        if deleted:
            _print(_INFO, f"Cleaned up {deleted} test message(s)")
    except Exception:
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AIMOS CR Validation")
    parser.add_argument("--agent", default="agent1", help="Agent to test (default: neo)")
    parser.add_argument("--dry-run", action="store_true", help="Only check DB + structure, no message injection")
    parser.add_argument("--timeout", type=int, default=30, help="Seconds to wait for processing (default: 30)")
    args = parser.parse_args()

    print(f"\n{'=' * 50}")
    print(f"  AIMOS CR Validation — Agent: {args.agent}")
    print(f"{'=' * 50}\n")

    results = []

    # Structural tests (always run)
    results.append(("DB Connection", test_db_connection()))
    if not results[-1][1]:
        print(f"\n  ABORT: Cannot connect to database.\n")
        sys.exit(1)

    results.append(("Tables", test_tables_exist()))
    results.append(("Agent Exists", test_agent_exists(args.agent)))
    results.append(("SKILL_REGISTRY", test_skill_registry()))
    results.append(("Workspace", test_workspace(args.agent)))
    results.append(("agent_jobs Table", test_agent_jobs_table()))
    results.append(("Vault Anonymization", test_vault_anonymization(args.agent)))

    if args.dry_run:
        _print(_SKIP, "Dry-run mode — skipping message injection")
    else:
        # Pipeline test: inject → process → verify
        msg_id = test_inject_message(args.agent)
        results.append(("Inject Message", msg_id is not None))

        if msg_id:
            processed = test_message_processed(msg_id, timeout=args.timeout)
            results.append(("Message Processed", processed))

            if processed:
                results.append(("Outbound Reply", test_outbound_exists(args.agent, msg_id)))

            cleanup_test_messages(args.agent)

    # Summary
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 50}")
    print(f"  Results: {passed}/{total} passed")
    print(f"{'=' * 50}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
