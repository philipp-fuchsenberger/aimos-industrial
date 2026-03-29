#!/usr/bin/env python3
"""
AIMOS Orchestrator — v4.1.0 (Daemon Mode)
=============================================
Non-blocking multi-agent manager. Runs as infinite loop.

Every 2s:
  1. Scan pending_messages for ALL agents with unprocessed work
  2. For each: is a process already running? If no → spawn it
  3. Clean up dead processes (zombie detection)

The orchestrator NEVER waits for an agent to finish (.wait()).
Agents manage their own lifecycle (90s watchdog, self-shutdown).

Usage:
  python -m core.orchestrator           # daemon mode (infinite)
  python -m core.orchestrator --once    # single scan, then exit
"""

import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# CR-109: Ensure CUDA libs are in LD_LIBRARY_PATH for Whisper STT in agent subprocesses.
# Needed when orchestrator is started from dashboard (not via start_clean.sh).
try:
    import nvidia.cublas.lib
    import nvidia.cudnn.lib
    _cuda_paths = nvidia.cublas.lib.__path__[0] + ":" + nvidia.cudnn.lib.__path__[0]
    os.environ["LD_LIBRARY_PATH"] = _cuda_paths + ":" + os.environ.get("LD_LIBRARY_PATH", "")
except ImportError:
    pass  # nvidia libs not installed — Whisper will use CPU or fail gracefully

import psutil
import psycopg2
import psycopg2.extras
from core.config import Config

_ROOT = Path(__file__).parent.parent

# Logging: console + logs/orchestrator.log
_log_dir = _ROOT / "logs"
_log_dir.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = RotatingFileHandler(
    _log_dir / "orchestrator.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger("AIMOS.orchestrator")
_POLL_INTERVAL = 2.0

# Track all spawned agent processes: name → Popen
_agents: dict[str, subprocess.Popen] = {}

# CR-090: Dreaming — track which agents already dreamed this idle period
_dreamed_this_idle: set[str] = set()

# CR-164: GPU Mutex — file lock to prevent concurrent GPU access
_GPU_LOCK = "/tmp/aimos_gpu.lock"
_gpu_locks: dict[str, object] = {}  # agent_name → lock file handle


def _acquire_gpu():
    """Acquire GPU mutex. Returns lock file handle or None.

    CR-164b: If lock file exists but holder PID is dead → force-release stale lock.
    This prevents deadlocks after unclean shutdown (kill -9, start_clean.sh).
    """
    lock_path = Path(_GPU_LOCK)

    # Check for stale lock: if lock file exists, check if holder is still alive
    if lock_path.exists():
        try:
            fh_test = open(_GPU_LOCK, 'r')
            try:
                fcntl.flock(fh_test, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Lock acquired immediately → it was stale (no process held it)
                fcntl.flock(fh_test, fcntl.LOCK_UN)
                fh_test.close()
                log.warning("[CR-164b] Stale GPU lock detected — no process holds it. Cleaning up.")
            except (IOError, OSError):
                # Lock is actually held by a live process — check if it's an AIMOS agent
                fh_test.close()
                # Check how old the lock file is — if > 15 min, force release
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > 900:  # 15 minutes
                        log.warning(f"[CR-164b] GPU lock held for {age:.0f}s (>15min) — force releasing stale lock")
                        lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    try:
        fh = open(_GPU_LOCK, 'w')
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Touch the file so we can track lock age
        lock_path.touch()
        return fh
    except (IOError, OSError):
        return None


def _release_gpu(fh):
    if fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass


# CR-187: Spawn Backoff — track consecutive spawn failures per agent
_spawn_failures: dict[str, int] = {}
_spawn_backoff: dict[str, float] = {}  # agent_name → next allowed spawn time (monotonic)
_spawn_times: dict[str, float] = {}    # agent_name → last spawn time (monotonic)
_BACKOFF_STEPS = [30, 60, 300]  # seconds: escalating backoff after >3 failures


# ── DB ────────────────────────────────────────────────────────────────────────

def _db():
    """Connect with auto-retry on stale/broken connections."""
    for attempt in range(2):
        try:
            conn = psycopg2.connect(
                host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
                user=Config.PG_USER, password=Config.PG_PASSWORD,
                connect_timeout=3, cursor_factory=psycopg2.extras.RealDictCursor,
            )
            conn.autocommit = True
            return conn
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            if attempt == 0:
                continue
            raise


def _is_enabled() -> bool:
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute("SELECT value FROM global_settings WHERE key='orchestrator_mode'")
            row = cur.fetchone()
        c.close()
        if not row:
            return False
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        return val.get("enabled", False) is True if isinstance(val, dict) else bool(val)
    except Exception:
        return False


def _get_agents_with_pending() -> list[str]:
    """Return agent names with unprocessed INPUT messages, prioritized.

    CR-110: Real user messages (telegram, email, voice) get priority over
    scheduled jobs and internal messages. This prevents cronjob-heavy agents
    from starving agents with actual user requests.
    """
    try:
        c = _db()
        with c.cursor() as cur:
            # Get agents with REAL user messages first (telegram, email, voice)
            cur.execute(
                "SELECT DISTINCT agent_name FROM pending_messages "
                "WHERE processed=FALSE AND kind IN ('telegram', 'telegram_voice', 'telegram_doc', 'email', 'voice_local')"
            )
            priority = [r["agent_name"] for r in cur.fetchall()]

            # Then agents with other pending messages (scheduled_job, text, internal)
            cur.execute(
                "SELECT DISTINCT agent_name FROM pending_messages "
                "WHERE processed=FALSE AND kind NOT LIKE 'outbound_%'"
            )
            all_pending = [r["agent_name"] for r in cur.fetchall()]
        c.close()

        # CR-135: Filter out inactive agents (config.active=false)
        inactive = set()
        try:
            c2 = _db()
            with c2.cursor() as cur2:
                cur2.execute("SELECT name, config FROM agents")
                for row in cur2.fetchall():
                    cfg = row["config"]
                    if isinstance(cfg, str):
                        import json as _json
                        cfg = _json.loads(cfg)
                    if isinstance(cfg, dict) and cfg.get("active") is False:
                        inactive.add(row["name"])
            c2.close()
        except Exception:
            pass

        # Priority agents first, then the rest (deduplicated, inactive excluded)
        seen = set()
        result = []
        for name in priority + all_pending:
            if name not in seen and name not in inactive:
                seen.add(name)
                result.append(name)
        return result
    except Exception:
        return []


def _set_status(name: str, status: str, pid: int = None):
    try:
        c = _db()
        with c.cursor() as cur:
            if pid is not None:
                cur.execute("UPDATE agents SET status=%s, updated_at=NOW(), pid=%s WHERE name=%s", (status, pid, name))
            else:
                cur.execute("UPDATE agents SET status=%s, updated_at=NOW(), pid=NULL WHERE name=%s", (status, name))
        c.commit()
        c.close()
    except Exception:
        pass


def _load_env(name: str) -> dict[str, str]:
    """Build subprocess env with DB secrets injected."""
    env = dict(os.environ)
    try:
        c = _db()
        with c.cursor() as cur:
            # Global secrets
            cur.execute("SELECT key, value FROM global_settings WHERE key LIKE 'secret.%%'")
            for row in cur.fetchall():
                ek = row["key"].replace("secret.", "", 1)
                val = row["value"]
                env[ek] = val.strip('"') if isinstance(val, str) else json.dumps(val).strip('"')
            # Agent secrets
            cur.execute("SELECT env_secrets FROM agents WHERE name=%s", (name,))
            row = cur.fetchone()
            if row:
                sec = row["env_secrets"]
                if isinstance(sec, str):
                    sec = json.loads(sec)
                if isinstance(sec, dict):
                    for k, v in sec.items():
                        if k and v and isinstance(v, str):
                            env[k] = v
        c.close()
    except Exception as exc:
        log.warning(f"Secret load for '{name}': {exc}")
    return env


# ── Process Management ────────────────────────────────────────────────────────

def _spawn(name: str):
    """Start an agent subprocess. No message recovery — processed messages stay processed."""
    # CR-187: Check backoff — skip if agent is in exponential backoff period
    backoff_deadline = _spawn_backoff.get(name, 0)
    if time.monotonic() < backoff_deadline:
        remaining = int(backoff_deadline - time.monotonic())
        log.warning(f"[CR-187] '{name}' in spawn backoff ({remaining}s remaining) — skipping")
        return

    # CR-164 simplified: Only one agent at a time — check in-memory state
    # The orchestrator is the only process that spawns agents, so _agents dict is authoritative
    running = [n for n in _agents if _is_running(n)]
    if running:
        log.info(f"[CR-164] Agent '{running[0]}' is running — skipping spawn of '{name}'")
        return

    # Clean stale PID file before spawn (prevents singleton abort)
    Path(f"/tmp/aimos_agent_{name}.pid").unlink(missing_ok=True)

    env = _load_env(name)
    mode = "orchestrator" if _is_enabled() else "manual"
    cmd = [sys.executable, str(_ROOT / "main.py"), "--id", name, "--mode", mode]
    log.info(f"[Orchestrator] Spawning: {' '.join(cmd)} (cwd={_ROOT})")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env, cwd=str(_ROOT),
    )
    _agents[name] = proc
    _spawn_times[name] = time.monotonic()  # CR-187: track spawn time
    _set_status(name, "active", pid=proc.pid)
    log.info(f"[Orchestrator] Spawned '{name}' PID={proc.pid} mode={mode}")


def _is_running(name: str) -> bool:
    """Check if agent process is truly alive. Detects zombies and dead processes."""
    proc = _agents.get(name)
    if proc is None:
        return False
    pid = proc.pid

    # Method 1: Popen.poll() — reaps zombie if possible
    if hasattr(proc, "poll"):
        try:
            proc.poll()  # Force reap attempt
        except Exception:
            pass
        if proc.returncode is not None:
            _agents.pop(name, None)
            _set_status(name, "offline")
            log.info(f"'{name}' PID={pid} exited (rc={proc.returncode})")
            return False

    # Method 2: psutil — check actual process status (catches zombies)
    try:
        p = psutil.Process(pid)
        status = p.status()
        if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            # Zombie — try to reap it
            if hasattr(proc, "wait"):
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
            _agents.pop(name, None)
            _set_status(name, "offline")
            log.warning(f"'{name}' PID={pid} is {status} → offline")
            return False
        # Check if the process is actually an AIMOS agent (not a recycled PID)
        cmdline = " ".join(p.cmdline() or [])
        if "main.py" not in cmdline and name not in cmdline:
            _agents.pop(name, None)
            _set_status(name, "offline")
            log.warning(f"'{name}' PID={pid} is not an AIMOS agent (cmd: {cmdline[:60]}) → offline")
            return False
    except psutil.NoSuchProcess:
        _agents.pop(name, None)
        _set_status(name, "offline")
        log.warning(f"'{name}' PID={pid} vanished → offline")
        return False
    except psutil.AccessDenied:
        pass  # Can't check — assume alive

    return True


def _stop(name: str):
    """Stop a specific agent and clean up PID file."""
    # CR-187: Track spawn failures — if agent died within 10s of spawn, count as failure
    spawn_t = _spawn_times.pop(name, None)
    if spawn_t is not None:
        alive_duration = time.monotonic() - spawn_t
        if alive_duration < 10:
            _spawn_failures[name] = _spawn_failures.get(name, 0) + 1
            fails = _spawn_failures[name]
            log.warning(f"[CR-187] '{name}' died after {alive_duration:.1f}s (failure #{fails})")
            if fails > 3:
                idx = min(fails - 4, len(_BACKOFF_STEPS) - 1)
                backoff_secs = _BACKOFF_STEPS[idx]
                _spawn_backoff[name] = time.monotonic() + backoff_secs
                log.error(f"[CR-187] '{name}' backoff activated: {backoff_secs}s (failures={fails})")
        elif alive_duration > 60:
            # Successful run — reset failure counter
            _spawn_failures.pop(name, None)
            _spawn_backoff.pop(name, None)

    proc = _agents.pop(name, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info(f"Stopped '{name}'")
    # CR-164: GPU lock removed — in-memory _agents check is sufficient
    Path(f"/tmp/aimos_agent_{name}.pid").unlink(missing_ok=True)
    _set_status(name, "offline")


def _stop_all():
    """Stop all tracked agents."""
    for name in list(_agents):
        _stop(name)


# ── Self-Healing ──────────────────────────────────────────────────────────────

def _heal_zombies():
    """Heal stale agents: no PID, stale heartbeat, stuck messages."""
    try:
        c = _db()
        with c.cursor() as cur:
            # 1. Agents marked active but no live process → offline
            cur.execute("SELECT name, updated_at FROM agents WHERE status IN ('active','running','idle','starting')")
            db_active = cur.fetchall()
            for row in db_active:
                name = row["name"]
                # Check if WE track the process OR it has a live PID file
                if _is_running(name):
                    pass  # tracked and alive — check heartbeat below
                else:
                    # Not tracked by us — check PID file (manual-mode agent?)
                    pidfile = Path(f"/tmp/aimos_agent_{name}.pid")
                    if pidfile.exists():
                        try:
                            pid = int(pidfile.read_text().strip())
                            if psutil.pid_exists(pid):
                                continue  # alive but not ours — leave it
                        except (ValueError, OSError):
                            pass
                    # No PID file, no tracked process — only reset if updated_at is stale
                    if row["updated_at"]:
                        from datetime import datetime, timezone
                        age = (datetime.now(timezone.utc) - row["updated_at"].replace(tzinfo=timezone.utc)).total_seconds()
                        if age < 15:
                            continue  # recently active — might still be booting
                    log.warning(f"Zombie heal: '{name}' no live PID + stale heartbeat → offline")
                    cur.execute("UPDATE agents SET status='offline', updated_at=NOW() WHERE name=%s", (name,))
                    continue
                # 2. Heartbeat stale: updated_at not refreshed for >30s while process exists
                #    This catches agents stuck in Ollama connect or DB deadlock
                if row["updated_at"]:
                    from datetime import datetime, timezone
                    age = (datetime.now(timezone.utc) - row["updated_at"].replace(tzinfo=timezone.utc)).total_seconds()
                    if age > 180 and name in _agents:
                        proc = _agents[name]
                        log.error(f"STALE HEARTBEAT: '{name}' last heartbeat {age:.0f}s ago — killing PID={proc.pid}")
                        _stop(name)
                        cur.execute("UPDATE agents SET status='offline', updated_at=NOW() WHERE name=%s", (name,))

            # No message recovery — processed messages stay processed.
            # This prevents re-processing old messages (the 12x reply bug).

        c.commit()
        c.close()
    except Exception as exc:
        log.debug(f"Heal check failed: {exc}")


_COOLDOWN = 300  # 5 minutes lockout after rate-limit trigger
_cooldown_until: dict[str, float] = {}  # agent_name → monotonic deadline


def _is_cooled_down(name: str) -> bool:
    """Check if agent is in rate-limit cooldown."""
    deadline = _cooldown_until.get(name, 0)
    if time.monotonic() < deadline:
        return True
    _cooldown_until.pop(name, None)
    return False


def _check_rate_limit():
    """If an agent sent >10 replies in 60s, force-stop + 5min cooldown."""
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute("""
                SELECT agent_name, COUNT(*) as cnt
                FROM aimos_chat_histories
                WHERE role='assistant' AND created_at > NOW() - INTERVAL '60 seconds'
                GROUP BY agent_name
                HAVING COUNT(*) > 10
            """)
            for row in cur.fetchall():
                name = row["agent_name"]
                cnt = row["cnt"]
                log.error(f"RATE LIMIT: '{name}' sent {cnt} replies in 60s — force-stopping + {_COOLDOWN}s cooldown")
                _stop(name)
                _cooldown_until[name] = time.monotonic() + _COOLDOWN
        c.close()
    except Exception:
        pass


def _daily_wakeup():
    """CR-144: Trigger every active agent once per 24h for calendar/email/task review.

    Injects a 'scheduled_job' message that prompts the agent to check its calendar,
    pending emails, and workspace todo list. Skips agents that already have pending messages.
    """
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute(
                "SELECT name FROM agents WHERE COALESCE((config->>'active')::boolean, true) = true"
            )
            agents = [r["name"] for r in cur.fetchall()]

            for name in agents:
                # Skip agents with disable_auto_jobs — no point waking them
                cur.execute(
                    "SELECT config FROM agents WHERE name=%s", (name,)
                )
                row = cur.fetchone()
                if row and row["config"]:
                    import json as _json
                    try:
                        cfg = _json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
                        if cfg.get("disable_auto_jobs"):
                            continue
                    except Exception:
                        pass

                # Skip if agent already has unprocessed messages
                cur.execute(
                    "SELECT 1 FROM pending_messages WHERE agent_name=%s AND processed=FALSE LIMIT 1",
                    (name,),
                )
                if cur.fetchone():
                    continue

                # Skip if already triggered today
                cur.execute(
                    "SELECT 1 FROM pending_messages WHERE agent_name=%s AND kind='scheduled_job' "
                    "AND content LIKE '%%Daily Review%%' AND created_at > NOW() - INTERVAL '20 hours'",
                    (name,),
                )
                if cur.fetchone():
                    continue

                cur.execute(
                    "INSERT INTO pending_messages (agent_name, sender_id, content, kind) "
                    "VALUES (%s, 0, %s, 'scheduled_job')",
                    (name, "[Scheduled Task] [Daily Review] Check your calendar for overdue and "
                     "upcoming events. Check emails if available. Review your workspace todo.txt "
                     "for open tasks. If anything needs attention, act on it or notify the user."),
                )
                log.info(f"[DailyWakeup] Triggered daily review for '{name}'")

        c.commit()
        c.close()
    except Exception as exc:
        log.debug(f"DailyWakeup error: {exc}")


def _fire_due_jobs():
    """CR-063: Fire scheduled jobs whose time has arrived.

    Converts due agent_jobs into pending_messages with kind='scheduled_job'.
    Anti-recursion: jobs sourced from 'agent' cannot trigger if the agent
    already has a pending scheduled_job (prevents ping-pong loops).
    """
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, agent_name, task_prompt, source FROM agent_jobs "
                "WHERE status='pending' AND scheduled_time <= NOW() "
                "ORDER BY scheduled_time ASC LIMIT 10"
            )
            due_jobs = cur.fetchall()
            for job in due_jobs:
                jid = job["id"]
                name = job["agent_name"]
                prompt = job["task_prompt"]

                # Inactive agents don't get cronjobs — mark as blocked
                cur.execute("SELECT config FROM agents WHERE name=%s", (name,))
                agent_row = cur.fetchone()
                if agent_row:
                    acfg = agent_row["config"]
                    if isinstance(acfg, str):
                        acfg = json.loads(acfg)
                    if acfg.get("active", True) is False:
                        cur.execute("UPDATE agent_jobs SET status='blocked', fired_at=NOW() WHERE id=%s", (jid,))
                        log.debug(f"Job #{jid} blocked — '{name}' is inactive")
                        continue
                source = job.get("source", "agent")

                # Anti-recursion: skip if agent already has a pending scheduled_job
                cur.execute(
                    "SELECT 1 FROM pending_messages "
                    "WHERE agent_name=%s AND kind='scheduled_job' AND processed=FALSE LIMIT 1",
                    (name,),
                )
                if cur.fetchone():
                    log.debug(f"Job #{jid} skipped — '{name}' already has pending scheduled_job")
                    continue

                # Anti-spam: max 5 scheduled_jobs per agent per hour (hard system limit)
                cur.execute(
                    "SELECT COUNT(*) FROM pending_messages "
                    "WHERE agent_name=%s AND kind='scheduled_job' "
                    "AND created_at > NOW() - INTERVAL '1 hour'",
                    (name,),
                )
                hourly_count = cur.fetchone()[0]
                if hourly_count >= 5:
                    log.warning(f"Job #{jid} BLOCKED — '{name}' hit hourly limit ({hourly_count}/5)")
                    cur.execute("UPDATE agent_jobs SET status='blocked', fired_at=NOW() WHERE id=%s", (jid,))
                    continue

                # Fire: insert as pending_message
                cur.execute(
                    "INSERT INTO pending_messages (agent_name, sender_id, content, kind) "
                    "VALUES (%s, 0, %s, 'scheduled_job')",
                    (name, f"[Scheduled Task] {prompt}"),
                )
                cur.execute(
                    "UPDATE agent_jobs SET status='fired', fired_at=NOW() WHERE id=%s",
                    (jid,),
                )
                log.info(f"[JobRunner] Fired job #{jid} for '{name}': {prompt[:60]}")
        c.commit()
        c.close()
    except Exception as exc:
        log.debug(f"JobRunner error: {exc}")


# ── CR-090: Dreaming ──────────────────────────────────────────────────────

_DREAM_HISTORY_THRESHOLD_DEFAULT = 25  # Dream when agent has this many messages

def _dream_check():
    """Check if any active agent needs dreaming based on context pressure.

    Trigger: agent has >25 messages in chat history AND is not currently running.
    This means dreaming happens when the context is getting full, not on a timer.
    An agent that has nothing to process doesn't dream.
    Each agent dreams at most once per batch; reset when new messages arrive.
    """
    try:
        c = _db()
        with c.cursor() as cur:
            # Get message count per agent (= context pressure indicator)
            cur.execute("""
                SELECT agent_name, COUNT(*) AS msg_count
                FROM aimos_chat_histories
                GROUP BY agent_name
            """)
            count_rows = cur.fetchall()

            # Only active-team agents dream
            cur.execute("SELECT name, config FROM agents")
            all_agents = set()
            for r in cur.fetchall():
                cfg = r["config"]
                if isinstance(cfg, str):
                    import json as _json
                    cfg = _json.loads(cfg)
                if cfg.get("active", True) is not False:
                    all_agents.add(r["name"])
        c.close()
    except Exception as exc:
        log.debug(f"[Dream] DB check failed: {exc}")
        return

    msg_counts = {row["agent_name"]: row["msg_count"] for row in count_rows}

    for name in all_agents:
        msg_count = msg_counts.get(name, 0)

        # Only dream if there's enough history to process
        # Dynamic threshold: agents with long system prompts dream earlier
        # because their context fills up faster
        threshold = _DREAM_HISTORY_THRESHOLD_DEFAULT
        try:
            cfg = None
            for r in count_rows:
                pass  # we need the config — fetch it
            c2 = _db()
            with c2.cursor() as cur2:
                cur2.execute("SELECT config FROM agents WHERE name=%s", (name,))
                arow = cur2.fetchone()
                if arow:
                    acfg = arow["config"]
                    if isinstance(acfg, str):
                        acfg = json.loads(acfg)
                    sp_len = len(acfg.get("system_prompt", ""))
                    if sp_len > 8000:
                        threshold = 12  # Very long prompt → dream early
                    elif sp_len > 5000:
                        threshold = 18
            c2.close()
        except Exception:
            pass

        if msg_count < threshold:
            _dreamed_this_idle.discard(name)
            continue

        # Skip if already dreamed since last threshold crossing or currently running
        if name in _dreamed_this_idle:
            continue
        if _is_running(name):
            continue

        # CR-201: Check DB for recent dream — survives restarts
        try:
            c3 = _db()
            with c3.cursor() as cur3:
                cur3.execute("SELECT value FROM global_settings WHERE key=%s", (f"last_dream.{name}",))
                dream_row = cur3.fetchone()
            c3.close()
            if dream_row:
                dream_data = json.loads(dream_row["value"]) if isinstance(dream_row["value"], str) else dream_row["value"]
                last_dream_count = dream_data.get("msg_count", 0)
                if msg_count <= last_dream_count:
                    # No new messages since last dream — skip
                    _dreamed_this_idle.add(name)
                    continue
        except Exception:
            pass

        # Find memory.db path
        from core.skills.base import BaseSkill
        db_path = BaseSkill.memory_db_path(name)
        if not db_path.exists():
            continue

        # CR-178: Dreaming runs while agent is offline → before next _compress_history()
        log.info(f"[Dream] '{name}' has {msg_count} messages (threshold={threshold}) — starting dream (before compression)")
        try:
            # Set status to 'dreaming' so Dashboard shows who is using the LLM
            _set_status(name, "dreaming")
            from core.dreaming import dream
            summary = dream(name, db_path)
            _set_status(name, "offline")
            _dreamed_this_idle.add(name)
            extracted = summary.get('extracted_from_history', 0)
            log.info(f"[Dream] '{name}' done: {summary.get('duration_ms', 0)}ms, "
                     f"extracted={extracted}, "
                     f"report={'yes' if summary.get('weekly_report') else 'no'}")

            # CR-201: Compress history after dreaming to prevent re-triggering
            # Keep only (threshold - 10) messages so we stay well below the trigger point
            keep_count = max(threshold - 10, 5)
            try:
                c = _db()
                with c.cursor() as cur:
                    cur.execute(
                        "WITH excess AS ("
                        "  SELECT id FROM aimos_chat_histories "
                        "  WHERE agent_name=%s "
                        "  AND id NOT IN ("
                        "    SELECT id FROM aimos_chat_histories WHERE agent_name=%s "
                        "    ORDER BY id DESC LIMIT %s"
                        "  )"
                        ") "
                        "DELETE FROM aimos_chat_histories WHERE id IN (SELECT id FROM excess)",
                        (name, name, keep_count),
                    )
                    deleted = cur.rowcount
                c.commit()
                c.close()
                if deleted:
                    log.info(f"[CR-201] '{name}' post-dream compression: {deleted} messages deleted, {keep_count} kept")
            except Exception as exc2:
                log.warning(f"[CR-201] Post-dream compression failed for '{name}': {exc2}")

            # CR-201: Persist dream timestamp so we don't re-dream after restart
            try:
                dream_info = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         "extracted": extracted, "msg_count": msg_count})
                c = _db()
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO global_settings (key, value, updated_at) "
                        "VALUES (%s, %s, NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                        (f"last_dream.{name}", dream_info, dream_info),
                    )
                c.commit()
                c.close()
            except Exception:
                pass

        except Exception as exc:
            _set_status(name, "offline")
            log.error(f"[Dream] '{name}' dream failed: {exc}")
            _dreamed_this_idle.add(name)  # don't retry on failure


# ── Main Loop ─────────────────────────────────────────────────────────────────

async def run(once: bool = False):
    log.info("=" * 50)
    log.info("  AIMOS Orchestrator v4.1.0 — Daemon Mode")
    log.info(f"  Poll interval: {_POLL_INTERVAL}s")
    log.info("=" * 50)

    # Kill old agent processes and duplicate orchestrators
    my_pid = os.getpid()
    my_ppid = os.getppid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if pid == my_pid or pid == my_ppid:
                continue  # don't kill ourselves or our parent shell
            pname = proc.info.get("name", "")
            if pname != "python3" and pname != "python":
                continue  # only kill python processes
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "main.py" in cmdline and "--id" in cmdline:
                log.info(f"Startup: killing orphan agent PID={pid}")
                proc.kill()
            elif "core.orchestrator" in cmdline:
                log.info(f"Startup: killing duplicate orchestrator PID={pid}")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Force-cleanup: verify all PIDs in DB actually exist
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute("SELECT name, pid, status FROM agents WHERE pid IS NOT NULL")
            for row in cur.fetchall():
                if not psutil.pid_exists(row["pid"]):
                    log.warning(f"Startup cleanup: '{row['name']}' PID={row['pid']} dead → offline")
                    cur.execute("UPDATE agents SET pid=NULL, status='offline', updated_at=NOW() WHERE name=%s", (row["name"],))
            # Don't clear requested_state — the loop handles it on first cycle
        c.commit()
        c.close()
        log.info("Startup PID verification complete")
    except Exception as exc:
        log.warning(f"Startup cleanup failed: {exc}")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    cycle = 0
    try:
        while not shutdown.is_set():
            cycle += 1
            try:
                enabled = _is_enabled()
            except Exception:
                enabled = False

            # ALWAYS handle requested_state (works in both auto and manual mode)
            _c = None
            try:
                _c = psycopg2.connect(host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
                                      user=Config.PG_USER, password=Config.PG_PASSWORD, connect_timeout=3)
                _c.autocommit = True
                _cur = _c.cursor()
                _cur.execute("SELECT name, requested_state FROM agents WHERE requested_state IS NOT NULL")
                _reqs = _cur.fetchall()
                for _r in _reqs:
                    _name, _state = _r[0], _r[1]
                    _cur.execute("UPDATE agents SET requested_state=NULL WHERE name=%s", (_name,))
                    if _state == "active" and not _is_running(_name):
                        _cooldown_until.pop(_name, None)
                        log.info(f"[cycle {cycle}] Starting '{_name}'")
                        _set_status(_name, "starting")
                        _spawn(_name)
                        break
                    elif _state == "offline" and _is_running(_name):
                        log.info(f"[cycle {cycle}] Stopping '{_name}'")
                        _stop(_name)
            except Exception as _e:
                log.error(f"[cycle {cycle}] requested_state error: {_e}")
            finally:
                if _c:
                    try:
                        _c.close()
                    except Exception:
                        pass

            # Log state every 15 cycles
            if cycle % 15 == 0:
                active_names = [n for n in list(_agents) if _is_running(n)]
                log.info(f"[cycle {cycle}] mode={'AUTO' if enabled else 'MANUAL'} active={active_names}")

            # Manual mode: only handle requested_state (already done above).
            # Don't stop running agents — they stay until user clicks Stop or watchdog fires.
            if not enabled:
                await asyncio.sleep(_POLL_INTERVAL)
                if once:
                    break
                continue

            if cycle == 1:
                log.info("Orchestrator ACTIVE — polling pending_messages")

            # Self-heal every 5 cycles (~10s)
            if cycle % 5 == 0:
                _heal_zombies()

            # Job Runner (CR-063): fire due scheduled jobs every 10 cycles (~20s)
            if cycle % 10 == 0:
                _fire_due_jobs()

            # CR-090 + CR-178: Dream check every 30 cycles (~60s)
            # Sequenced Dreaming: dreams run BEFORE history compression.
            # Dreams execute while the agent is offline (idle), extracting
            # memories from the full chat history. When the agent next starts
            # via _spawn(), agent_base._compress_history() truncates old
            # messages. This order ensures dreaming sees full history before
            # compression removes it.
            if cycle % 30 == 0:
                _dream_check()

            # CR-215d: Ollama health check every 60 cycles (~120s)
            if cycle % 60 == 0:
                try:
                    import urllib.request
                    req = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5)
                    req.close()
                except Exception as _ohc_exc:
                    log.error(f"[CR-215d] Ollama health check FAILED: {_ohc_exc}")
                    # Try to restart via docker
                    try:
                        import subprocess
                        subprocess.run(["docker", "restart", "aimos-ollama"],
                                       capture_output=True, timeout=30)
                        log.info("[CR-215d] Ollama container restart triggered")
                    except Exception as _restart_exc:
                        log.error(f"[CR-215d] Ollama restart failed: {_restart_exc}")

            # CR-144: Daily wake-up — trigger every active agent once per 24h
            # so they check calendar, email, pending tasks. Runs at cycle 100 (~200s after start)
            # then every 43200 cycles (~24h at 2s interval)
            if cycle == 100 or (cycle > 0 and cycle % 43200 == 0):
                _daily_wakeup()

            # CR-120 + CR-205: Cleanup dead processes — but only once per dead agent
            any_exited = False
            for name in list(_agents):
                if not _is_running(name):
                    any_exited = True
                    log.info(f"[CR-120] '{name}' exited — cleaning up from _agents")
                    # CR-205: Pop from _agents FIRST to prevent repeated _stop() calls
                    _agents.pop(name, None)
                    _set_status(name, "offline")
                    # Reset spawn failure if it ran > 60s (normal exit, not crash)
                    spawn_t = _spawn_times.pop(name, None)
                    if spawn_t and (time.monotonic() - spawn_t) > 60:
                        _spawn_failures.pop(name, None)
                        _spawn_backoff.pop(name, None)
            # Double-check: verify DB status matches reality
            # If DB says active but _agents has no entry, reset to offline
            if cycle % 5 == 0:
                try:
                    c = _db()
                    with c.cursor() as cur:
                        cur.execute(
                            "SELECT name, pid FROM agents WHERE status IN ('active','running','starting') "
                            "AND name NOT IN %s",
                            (tuple(_agents.keys()) or ('__none__',),),
                        )
                        for row in cur.fetchall():
                            db_pid = row.get("pid")
                            if not db_pid or not psutil.pid_exists(db_pid):
                                _set_status(row["name"], "offline")
                                log.warning(f"[CR-120] '{row['name']}' stale in DB (PID={db_pid}) → offline")
                    c.close()
                except Exception:
                    pass

            # CR-137: Process-level watchdog — kill agents that hang (alive but unresponsive)
            # Checks every 30 cycles (~60s): if agent PID has 0% CPU for 2 consecutive checks
            # AND its DB updated_at is stale (>5 min), it's hanging → kill it
            if cycle % 30 == 0:
                for name in list(_agents):
                    proc = _agents.get(name)
                    if proc is None:
                        continue
                    try:
                        p = psutil.Process(proc.pid)
                        cpu = p.cpu_percent(interval=0.5)
                        # Check DB heartbeat
                        c = _db()
                        with c.cursor() as cur:
                            cur.execute(
                                "SELECT updated_at FROM agents WHERE name=%s", (name,)
                            )
                            row = cur.fetchone()
                        c.close()
                        if row and row["updated_at"]:
                            from datetime import datetime, timezone
                            age_sec = (datetime.now(timezone.utc) - row["updated_at"]).total_seconds()
                            # Hanging: 0% CPU + no DB heartbeat for 5 minutes
                            if cpu < 0.1 and age_sec > 300:
                                log.warning(
                                    f"[CR-137] '{name}' PID={proc.pid} appears hung "
                                    f"(CPU={cpu}%, last heartbeat {age_sec:.0f}s ago) → killing"
                                )
                                try:
                                    proc.kill()
                                    proc.wait(timeout=3)
                                except Exception:
                                    pass
                                _agents.pop(name, None)
                                _set_status(name, "offline")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    except Exception as exc:
                        log.debug(f"[CR-137] Watchdog check failed for '{name}': {exc}")

            # VRAM settle after agent exit
            if any_exited:
                await asyncio.sleep(2)

            # Scan for agents with pending work
            pending_agents = _get_agents_with_pending()
            if pending_agents:
                log.info(f"[cycle {cycle}] Queue: {', '.join(pending_agents)}")

            # VRAM Guard: enforce max ONE agent running in auto-mode
            running_agents = [n for n in list(_agents) if _is_running(n)]
            if len(running_agents) > 1:
                # Kill all except the first — should never happen, but self-heal
                for extra in running_agents[1:]:
                    log.warning(f"[cycle {cycle}] VRAM Guard: killing extra agent '{extra}'")
                    _stop(extra)
                await asyncio.sleep(2)
            if running_agents:
                # Smart-Yield (CR-072): if the running agent is idle AND other
                # agents have pending messages, stop it to free the VRAM slot.
                if pending_agents and len(running_agents) == 1:
                    occupant = running_agents[0]
                    waiting = [a for a in pending_agents if a != occupant]
                    if waiting:
                        try:
                            c = _db()
                            with c.cursor() as cur:
                                cur.execute("SELECT status FROM agents WHERE name=%s", (occupant,))
                                row = cur.fetchone()
                            c.close()
                            occ_status = row["status"] if row else "unknown"
                        except Exception:
                            occ_status = "unknown"
                        # Also check heartbeat — if stale >60s, agent is de facto idle
                        heartbeat_stale = False
                        try:
                            c2 = _db()
                            with c2.cursor() as cur2:
                                cur2.execute(
                                    "SELECT EXTRACT(EPOCH FROM (NOW() - updated_at)) AS age "
                                    "FROM agents WHERE name=%s", (occupant,))
                                hb = cur2.fetchone()
                                if hb and hb["age"] > 60:
                                    heartbeat_stale = True
                            c2.close()
                        except Exception:
                            pass

                        # CR-213b: Never yield if the occupant still has pending messages
                        occupant_has_pending = False
                        try:
                            c3 = _db()
                            with c3.cursor() as cur3:
                                cur3.execute(
                                    "SELECT 1 FROM pending_messages "
                                    "WHERE agent_name=%s AND processed=FALSE LIMIT 1",
                                    (occupant,))
                                occupant_has_pending = cur3.fetchone() is not None
                            c3.close()
                        except Exception:
                            pass

                        if occupant_has_pending:
                            pass  # keep running — agent still has work to do
                        elif occ_status == "idle" or heartbeat_stale:
                            reason = "idle" if occ_status == "idle" else f"stale heartbeat ({int(hb['age'] if hb else 0)}s)"
                            log.info(
                                f"[cycle {cycle}] Smart-Yield: '{occupant}' is {reason}, "
                                f"yielding VRAM for {waiting}"
                            )
                            _stop(occupant)
                            await asyncio.sleep(2)
                            continue  # next cycle will spawn the waiting agent
                await asyncio.sleep(_POLL_INTERVAL)
                continue

            # Spawn the FIRST pending agent (one at a time)
            for name in pending_agents:
                if _is_running(name):
                    continue

                # Cooldown check: rate-limited agents are locked out for 5 minutes
                if _is_cooled_down(name):
                    remaining = int(_cooldown_until.get(name, 0) - time.monotonic())
                    if cycle % 15 == 0:  # log periodically, not every cycle
                        log.warning(f"[Orchestrator] '{name}' in cooldown ({remaining}s remaining)")
                    continue

                # Double-check DB status — if not 'active', agent is definitely not working
                try:
                    c = _db()
                    with c.cursor() as cur:
                        cur.execute("SELECT status FROM agents WHERE name=%s", (name,))
                        row = cur.fetchone()
                    c.close()
                    db_status = row["status"] if row else "offline"
                except Exception:
                    db_status = "unknown"

                if db_status == "active":
                    # DB says active but we have no PID — stale, fix it
                    _set_status(name, "offline")
                    log.warning(f"[Orchestrator] '{name}' was active in DB but no PID — reset")

                _set_status(name, "starting")
                log.info(f"[Orchestrator] Starting '{name}' (auto-mode)")
                _spawn(name)
                await asyncio.sleep(5)
                break  # VRAM Guard: only one agent per cycle

            # (requested_state handled at top of loop — always runs)

            # Rate-limit: if an agent generated >10 replies in 60s, force-stop (loop detection)
            if cycle % 10 == 0:
                _check_rate_limit()

            # CR-185: RSS monitoring — restart agents exceeding 2 GB RSS
            if cycle % 10 == 0:
                for _rss_name, _rss_proc in list(_agents.items()):
                    try:
                        _rss_p = psutil.Process(_rss_proc.pid)
                        _rss_mb = _rss_p.memory_info().rss / 1024 / 1024
                        if _rss_mb > 2048:
                            log.warning(f"[CR-185] Agent '{_rss_name}' RSS={_rss_mb:.0f}MB > 2GB — restarting")
                            _stop(_rss_name)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            # Log active agents periodically + cleanup stale entries
            if cycle % 15 == 0 and _agents:
                active = []
                stale = []
                for n, p in list(_agents.items()):
                    if hasattr(p, 'poll') and p.poll() is not None:
                        stale.append(n)
                    elif not psutil.pid_exists(p.pid):
                        stale.append(n)
                    else:
                        active.append(f"{n}(PID={p.pid})")
                for n in stale:
                    _agents.pop(n, None)
                    _set_status(n, "offline")
                    log.warning(f"[CR-120] Cleaned stale agent '{n}' from _agents dict")
                if active:
                    log.info(f"[cycle {cycle}] mode=AUTO active={active}")

            sleep_time = _POLL_INTERVAL
            await asyncio.sleep(sleep_time)
            if once:
                break

    except asyncio.CancelledError:
        pass
    finally:
        _stop_all()
        log.info("Orchestrator stopped.")


if __name__ == "__main__":
    once = "--once" in sys.argv
    asyncio.run(run(once=once))
