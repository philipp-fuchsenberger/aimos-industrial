"""
CR-158: Shared psycopg2 connection pool for non-async components.
=================================================================
Prevents PostgreSQL max_connections exhaustion under dashboard load.
Used by dashboard app.py / routes.py. FMEA RPN 162.

CR-190: Added db_connection() context manager — guarantees release.
"""
import logging
from contextlib import contextmanager

import psycopg2.pool
import psycopg2.extras

from core.config import Config

_log = logging.getLogger("AIMOS.db_pool")
_pool = None


def get_pool():
    """Return (or create) the module-level ThreadedConnectionPool."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=Config.PG_HOST,
            port=Config.PG_PORT,
            dbname=Config.PG_DB,
            user=Config.PG_USER,
            password=Config.PG_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=5,
        )
        _log.info("DB pool created (2-10 connections)")
    return _pool


def get_conn():
    """Get a connection from the pool."""
    conn = get_pool().getconn()
    conn.autocommit = False
    return conn


def put_conn(conn):
    """Return a connection to the pool (safe to call even on error)."""
    try:
        if _pool and not _pool.closed:
            _pool.putconn(conn)
    except Exception:
        pass


@contextmanager
def db_connection():
    """Context manager for pooled DB connections. Guarantees release."""
    conn = get_conn()
    try:
        yield conn
    finally:
        put_conn(conn)
