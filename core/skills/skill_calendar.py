"""
CR-144: Agent Calendar — persistent event tracking in workspace.

Events are stored as JSON in the agent's workspace (calendar.json).
No cronjobs needed — the agent checks upcoming events at every startup
and they are injected into the system prompt automatically.

Tools:
  add_event(date, title, time, notes)   — Add a calendar event
  list_events(days_ahead)               — Show upcoming events
  complete_event(title)                  — Mark an event as done
  delete_event(title)                    — Remove an event

For short-term reminders (<24h): use set_reminder (cronjob).
For everything longer: use add_event (calendar).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("AIMOS.Calendar")


class CalendarSkill(BaseSkill):
    """Persistent calendar for agents — stored in workspace, no cronjobs."""

    name = "calendar"
    display_name = "Calendar (Events & Deadlines)"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name

    def is_available(self) -> bool:
        return bool(self._agent_name)

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "add_event",
                "description": (
                    "Add an event or deadline to your calendar. "
                    "Use this for anything more than 24 hours away. "
                    "Examples: meetings, deadlines, follow-ups, birthdays. "
                    "The calendar is checked automatically at every startup."
                ),
                "parameters": {
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD)", "required": True},
                    "title": {"type": "string", "description": "Event title", "required": True},
                    "time": {"type": "string", "description": "Time (HH:MM), optional", "default": ""},
                    "notes": {"type": "string", "description": "Additional notes, optional", "default": ""},
                },
            },
            {
                "name": "list_events",
                "description": "Show upcoming calendar events. Default: next 14 days.",
                "parameters": {
                    "days_ahead": {"type": "integer", "description": "How many days to look ahead", "default": 14},
                },
            },
            {
                "name": "complete_event",
                "description": "Mark a calendar event as completed.",
                "parameters": {
                    "title": {"type": "string", "description": "Event title (or partial match)", "required": True},
                },
            },
            {
                "name": "delete_event",
                "description": "Permanently remove a calendar event.",
                "parameters": {
                    "title": {"type": "string", "description": "Event title (or partial match)", "required": True},
                },
            },
        ]

    def _calendar_path(self) -> Path:
        ws = self.workspace_path(self._agent_name)
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "calendar.json"

    def _load(self) -> list[dict]:
        path = self._calendar_path()
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, events: list[dict]):
        path = self._calendar_path()
        path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "add_event":
            return self._add_event(
                arguments.get("date", ""),
                arguments.get("title", ""),
                arguments.get("time", ""),
                arguments.get("notes", ""),
            )
        elif tool_name == "list_events":
            return self._list_events(int(arguments.get("days_ahead", 14)))
        elif tool_name == "complete_event":
            return self._complete_event(arguments.get("title", ""))
        elif tool_name == "delete_event":
            return self._delete_event(arguments.get("title", ""))
        return f"Unknown tool: {tool_name}"

    def _add_event(self, date: str, title: str, time: str = "", notes: str = "") -> str:
        if not date or not title:
            return "Error: 'date' and 'title' are required."
        # Validate date
        try:
            dt = datetime.strptime(date.strip(), "%Y-%m-%d")
        except ValueError:
            return f"Error: Invalid date '{date}'. Use YYYY-MM-DD format."

        events = self._load()
        event = {
            "date": date.strip(),
            "title": title.strip(),
            "time": time.strip() if time else None,
            "notes": notes.strip() if notes else None,
            "done": False,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        events.append(event)
        # Sort by date
        events.sort(key=lambda e: e.get("date", "9999") + (e.get("time") or "99:99"))
        self._save(events)

        time_str = f" at {time}" if time else ""
        logger.info(f"[Calendar] {self._agent_name}: added '{title}' on {date}{time_str}")
        return f"Event added: {title} on {date}{time_str}"

    def _list_events(self, days_ahead: int = 14) -> str:
        events = self._load()
        if not events:
            return "Calendar is empty."

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        lines = []
        overdue = []
        upcoming = []
        done_recent = []

        for e in events:
            d = e.get("date", "")
            title = e.get("title", "?")
            time = e.get("time") or ""
            notes = e.get("notes") or ""
            is_done = e.get("done", False)

            if is_done and d >= today:
                done_recent.append(f"  [x] {d} {time} {title}")
            elif d < today and not is_done:
                overdue.append(f"  [!] {d} {time} {title}" + (f" — {notes}" if notes else ""))
            elif d <= cutoff and not is_done:
                marker = ">>>" if d == today else "   "
                upcoming.append(f"  {marker} {d} {time} {title}" + (f" — {notes}" if notes else ""))

        if overdue:
            lines.append("OVERDUE:")
            lines.extend(overdue)
        if upcoming:
            lines.append(f"UPCOMING ({days_ahead} days):")
            lines.extend(upcoming)
        if done_recent:
            lines.append("COMPLETED:")
            lines.extend(done_recent)

        if not lines:
            return f"No events in the next {days_ahead} days."
        return "\n".join(lines)

    def _complete_event(self, title: str) -> str:
        if not title:
            return "Error: 'title' is required."
        events = self._load()
        found = False
        for e in events:
            if title.lower() in e.get("title", "").lower() and not e.get("done"):
                e["done"] = True
                e["completed"] = datetime.now(timezone.utc).isoformat()
                found = True
                logger.info(f"[Calendar] {self._agent_name}: completed '{e['title']}'")
                break
        if found:
            self._save(events)
            return f"Event completed: {title}"
        return f"No open event matching '{title}' found."

    def _delete_event(self, title: str) -> str:
        if not title:
            return "Error: 'title' is required."
        events = self._load()
        before = len(events)
        events = [e for e in events if title.lower() not in e.get("title", "").lower()]
        if len(events) < before:
            self._save(events)
            logger.info(f"[Calendar] {self._agent_name}: deleted '{title}'")
            return f"Event deleted: {title}"
        return f"No event matching '{title}' found."


def get_calendar_context(agent_name: str) -> str:
    """Load upcoming/overdue events for injection into system prompt.

    Called at every think() cycle — lightweight JSON file read.
    Returns formatted string or empty string if no events.
    """
    try:
        ws = BaseSkill.workspace_path(agent_name)
        cal_path = ws / "calendar.json"
        if not cal_path.exists():
            return ""

        events = json.loads(cal_path.read_text(encoding="utf-8"))
        if not events:
            return ""

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        week_ahead = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

        lines = []
        for e in events:
            if e.get("done"):
                continue
            d = e.get("date", "")
            title = e.get("title", "?")
            time = e.get("time") or ""
            notes = e.get("notes") or ""

            if d < today:
                lines.append(f"[OVERDUE] {d} {time} {title}" + (f" — {notes}" if notes else ""))
            elif d == today:
                lines.append(f"[TODAY] {time} {title}" + (f" — {notes}" if notes else ""))
            elif d == tomorrow:
                lines.append(f"[TOMORROW] {time} {title}" + (f" — {notes}" if notes else ""))
            elif d <= week_ahead:
                lines.append(f"[{d}] {time} {title}")

        if not lines:
            return ""
        return "\n<calendar>\n" + "\n".join(lines) + "\n</calendar>"

    except Exception:
        return ""
