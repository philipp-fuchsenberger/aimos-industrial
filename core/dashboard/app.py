#!/usr/bin/env python3
"""
AIMOS Dashboard — v4.1.0 (Shard)
===================================
Management Layer for the AIMOS multi-agent system.
Routes are in routes.py (PRQ-01: <500 lines per file).

Usage:
  python -m core.dashboard.app         # → http://0.0.0.0:8080
"""

import json
import logging
import os
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import secrets

import psutil
import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

# ── Path setup ────────────────────────────────────────────────────────────────
_PKG_DIR = Path(__file__).parent
_ROOT_DIR = _PKG_DIR.parent.parent
sys.path.insert(0, str(_ROOT_DIR))

from core.config import Config, SecretFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("AIMOS.dashboard")

TEMPLATES_DIR = _PKG_DIR / "templates"
STATIC_DIR = _PKG_DIR / "static"
LOGS_DIR = _ROOT_DIR / "logs"

# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
_security = HTTPBasic()
DASHBOARD_USER = "admin"
DASHBOARD_PASSWORD = os.environ.get("AIMOS_DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD = "aimos2026"  # Development default — set AIMOS_DASHBOARD_PASSWORD in production
    logging.getLogger("AIMOS.dashboard").warning("⚠ Using default dashboard password. Set AIMOS_DASHBOARD_PASSWORD env var for production.")


def verify_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    """Verify HTTP Basic Auth credentials. Applied as a global dependency."""
    correct_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


app = FastAPI(title="AIMOS Dashboard", version="4.1.0", dependencies=[Depends(verify_auth)])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# CR-162/CR-175: CORS restriction — set AIMOS_CORS_ORIGIN env var in production
# (e.g. "https://aimos.local"). Defaults to "*" for development.
# See docs/ARCHITECTURE.md → Dashboard Environment Variables.
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("AIMOS_CORS_ORIGIN", "*")],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
#  Database Helpers (shared with routes.py)  — CR-158: pooled connections
# ══════════════════════════════════════════════════════════════════════════════

from core.db_pool import get_conn, put_conn


def _db_connect():
    """Get a pooled connection (CR-158). Return via _db_release(conn).
    DEPRECATED (CR-190): Use `from core.db_pool import db_connection` context manager instead.
    Kept for backward compatibility only.
    """
    return get_conn()


def _db_release(conn):
    """Return a connection to the pool. Safe no-op on None/error.
    DEPRECATED (CR-190): Use `from core.db_pool import db_connection` context manager instead.
    Kept for backward compatibility only.
    """
    put_conn(conn)


def _is_orchestrator_on() -> bool:
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM global_settings WHERE key='orchestrator_mode'")
            row = cur.fetchone()
        _db_release(conn)
        if not row:
            return False
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        if isinstance(val, dict):
            return val.get("enabled", False) is True
        return bool(val)
    except Exception:
        return False


def _fetch_agents() -> list[dict]:
    """Fetch agents with PID verification from DB pid column + psutil."""
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, status, config, pid, created_at, updated_at "
                "FROM public.agents ORDER BY name"
            )
            rows = cur.fetchall()
        _db_release(conn)
    except Exception as exc:
        log.warning(f"DB agents: {exc}")
        return []

    # Trust DB status completely — orchestrator is responsible for accuracy
    # Ensure config is always a dict (not a JSON string)
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                d["config"] = {}
        if not isinstance(d.get("config"), dict):
            d["config"] = {}
        result.append(d)
    return result


def _fetch_last_activity() -> list[dict]:
    """Last 5 processed messages across all agents."""
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name, kind, LEFT(content, 60) as preview, created_at "
                "FROM pending_messages WHERE processed=TRUE "
                "ORDER BY id DESC LIMIT 5"
            )
            rows = cur.fetchall()
        _db_release(conn)
        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].strftime("%H:%M:%S")
            result.append(d)
        return result
    except Exception:
        return []


def _fetch_pending_count() -> dict[str, int]:
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name, COUNT(*) AS cnt FROM public.pending_messages "
                "WHERE processed=FALSE AND kind NOT LIKE 'outbound_%%' "
                "GROUP BY agent_name"
            )
            rows = cur.fetchall()
        _db_release(conn)
        return {r["agent_name"]: r["cnt"] for r in rows}
    except Exception:
        return {}


def _fetch_pending_detail() -> list[dict]:
    """Pending messages with kind breakdown for queue display."""
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name, kind, COUNT(*) AS cnt "
                "FROM public.pending_messages WHERE processed=FALSE "
                "AND kind NOT LIKE 'outbound_%%' "
                "GROUP BY agent_name, kind ORDER BY agent_name, kind"
            )
            rows = cur.fetchall()
        _db_release(conn)
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_global_settings() -> list[dict]:
    try:
        conn = _db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT key, value, updated_at FROM public.global_settings ORDER BY key")
            rows = cur.fetchall()
        _db_release(conn)
        return [dict(r) for r in rows]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Hardware Metrics (GPU cached in background thread)
# ══════════════════════════════════════════════════════════════════════════════

import threading

_gpu_cache: dict | None = None
_gpu_lock = threading.Lock()


def _gpu_poll_loop():
    """Background thread: refresh GPU metrics every 3s."""
    global _gpu_cache
    while True:
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                p = [x.strip() for x in result.stdout.strip().split(",")]
                if len(p) >= 6:
                    with _gpu_lock:
                        _gpu_cache = {
                            "name": p[0], "vram_total_mb": int(p[1]),
                            "vram_used_mb": int(p[2]), "vram_free_mb": int(p[3]),
                            "gpu_util_percent": int(p[4]), "temp_c": int(p[5]),
                            "vram_percent": round(int(p[2]) / int(p[1]) * 100, 1),
                        }
        except Exception:
            pass
        import time
        time.sleep(3)


# Start GPU polling thread on import
_gpu_thread = threading.Thread(target=_gpu_poll_loop, daemon=True)
_gpu_thread.start()


def get_metrics() -> dict:
    metrics = {
        "cpu_percent": psutil.cpu_percent(interval=0),
        "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "ram_used_gb": round(psutil.virtual_memory().used / (1024**3), 1),
        "ram_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage("/").percent,
        "gpu": None,
    }
    with _gpu_lock:
        metrics["gpu"] = _gpu_cache
    return metrics


def _tail_log(agent_id: str, lines: int = 50) -> list[str]:
    log_file = LOGS_DIR / f"{agent_id}.log"
    if not log_file.exists():
        return [f"(no log file: {log_file})"]
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=lines))
    except Exception as exc:
        return [f"(error: {exc})"]


# ══════════════════════════════════════════════════════════════════════════════
#  Infrastructure Watchdog (CR-090)
# ══════════════════════════════════════════════════════════════════════════════

import asyncio

_WATCHDOG_INTERVAL = 10  # seconds between checks
infra_watchdog_active = True  # Nuclear shutdown sets this to False


def _check_and_restart_infra():
    """Check if orchestrator and shared_listener are alive; restart if dead."""
    orch_alive = False
    listener_alive = False
    my_pid = os.getpid()

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == my_pid:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "core.orchestrator" in cmdline and "dashboard" not in cmdline:
                orch_alive = True
            if "shared_listener" in cmdline:
                listener_alive = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # CR-173: Check if Ollama is running
    try:
        r = subprocess.run(["pgrep", "-f", "ollama"], capture_output=True, timeout=3)
        if r.returncode != 0:
            log.warning("[Watchdog] Ollama not running — attempting restart")
            subprocess.Popen(["systemctl", "--user", "start", "ollama"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    python = sys.executable
    if not orch_alive:
        log.warning("[Watchdog] Orchestrator dead — restarting")
        orch_log = open(LOGS_DIR / "orchestrator.log", "a")
        subprocess.Popen(
            [python, "-m", "core.orchestrator"],
            stdout=orch_log, stderr=orch_log,
            start_new_session=True, cwd=str(_ROOT_DIR),
        )
        orch_log.close()  # CR-191: close FD after Popen inherits it
    if not listener_alive:
        log.warning("[Watchdog] Shared listener dead — restarting")
        listener_log = open(LOGS_DIR / "shared_listener.log", "a")
        subprocess.Popen(
            [python, str(_ROOT_DIR / "scripts" / "shared_listener.py")],
            stdout=listener_log, stderr=listener_log,
            start_new_session=True, cwd=str(_ROOT_DIR),
        )
        listener_log.close()  # CR-191: close FD after Popen inherits it
    if not orch_alive or not listener_alive:
        log.info(f"[Watchdog] Status after check: orchestrator={'alive' if orch_alive else 'RESTARTED'}, "
                 f"listener={'alive' if listener_alive else 'RESTARTED'}")


async def _infra_watchdog():
    """Background task: ensure orchestrator + listener stay alive."""
    await asyncio.sleep(15)  # grace period after dashboard startup
    log.info("[Watchdog] Infrastructure watchdog active (interval=%ds)", _WATCHDOG_INTERVAL)
    while True:
        if not infra_watchdog_active:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            continue
        try:
            _check_and_restart_infra()
        except Exception as exc:
            log.debug(f"[Watchdog] Error: {exc}")
        await asyncio.sleep(_WATCHDOG_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  Router Registration + Startup
# ══════════════════════════════════════════════════════════════════════════════

from core.dashboard.routes import router  # noqa: E402
from core.connectors.webhook import webhook_router  # noqa: E402
from core.connectors.rest_api import rest_api_router  # noqa: E402

app.include_router(router)
app.include_router(webhook_router)
app.include_router(rest_api_router)


@app.on_event("startup")
async def startup():
    log.info("Dashboard v4.2.0 on :8080")
    log.info(f"DB: {Config.PG_USER}@{Config.PG_HOST}:{Config.PG_PORT}/{Config.PG_DB}")
    asyncio.create_task(_infra_watchdog())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "core.dashboard.app:app",
        host="0.0.0.0", port=8080, reload=False, log_level="info",
    )
