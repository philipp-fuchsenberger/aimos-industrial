"""
AIMOS Dashboard Routes — v4.1.0
=================================
All FastAPI endpoints extracted from app.py for PRQ-01 compliance (<500 lines).
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import Config, SecretFilter
from core.db_pool import db_connection
from core.dashboard.app import (
    _is_orchestrator_on, _fetch_agents, _fetch_pending_count,
    _fetch_pending_detail, _fetch_global_settings, _fetch_last_activity,
    get_metrics, _tail_log, templates, _ROOT_DIR,
)
import core.dashboard.app as _app_module  # for watchdog flag

log = logging.getLogger("AIMOS.dashboard")
router = APIRouter()

# No agent processes tracked here — orchestrator is the boss.
# Only infrastructure (listener, orchestrator) tracked below.


# Zombie cleanup is handled by the orchestrator, not the dashboard.



def _fetch_authorized_users() -> dict[str, list[int]]:
    """Get allowed_chat_ids per agent from agents.config."""
    result: dict[str, list[int]] = {}
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, config FROM public.agents ORDER BY name")
                rows = cur.fetchall()
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            if isinstance(cfg, dict):
                ids = cfg.get("allowed_chat_ids", [])
                if ids:
                    result[r["name"]] = [int(i) for i in ids]
    except Exception:
        pass
    return result


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    agents = _fetch_agents()
    metrics = get_metrics()
    pending = _fetch_pending_count()
    settings = _fetch_global_settings()
    orchestrator_on = _is_orchestrator_on()
    auth_users = _fetch_authorized_users()
    for s in settings:
        if isinstance(s.get("value"), dict):
            s["value"] = SecretFilter.redact(s["value"])

    # VRAM occupant for GPU panel
    vram_occupant = next((a["name"] for a in agents if a.get("status") in ("active", "running")), None)
    pending_detail = _fetch_pending_detail()
    last_activity = _fetch_last_activity()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "agents": agents, "metrics": metrics,
        "pending": pending, "pending_detail": pending_detail,
        "settings": settings, "auth_users": auth_users,
        "last_activity": last_activity,
        "config": Config, "orchestrator_on": orchestrator_on,
        "vram_occupant": vram_occupant,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


import asyncio as _asyncio
_backup_lock = _asyncio.Lock()  # CR-193: async lock replaces bare flag
_backup_running = False

@router.post("/api/backup/run", response_class=JSONResponse)
async def api_backup_run():
    """Trigger a full backup. Runs backup_full.sh synchronously."""
    global _backup_running
    if _backup_lock.locked():
        return JSONResponse({"status": "error", "error": "Backup already running"}, status_code=409)
    async with _backup_lock:
        _backup_running = True
        import subprocess
        script = Path(__file__).parent.parent.parent / "scripts" / "backup_full.sh"
        if not script.exists():
            _backup_running = False
            return JSONResponse({"status": "error", "error": "backup_full.sh not found"}, status_code=404)
        try:
            result = subprocess.run(
                ["bash", str(script)], capture_output=True, text=True, timeout=300, cwd=str(script.parent.parent)
            )
            # Check stdout+stderr for actual result
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0 or "Archive created" in output:
                # Find latest backup file
                backup_dir = script.parent.parent / "backups" / "daily"
                latest = sorted(backup_dir.glob("*.tar.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
                if latest:
                    size_mb = latest[0].stat().st_size / 1024 / 1024
                    return {"status": "ok", "file": latest[0].name, "size": f"{size_mb:.1f} MB"}
                return {"status": "ok", "file": "completed", "size": "unknown"}
            else:
                log.error(f"Backup failed (rc={result.returncode}): stdout={result.stdout[:300]} stderr={result.stderr[:300]}")
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error (check logs/backup.log)"
                return JSONResponse({"status": "error", "error": error_msg[:200]}, status_code=500)
        except subprocess.TimeoutExpired:
            return JSONResponse({"status": "error", "error": "Backup timed out (>5 min)"}, status_code=500)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
        finally:
            _backup_running = False


@router.get("/api/backup/status", response_class=JSONResponse)
async def api_backup_status():
    """Get the status of the last backup."""
    backup_dir = Path(__file__).parent.parent.parent / "backups" / "daily"
    if not backup_dir.exists():
        return {"last_backup": None}
    backups = sorted(backup_dir.glob("*.tar.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not backups:
        return {"last_backup": None}
    latest = backups[0]
    size_mb = latest.stat().st_size / 1024 / 1024
    from datetime import datetime as _dt
    mtime = _dt.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {"last_backup": mtime, "file": latest.name, "size": f"{size_mb:.1f} MB", "count": len(backups)}


@router.get("/api/ollama/health", response_class=JSONResponse)
async def api_ollama_health():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://127.0.0.1:11434/api/tags")
            return {"ok": r.status_code == 200}
    except Exception:
        return {"ok": False}


@router.get("/api/network/health", response_class=JSONResponse)
async def api_network_health():
    """CR-181: Network status check — DNS, internet, Telegram API."""
    checks = {}
    # Internet connectivity (ping 8.8.8.8)
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "8.8.8.8"],
            capture_output=True, timeout=5,
        )
        checks["internet"] = r.returncode == 0
    except Exception:
        checks["internet"] = False
    # Telegram API reachability
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get("https://api.telegram.org")
            checks["telegram_api"] = r.status_code == 200
    except Exception:
        checks["telegram_api"] = False
    return checks


@router.get("/api/metrics", response_class=JSONResponse)
async def api_metrics():
    data = get_metrics()
    # Add Ollama model info
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:11434/api/ps", timeout=3)
        models = r.json().get("models", [])
        if models:
            m = models[0]
            data["ollama"] = {
                "model": m.get("name", "unknown"),
                "vram_mb": int(m.get("size", 0) / 1e6),
            }
    except Exception:
        pass
    return data


@router.get("/api/agents", response_class=JSONResponse)
async def api_agents():
    agents = _fetch_agents()
    for a in agents:
        for k in ("created_at", "updated_at"):
            if isinstance(a.get(k), datetime):
                a[k] = a[k].isoformat()
        if isinstance(a.get("config"), dict):
            a["config"] = SecretFilter.redact(a["config"])
    return agents


@router.get("/api/logs/{agent_id}", response_class=JSONResponse)
async def api_logs(agent_id: str, lines: int = 50):
    return {"agent": agent_id, "lines": _tail_log(agent_id.lower(), lines)}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/api/settings", response_class=JSONResponse)
async def api_settings():
    settings = _fetch_global_settings()
    for s in settings:
        if isinstance(s.get("value"), dict):
            s["value"] = SecretFilter.redact(s["value"])
        for k in ("updated_at",):
            if isinstance(s.get(k), datetime):
                s[k] = s[k].isoformat()
    return settings


@router.get("/api/alerts", response_class=JSONResponse)
async def api_alerts():
    """Return active system alerts (external API failures, etc.)."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, value, updated_at FROM global_settings "
                    "WHERE key LIKE 'alert.%' ORDER BY updated_at DESC"
                )
                rows = cur.fetchall()
        alerts = []
        for r in rows:
            val = r["value"]
            if isinstance(val, str):
                import json as _json
                try:
                    val = _json.loads(val)
                except Exception:
                    pass
            alerts.append({
                "type": r["key"].replace("alert.", ""),
                "data": val,
                "time": r["updated_at"].isoformat() if r.get("updated_at") else None,
            })
        return alerts
    except Exception:
        return []


@router.delete("/api/alerts/{alert_type}", response_class=JSONResponse)
async def api_dismiss_alert(alert_type: str):
    """Dismiss/acknowledge an alert."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM global_settings WHERE key=%s", (f"alert.{alert_type}",))
            conn.commit()
        return {"status": "dismissed", "type": alert_type}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/settings/{key}", response_class=JSONResponse)
async def api_set_setting(key: str, request: Request):
    body = await request.json()
    value = body.get("value")
    if value is None:
        return JSONResponse({"error": "missing 'value'"}, status_code=400)
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO global_settings (key, value, updated_at) "
                    "VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (key, json.dumps(value), json.dumps(value)),
                )
            conn.commit()
        return {"status": "saved", "key": key}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Wizard ────────────────────────────────────────────────────────────────────

@router.get("/wizard", response_class=HTMLResponse)
@router.get("/wizard/{agent_name}", response_class=HTMLResponse)
async def wizard_page(request: Request, agent_name: str = ""):
    agent_data = None
    if agent_name:
        try:
            with db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name, status, config, env_secrets FROM agents WHERE name=%s", (agent_name,))
                    row = cur.fetchone()
                if row:
                    agent_data = dict(row)
        except Exception:
            pass
    # Build dynamic skill + connector metadata for wizard UI
    from core.skills import SKILL_REGISTRY

    # Connectors (communication channels — not in SKILL_REGISTRY)
    _CONNECTOR_SKILLS = {"email", "voice_io", "mail_monitor"}

    skill_meta = [
        {"name": "telegram", "label": "Telegram Bot", "category": "connector",
         "config_fields": [
             {"key": "TELEGRAM_BOT_TOKEN", "label": "Bot Token", "type": "password",
              "placeholder": "", "hint": "Get one from @BotFather", "secret": True},
         ]},
        {"name": "webhook", "label": "Webhook (HTTP POST)", "category": "connector",
         "config_fields": [
             {"key": "WEBHOOK_TOKEN", "label": "Webhook Secret Token", "type": "password",
              "placeholder": "", "hint": "External systems send this in X-Webhook-Token header", "secret": True},
         ]},
        {"name": "rest_api", "label": "REST API", "category": "connector",
         "config_fields": []},
        {"name": "dashboard", "label": "Dashboard Chat", "category": "connector",
         "config_fields": []},
    ]

    # Skill category mapping
    _SKILL_CATEGORIES = {
        "structural": "engineering", "file_ops": "engineering", "brave_search": "engineering",
        "email": "connector", "voice_io": "connector", "mail_monitor": "connector",
        "shared_storage": "storage", "remote_storage": "storage",
        "scheduler": "planning", "calendar": "planning", "contacts": "planning",
        "persistence": "planning", "project_management": "planning",
        "de_calendar_awareness": "planning", "tr_calendar_awareness": "planning",
        "hybrid_reasoning": "intelligence",
        "football_observer": "intelligence",
        "eta_firebird": "accounting", "eta_mssql": "accounting", "web_automation": "accounting",
    }

    for sname, scls in SKILL_REGISTRY.items():
        cat = _SKILL_CATEGORIES.get(sname, "skill")
        if cat == "connector":
            # Already handled above or add to connectors
            if sname not in [s["name"] for s in skill_meta]:
                skill_meta.append({
                    "name": sname,
                    "label": getattr(scls, "display_name", sname),
                    "category": "connector",
                    "config_fields": scls.config_fields() if hasattr(scls, "config_fields") else [],
                })
        else:
            skill_meta.append({
                "name": sname,
                "label": getattr(scls, "display_name", sname),
                "category": cat,
                "config_fields": scls.config_fields() if hasattr(scls, "config_fields") else [],
            })
    return templates.TemplateResponse("wizard.html", {
        "request": request, "agent_data": agent_data, "config": Config,
        "skill_meta": skill_meta,
    })


@router.post("/api/wizard", response_class=JSONResponse)
async def api_wizard_save(request: Request):
    """Save agent config. Only allowed when agent is confirmed offline by orchestrator."""
    data = await request.json()
    name = (data.get("name") or "").strip().lower()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    # Parse agent-editable secrets list
    raw_editable = data.get("agent_editable_secrets", "")
    if isinstance(raw_editable, str):
        editable_list = [k.strip().upper() for k in raw_editable.split(",") if k.strip()]
    elif isinstance(raw_editable, list):
        editable_list = [k.strip().upper() for k in raw_editable if k.strip()]
    else:
        editable_list = []

    config = {
        "active": data.get("active", True),
        "agent_type": data.get("agent_type", "business"),
        "display_name": data.get("display_name", name),
        "system_prompt": data.get("system_prompt", ""),
        "skills": data.get("skills", data.get("modules", [])),
        "execution_strategy": data.get("execution_strategy", "sequential"),
        "vram_profile": data.get("vram_profile", "max_context"),
        "num_ctx": int(data.get("num_ctx", 24576)),
        "cognitive_balance": int(data.get("cognitive_balance", 0)),
        "max_ring": int(data.get("max_ring", 2)),
        "voice_mode": data.get("voice_mode", "off"),
        "whisper_language": data.get("whisper_language", "de"),
        "agent_editable_secrets": editable_list,
        "character": {
            "description": data.get("char_description", ""),
        },
    }

    # Dynamic skill config: extract fields from skill_config dict
    skill_config = data.get("skill_config", {})
    agent_secrets = {}
    from core.skills import SKILL_REGISTRY
    for _sname, _scls in SKILL_REGISTRY.items():
        for field in _scls.config_fields():
            key = field["key"]
            val = skill_config.get(key, "")
            if not isinstance(val, str):
                val = str(val) if val else ""
            val = val.strip()
            if not val:
                continue
            if field.get("secret", True):
                agent_secrets[key] = val
            else:
                # Try to parse as JSON (for structured fields like web_flows)
                try:
                    parsed = json.loads(val)
                    config[key.lower()] = parsed
                except (json.JSONDecodeError, TypeError):
                    config[key.lower()] = val

    # Telegram token (from skill_config or legacy top-level key)
    tg_token = skill_config.get("TELEGRAM_BOT_TOKEN", data.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if tg_token:
        agent_secrets["TELEGRAM_BOT_TOKEN"] = tg_token

    # Whisper language → stored as env_secret so orchestrator injects it
    wl = data.get("whisper_language", "de").strip()
    if wl:
        agent_secrets["WHISPER_LANGUAGE"] = wl

    # Global secrets (shared across all agents)
    global_secrets = {k: data[k].strip() for k in ("BRAVE_API_KEY", "OPENAI_API_KEY")
                      if data.get(k, "").strip()}

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, pid FROM agents WHERE name=%s", (name,))
                row = cur.fetchone()
                exists = row is not None
                if exists and (row["status"] in ("active", "running", "starting") or row.get("pid")):
                    return JSONResponse({"error": f"Agent '{name}' is {row['status']} (PID={row.get('pid')}) — stop first"}, status_code=409)
                if exists:
                    # Merge: preserve existing config keys not in the new config (e.g. web_flows, agent_editable_secrets set elsewhere)
                    cur.execute("SELECT config, updated_at FROM agents WHERE name=%s", (name,))
                    existing_row = cur.fetchone()
                    old_cfg = existing_row["config"] or {}
                    if isinstance(old_cfg, str):
                        old_cfg = json.loads(old_cfg)
                    # CR-179: Config backup before save
                    log.info(f"[Wizard] Config backup for '{name}': {json.dumps(old_cfg)[:500]}")
                    config["_last_config_backup"] = old_cfg.copy()
                    old_cfg.update(config)
                    # CR-184: Optimistic locking via updated_at
                    old_ts = existing_row["updated_at"]
                    cur.execute(
                        "UPDATE agents SET config=%s, updated_at=NOW() WHERE name=%s AND updated_at=%s",
                        (json.dumps(old_cfg), name, old_ts),
                    )
                    if cur.rowcount == 0:
                        conn.rollback()
                        return JSONResponse(
                            {"error": "Config was modified by another session. Please reload and try again."},
                            status_code=409,
                        )
                    if agent_secrets:
                        cur.execute("UPDATE agents SET env_secrets = env_secrets || %s WHERE name=%s", (json.dumps(agent_secrets), name))
                else:
                    cur.execute("INSERT INTO agents (name, status, config, env_secrets) VALUES (%s, 'idle', %s, %s)",
                                (name, json.dumps(config), json.dumps(agent_secrets)))
                for gk, gv in global_secrets.items():
                    cur.execute("INSERT INTO global_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
                                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                                (f"secret.{gk}", json.dumps(gv), json.dumps(gv)))
            conn.commit()
        restart_hint = ""
        if not exists or agent_secrets.get("TELEGRAM_BOT_TOKEN"):
            restart_hint = "NOTE: New agent or token change detected. A system restart (safe_restart.sh) is required for the Shared Listener to pick up the new Telegram bot."
        return {"status": "saved", "name": name, "is_new": not exists, "restart_hint": restart_hint}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Orchestrator & Shutdown ───────────────────────────────────────────────────

@router.get("/api/orchestrator/status", response_class=JSONResponse)
async def api_orchestrator_status():
    """Current VRAM occupant, queue depth, and orchestrator state."""
    orchestrator_on = _is_orchestrator_on()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, status FROM agents WHERE status IN ('active','running') LIMIT 1")
                occ = cur.fetchone()
                cur.execute(
                    "SELECT agent_name, COUNT(*) as cnt FROM pending_messages "
                    "WHERE processed=FALSE GROUP BY agent_name ORDER BY cnt DESC"
                )
                queue = [dict(r) for r in cur.fetchall()]
    except Exception:
        occ, queue = None, []
    # Check if orchestrator PROCESS is actually running (not just DB flag)
    import psutil as _ps
    orch_process_alive = any(
        "core.orchestrator" in " ".join(p.info.get("cmdline") or [])
        for p in _ps.process_iter(["cmdline"])
        if p.info.get("cmdline")
    )

    # Check voice_listener process
    voice_alive = any(
        "voice_listener" in " ".join(p.info.get("cmdline") or [])
        for p in _ps.process_iter(["cmdline"])
        if p.info.get("cmdline")
    )

    return {
        "orchestrator_enabled": orchestrator_on,
        "orchestrator_process": orch_process_alive,
        "voice_listener": voice_alive,
        "vram_occupant": occ["name"] if occ else None,
        "queue": queue,
        "total_pending": sum(q["cnt"] for q in queue),
    }


_autopilot_procs: dict[str, subprocess.Popen] = {}  # "listener" | "orchestrator" → Popen


@router.post("/api/orchestrator", response_class=JSONResponse)
async def api_toggle_orchestrator(request: Request):
    """Toggle Auto-Pilot: starts/stops shared_listener + orchestrator."""
    import signal as sig
    body = await request.json()
    enabled = bool(body.get("enabled", False))

    # Persist state
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO global_settings (key, value, updated_at) VALUES ('orchestrator_mode', %s, NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                            (json.dumps({"enabled": enabled}), json.dumps({"enabled": enabled})))
            conn.commit()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    # CR-090: re-enable watchdog when auto-pilot is toggled (on OR off — infra must stay alive)
    _app_module.infra_watchdog_active = True

    if enabled:
        # Kill any stale orchestrator/listener processes first (self-healing)
        import psutil as _ps
        for proc in _ps.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if proc.info["pid"] == os.getpid():
                    continue
                if "shared_listener" in cmdline or ("core.orchestrator" in cmdline and "dashboard" not in cmdline):
                    proc.kill()
                    log.info(f"Auto-Pilot: killed stale PID={proc.info['pid']}")
            except (_ps.NoSuchProcess, _ps.AccessDenied):
                pass

        # Start fresh
        python = sys.executable
        for name, cmd in [
            ("listener", [python, str(_ROOT_DIR / "scripts" / "shared_listener.py")]),
            ("orchestrator", [python, "-m", "core.orchestrator"]),
        ]:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, cwd=str(_ROOT_DIR),
                )
                _autopilot_procs[name] = proc
                log.info(f"Auto-Pilot: started {name} PID={proc.pid}")
            except Exception as exc:
                log.error(f"Failed to start {name}: {exc}")
    else:
        # Auto OFF: only flip the DB flag.
        # Orchestrator + Listener keep running — they handle manual start/stop
        # via requested_state even in manual mode. Killing them breaks everything.
        pass

    log.info(f"Orchestrator mode: {'ON' if enabled else 'OFF'}")
    return {"status": "ok", "orchestrator_on": enabled}


@router.post("/api/voice/toggle", response_class=JSONResponse)
async def api_toggle_voice(request: Request):
    """Start or stop the voice_listener process."""
    import psutil as _ps
    body = await request.json()
    action = body.get("action", "status")  # start | stop | status

    # Find existing voice_listener process
    voice_pid = None
    for proc in _ps.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "voice_listener" in cmdline and proc.info["pid"] != os.getpid():
                voice_pid = proc.info["pid"]
                break
        except (_ps.NoSuchProcess, _ps.AccessDenied):
            pass

    if action == "stop" and voice_pid:
        try:
            _ps.Process(voice_pid).kill()
            log.info(f"Voice listener killed (PID={voice_pid})")
        except Exception:
            pass
        return {"status": "stopped", "pid": voice_pid}

    elif action == "start" and not voice_pid:
        agent = body.get("agent", "voice_agent")
        device = body.get("device", "")
        out_device = body.get("output_device", device)  # same as input by default
        cmd = [sys.executable, str(_ROOT_DIR / "scripts" / "voice_listener.py"), "--agent", agent]
        if device:
            cmd += ["--device", str(device)]
        if out_device:
            cmd += ["--output-device", str(out_device)]
        log_file = open(_ROOT_DIR / "logs" / "voice_listener.log", "a")
        proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=log_file,
            start_new_session=True, cwd=str(_ROOT_DIR),
        )
        log_file.close()  # CR-191: close FD after Popen inherits it
        log.info(f"Voice listener started: agent={agent} device={device} PID={proc.pid}")
        return {"status": "started", "pid": proc.pid, "agent": agent}

    return {"status": "running" if voice_pid else "stopped", "pid": voice_pid}


@router.post("/api/system/shutdown", response_class=JSONResponse)
async def api_master_shutdown():
    """Nuclear shutdown: kill ALL AIMOS processes via psutil + flush VRAM."""
    _app_module.infra_watchdog_active = False  # CR-090: prevent watchdog respawn
    import psutil as _ps
    killed = []

    # 1. Kill tracked infrastructure subprocesses (listener + orchestrator)
    for name, proc in list(_autopilot_procs.items()):
        if proc.poll() is None:
            proc.kill()
            killed.append(f"{name}(PID={proc.pid})")
        _autopilot_procs.pop(name, None)

    # 2. Nuclear: find ALL AIMOS processes via psutil and SIGKILL
    my_pid = os.getpid()
    for proc in _ps.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if proc.info["pid"] == my_pid:
                continue  # don't kill ourselves (dashboard)
            if any(s in cmdline for s in ("main.py", "shared_listener", "core.orchestrator")):
                proc.kill()
                killed.append(f"psutil:{proc.info['pid']}")
        except (_ps.NoSuchProcess, _ps.AccessDenied):
            pass

    # 3. DB: all agents offline, orchestrator OFF
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE agents SET status='offline', updated_at=NOW()")
                cur.execute(
                    "UPDATE global_settings SET value=%s WHERE key='orchestrator_mode'",
                    (json.dumps({"enabled": False}),),
                )
            conn.commit()
    except Exception:
        pass

    # 4. Flush VRAM
    vram_flushed = False
    try:
        import httpx as _httpx
        tags = _httpx.get(f"{Config.LLM_BASE_URL.rstrip('/')}/api/tags", timeout=5)
        if tags.status_code == 200:
            for m in tags.json().get("models", []):
                _httpx.post(f"{Config.LLM_BASE_URL.rstrip('/')}/api/chat",
                            json={"model": m["name"], "messages": [], "keep_alive": 0}, timeout=5)
            vram_flushed = True
    except Exception:
        pass

    log.info(f"Nuclear shutdown: killed={killed}, vram_flushed={vram_flushed}")
    return {"status": "shutdown_complete", "killed": killed, "vram_flushed": vram_flushed}



@router.post("/api/logs/truncate", response_class=JSONResponse)
async def api_truncate_logs():
    """Truncate all log files to 0 bytes."""
    logs_dir = _ROOT_DIR / "logs"
    truncated = []
    if logs_dir.exists():
        for f in logs_dir.glob("*.log"):
            f.write_text("")
            truncated.append(f.name)
    return {"status": "truncated", "files": truncated}


# ── Start / Stop / Delete ────────────────────────────────────────────────────

@router.post("/api/agents/{agent_name}/start", response_class=JSONResponse)
async def api_start_agent(agent_name: str, request: Request):
    """Set requested_state='active' — orchestrator handles the actual spawn (always)."""
    name = agent_name.lower()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM agents WHERE name=%s", (name,))
                row = cur.fetchone()
                if not row:
                    return JSONResponse({"error": f"Agent '{name}' not found"}, status_code=404)
                if row["status"] in ("active", "running"):
                    return JSONResponse({"error": f"'{name}' already active"}, status_code=409)
                cur.execute("UPDATE agents SET requested_state='active', updated_at=NOW() WHERE name=%s", (name,))
            conn.commit()
        log.info(f"Dashboard: requested start for '{name}'")
        return {"status": "requested", "name": name}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agents/{agent_name}/stop", response_class=JSONResponse)
async def api_stop_agent(agent_name: str):
    """Set requested_state='offline' — orchestrator handles the actual kill (always)."""
    name = agent_name.lower()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE agents SET requested_state='offline', updated_at=NOW() WHERE name=%s", (name,))
            conn.commit()
        log.info(f"Dashboard: requested stop for '{name}'")
        return {"status": "requested", "name": name}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agents/{agent_name}/active", response_class=JSONResponse)
async def api_toggle_agent_active(agent_name: str, request: Request):
    """Toggle agent active/inactive for auto-mode team."""
    body = await request.json()
    active = bool(body.get("active", True))
    name = agent_name.lower()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT config FROM agents WHERE name=%s", (name,))
                row = cur.fetchone()
                if not row:
                    return JSONResponse({"error": f"Agent '{name}' not found"}, status_code=404)
                cfg = row["config"] or {}
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                cfg["active"] = active
                cur.execute("UPDATE agents SET config=%s, updated_at=NOW() WHERE name=%s", (json.dumps(cfg), name))
            conn.commit()
        return {"status": "ok", "name": name, "active": active}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Backup Settings ────────────────────────────────────────────────────────────

@router.get("/backup-settings")
async def backup_settings_page(request: Request):
    return templates.TemplateResponse("backup_settings.html", {"request": request})


@router.get("/api/backup/settings", response_class=JSONResponse)
async def api_backup_settings_get():
    """Get backup configuration from global_settings."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM global_settings WHERE key='backup_config'")
                row = cur.fetchone()
        if row:
            return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        return {
            "path": "/home/philipp/AIMOS/backups",
            "enabled": False,
            "frequency": "daily",
            "time": "02:00",
            "day": "7",
            "retain_daily": 7,
            "retain_weekly": 4,
            "retain_monthly": 3,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/backup/settings", response_class=JSONResponse)
async def api_backup_settings_save(request: Request):
    """Save backup configuration to global_settings."""
    data = await request.json()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO global_settings (key, value, updated_at) VALUES ('backup_config', %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (json.dumps(data), json.dumps(data))
                )
            conn.commit()

        # Update crontab if enabled
        _update_backup_cron(data)

        return {"status": "saved"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _update_backup_cron(config):
    """Update user crontab based on backup config."""
    script = str(Path(__file__).parent.parent.parent / "scripts" / "backup_full.sh")
    log_file = str(Path(__file__).parent.parent.parent / "logs" / "backup.log")

    # Remove existing AIMOS backup cron entry
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)  # CR-192
        existing = result.stdout if result.returncode == 0 else ""
        lines = [l for l in existing.strip().split('\n') if 'backup_full.sh' not in l and l.strip()]
    except Exception:
        lines = []

    if config.get("enabled"):
        time_parts = config.get("time", "02:00").split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
        freq = config.get("frequency", "daily")

        if freq == "daily":
            cron_expr = f"{minute} {hour} * * *"
        elif freq == "weekly":
            day = config.get("day", "7")
            cron_expr = f"{minute} {hour} * * {day}"
        elif freq == "monthly":
            cron_expr = f"{minute} {hour} 1 * *"
        else:
            cron_expr = f"{minute} {hour} * * *"

        lines.append(f"{cron_expr} {script} >> {log_file} 2>&1")

    # Write new crontab
    try:
        new_crontab = '\n'.join(lines) + '\n' if lines else ''
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True, timeout=10)  # CR-192
        log.info(f"Crontab updated: backup {'enabled' if config.get('enabled') else 'disabled'}")
    except Exception as exc:
        log.error(f"Crontab update failed: {exc}")


@router.delete("/api/agents/{agent_name}", response_class=JSONResponse)
async def api_delete_agent(agent_name: str):
    if _is_orchestrator_on():
        return JSONResponse({"error": "Orchestrator is ON — delete locked"}, status_code=409)
    name = agent_name.lower()
    # Request stop via orchestrator first
    await api_stop_agent(name)
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agents WHERE name=%s", (name,))
            conn.commit()
        return {"status": "deleted", "name": name}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Demo Page ────────────────────────────────────────────────────────────────

@router.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    return templates.TemplateResponse("demo.html", {"request": request})


@router.get("/api/demo/history/{agent_name}", response_class=JSONResponse)
async def demo_history(agent_name: str, since_id: int = 0):
    """Get chat history for demo view (incremental via since_id). Excludes scheduled job noise."""
    name = agent_name.lower()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, role, content, created_at FROM aimos_chat_histories "
                    "WHERE agent_name=%s AND id > %s "
                    "AND content NOT LIKE '%%[Scheduled Task]%%' "
                    "AND content NOT LIKE '%%täglichen Review%%' "
                    "AND content NOT LIKE '%%daily review%%' "
                    "AND content NOT LIKE '%%Daily Review%%' "
                    "AND content NOT LIKE '%%_icall_%%' "
                    "AND content NOT LIKE '%%<tool_call>%%' "
                    "AND role != 'tool' "
                    "ORDER BY id ASC LIMIT 50",
                    (name, since_id),
                )
                rows = cur.fetchall()
        result = []
        for r in rows:
            content = r["content"] or ""
            # Skip tool-call intermediates for cleaner view
            if content.strip().startswith("[Tool:") or content.strip().startswith('{"'):
                role = "tool"
            else:
                role = r["role"]
            result.append({
                "id": r["id"],
                "role": role,
                "content": content[:3000],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            })
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/demo/memory/{agent_name}", response_class=JSONResponse)
async def demo_memory(agent_name: str):
    """Get agent's SQLite memories for demo view."""
    import sqlite3
    from core.skills.base import BaseSkill
    db_path = BaseSkill.memory_db_path(agent_name.lower())
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, key, value, category, importance, updated_at "
            "FROM memories ORDER BY id DESC LIMIT 30"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/demo/reset/{agent_name}", response_class=JSONResponse)
async def demo_reset(agent_name: str):
    """Clear agent's chat history and memory for a fresh demo session."""
    import sqlite3
    from core.skills.base import BaseSkill
    name = agent_name.lower()
    cleared = {"history": 0, "memories": 0}
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM aimos_chat_histories WHERE agent_name=%s", (name,))
                cleared["history"] = cur.rowcount
                cur.execute("DELETE FROM pending_messages WHERE agent_name=%s", (name,))
                # Also delete messages FROM this agent to other agents (internal outbound)
                cur.execute(
                    "DELETE FROM pending_messages WHERE kind='internal' "
                    "AND content LIKE %s", (f'%[Nachricht von {name}]%',)
                )
                cur.execute("DELETE FROM agent_jobs WHERE agent_name=%s", (name,))
                cleared["jobs"] = cur.rowcount
            conn.commit()
    except Exception:
        pass
    try:
        db_path = BaseSkill.memory_db_path(name)
        if db_path.exists():
            conn = sqlite3.connect(str(db_path), timeout=3)
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.execute("DELETE FROM memories")
            conn.commit()
            conn.close()
            cleared["memories"] = count
    except Exception:
        pass
    # Force agent restart so it picks up clean state (empty history + memory)
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents SET requested_state='offline' WHERE name=%s", (name,)
                )
            conn.commit()
        # Brief pause for orchestrator to stop the agent
        import asyncio
        await asyncio.sleep(3)
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents SET requested_state='active' WHERE name=%s", (name,)
                )
            conn.commit()
        cleared["restarted"] = True
    except Exception:
        cleared["restarted"] = False

    return {"status": "reset", "agent": name, "cleared": cleared}


@router.get("/api/demo/workspace/{agent_name}", response_class=JSONResponse)
async def demo_workspace(agent_name: str):
    """List files in agent's workspace (public/ and root) for demo view."""
    from pathlib import Path
    name = agent_name.lower()
    workspace = Path("storage") / "agents" / name
    files = []
    skip = {"memory.db", "memory.db-shm", "memory.db-wal", "persistence.db",
            "api_audit.log", "external_api_audit.log"}
    for subdir in ["public", ""]:
        d = workspace / subdir if subdir else workspace
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.name not in skip:
                prefix = "public/" if subdir == "public" else ""
                files.append({
                    "name": prefix + f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                    "is_image": f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"),
                })
    return files


@router.get("/api/demo/status", response_class=JSONResponse)
async def demo_status():
    """Get status of demo agents (bauer_support + bauer_innendienst)."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, status, pid FROM agents "
                    "WHERE name IN ('agent_a', 'agent_b', 'bauer_backoffice') ORDER BY name"
                )
                rows = cur.fetchall()
        return [{"name": r["name"], "status": r["status"], "pid": r["pid"]} for r in rows]
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/demo/events/{agent_name}", response_class=JSONResponse)
async def demo_events(agent_name: str, since_id: int = 0):
    """Get recent pending_messages (inbound + outbound) for event log."""
    name = agent_name.lower()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, sender_id, content, processed, created_at "
                    "FROM pending_messages WHERE agent_name=%s AND id > %s "
                    "ORDER BY id ASC LIMIT 20",
                    (name, since_id),
                )
                rows = cur.fetchall()
        return [{
            "id": r["id"], "kind": r["kind"], "sender_id": r["sender_id"],
            "content": (r["content"] or "")[:3000],
            "processed": r["processed"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
        } for r in rows]
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


