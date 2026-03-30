"""
AIMOS Test Fixtures — CR-223
==============================
Shared fixtures for E2E and unit tests.
"""

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _db_reachable() -> bool:
    """Quick sync check whether the AIMOS database is reachable."""
    try:
        import psycopg2
        from core.config import Config
        conn = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT,
            dbname=Config.PG_DB, user=Config.PG_USER,
            password=Config.PG_PASSWORD, connect_timeout=5,
        )
        conn.close()
        return True
    except Exception:
        return False


# Cache DB reachability once per session
_DB_OK: bool | None = None


def _check_db():
    global _DB_OK
    if _DB_OK is None:
        _DB_OK = _db_reachable()
    return _DB_OK


@pytest.fixture
async def db_pool():
    """Create an asyncpg connection pool for the current test.

    Skips if DB is unreachable.
    """
    if not _check_db():
        pytest.skip("Database unavailable")

    import asyncpg
    from core.config import Config

    pool = await asyncpg.create_pool(
        host=Config.PG_HOST, port=Config.PG_PORT,
        database=Config.PG_DB, user=Config.PG_USER,
        password=Config.PG_PASSWORD,
        min_size=1, max_size=3, ssl=False,
        command_timeout=10,
    )
    yield pool
    await pool.close()
