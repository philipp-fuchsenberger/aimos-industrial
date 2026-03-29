"""
SchedulerSkill – Agenten koennen sich selbst Aufgaben fuer die Zukunft setzen.

Tools:
  set_reminder(when, task)  — Plant einen Job fuer einen bestimmten Zeitpunkt.
  list_jobs()               — Zeigt alle eigenen geplanten und gefeuerten Jobs.

Die Jobs werden in der zentralen DB-Tabelle agent_jobs gespeichert.
Der Orchestrator prueft regelmaessig auf faellige Jobs und triggert den Agenten.

Anti-Recursion: Jobs mit source='agent' werden nicht gefeuert, wenn der Agent
bereits einen pending scheduled_job hat (Loop-Protection).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("SchedulerSkill")

# Supported relative time formats: "in 5m", "in 2h", "in 1d"
_RELATIVE_UNITS = {"m": "minutes", "h": "hours", "d": "days", "s": "seconds"}


def _parse_when(when: str) -> datetime | None:
    """Parse a time specification into a UTC datetime.

    Accepts:
      - "in 5m", "in 2h", "in 30s", "in 1d"  (relative)
      - "2026-03-20 14:00"                     (absolute, assumed UTC)
      - "14:00"                                (today or tomorrow if past)
    """
    when = when.strip().lower()
    now = datetime.now(timezone.utc)

    # Relative: "in 5m", "in 2h"
    if when.startswith("in "):
        raw = when[3:].strip()
        if not raw:
            return None
        unit_char = raw[-1]
        if unit_char in _RELATIVE_UNITS:
            try:
                amount = int(raw[:-1].strip())
                delta = timedelta(**{_RELATIVE_UNITS[unit_char]: amount})
                return now + delta
            except (ValueError, OverflowError):
                return None

    # Absolute with date: "2026-03-20 14:00"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(when, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Time only: "14:00" → today or tomorrow
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(when, fmt).time()
            dt = datetime.combine(now.date(), t, tzinfo=timezone.utc)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
        except ValueError:
            continue

    return None


class SchedulerSkill(BaseSkill):
    """Agenten-Scheduler — plant Aufgaben fuer die Zukunft."""

    name = "scheduler"
    display_name = "Scheduler (Cronjobs)"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name

    def is_available(self) -> bool:
        return bool(self._agent_name)

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "set_reminder",
                "description": (
                    "Plant eine Aufgabe fuer einen bestimmten Zeitpunkt. "
                    "Beispiele: when='in 5m', when='in 2h', when='14:00', when='2026-03-20 14:00'. "
                    "Der Agent wird zum Zeitpunkt mit dem task-Prompt getriggert."
                ),
                "parameters": {
                    "when": {"type": "string", "description": "Zeitpunkt (relativ oder absolut)", "required": True},
                    "task": {"type": "string", "description": "Aufgabe / Prompt fuer den Agenten", "required": True},
                },
            },
            {
                "name": "list_jobs",
                "description": "Zeigt alle geplanten und ausgefuehrten Jobs dieses Agenten.",
                "parameters": {},
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "set_reminder":
            return await self._set_reminder(
                arguments.get("when", ""),
                arguments.get("task", ""),
            )
        elif tool_name == "list_jobs":
            return await self._list_jobs()
        return f"Unbekanntes Tool: {tool_name}"

    _MAX_PENDING_JOBS = 10  # CR-110: Max pending jobs per agent to prevent queue flooding

    async def _set_reminder(self, when: str, task: str) -> str:
        if not when or not task:
            return "Fehler: 'when' und 'task' sind Pflichtfelder."

        scheduled = _parse_when(when)
        if not scheduled:
            return (
                f"Fehler: Konnte '{when}' nicht als Zeitpunkt interpretieren. "
                "Beispiele: 'in 5m', 'in 2h', '14:00', '2026-03-20 14:00'"
            )

        # CR-144: Reminders max 24h in the future. For longer deadlines use add_event.
        from datetime import datetime as _dt, timezone as _tz
        hours_ahead = (scheduled - _dt.now(_tz.utc)).total_seconds() / 3600
        if hours_ahead > 24:
            return (
                f"Fehler: Reminder darf max. 24 Stunden in der Zukunft liegen "
                f"(angefragt: {hours_ahead:.0f}h). Fuer laengere Fristen nutze "
                f"add_event(date, title) — der Kalender erinnert automatisch."
            )

        try:
            import asyncpg
            from core.config import Config
            conn = await asyncpg.connect(**Config.get_db_params())

            # CR-110: Limit pending jobs per agent
            pending_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_jobs WHERE agent_name=$1 AND status='pending'",
                self._agent_name,
            )
            if pending_count >= self._MAX_PENDING_JOBS:
                await conn.close()
                return (
                    f"Fehler: Maximale Anzahl offener Jobs ({self._MAX_PENDING_JOBS}) erreicht. "
                    "Bitte warte bis bestehende Jobs abgearbeitet sind oder lösche alte Jobs."
                )

            jid = await conn.fetchval(
                "INSERT INTO agent_jobs (agent_name, scheduled_time, task_prompt, source) "
                "VALUES ($1, $2, $3, 'agent') RETURNING id",
                self._agent_name, scheduled, task,
            )
            await conn.close()
            ts_str = scheduled.strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info(f"[Scheduler] Job #{jid} for '{self._agent_name}' at {ts_str}: {task[:60]}")
            return f"Job #{jid} geplant fuer {ts_str}: {task}"
        except Exception as exc:
            logger.error(f"[Scheduler] set_reminder failed: {exc}")
            return f"Fehler beim Speichern: {exc}"

    async def _list_jobs(self) -> str:
        try:
            import asyncpg
            from core.config import Config
            conn = await asyncpg.connect(**Config.get_db_params())
            rows = await conn.fetch(
                "SELECT id, scheduled_time, task_prompt, status, fired_at "
                "FROM agent_jobs WHERE agent_name=$1 "
                "ORDER BY scheduled_time DESC LIMIT 20",
                self._agent_name,
            )
            await conn.close()
            if not rows:
                return "Keine Jobs geplant."
            lines = []
            for r in rows:
                ts = r["scheduled_time"].strftime("%Y-%m-%d %H:%M")
                status = r["status"]
                fired = f" (fired {r['fired_at'].strftime('%H:%M')})" if r["fired_at"] else ""
                lines.append(f"  #{r['id']} [{status}{fired}] {ts} — {r['task_prompt'][:60]}")
            return f"Jobs fuer '{self._agent_name}':\n" + "\n".join(lines)
        except Exception as exc:
            return f"Fehler: {exc}"
