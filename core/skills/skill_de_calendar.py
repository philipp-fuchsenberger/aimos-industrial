"""
skill_de_calendar – German calendar awareness skill.

Tracks German public holidays (nationwide + Bavaria), bridge days,
school holidays hints, and user-configured special dates.
"""

import logging
from datetime import date, timedelta
from typing import Any

from .base import BaseSkill

_log = logging.getLogger("AIMOS.GermanCalendar")

# ---------------------------------------------------------------------------
# Holiday database
# ---------------------------------------------------------------------------

# Fixed nationwide holidays: (month, day) -> info
FIXED_HOLIDAYS: dict[tuple[int, int], dict] = {
    (1, 1): {"name": "Neujahr", "note": "Gesetzlicher Feiertag"},
    (1, 6): {"name": "Heilige Drei Koenige", "note": "Feiertag in Bayern, BW, ST"},
    (5, 1): {"name": "Tag der Arbeit", "note": "Gesetzlicher Feiertag"},
    (8, 15): {"name": "Mariae Himmelfahrt", "note": "Feiertag in Bayern (katholische Gemeinden)"},
    (10, 3): {"name": "Tag der Deutschen Einheit", "note": "Gesetzlicher Feiertag"},
    (11, 1): {"name": "Allerheiligen", "note": "Feiertag in Bayern, BW, NRW, RP, SL"},
    (12, 25): {"name": "1. Weihnachtsfeiertag", "note": "Gesetzlicher Feiertag"},
    (12, 26): {"name": "2. Weihnachtsfeiertag", "note": "Gesetzlicher Feiertag"},
    (12, 31): {"name": "Silvester", "note": "Kein Feiertag, aber viele Betriebe geschlossen"},
}


def _easter(year: int) -> date:
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _get_moveable_holidays(year: int) -> dict[date, dict]:
    """Easter-dependent holidays."""
    e = _easter(year)
    return {
        e - timedelta(days=2): {"name": "Karfreitag", "note": "Gesetzlicher Feiertag"},
        e: {"name": "Ostersonntag", "note": "Feiertag in Brandenburg"},
        e + timedelta(days=1): {"name": "Ostermontag", "note": "Gesetzlicher Feiertag"},
        e + timedelta(days=39): {"name": "Christi Himmelfahrt", "note": "Gesetzlicher Feiertag (Vatertag)"},
        e + timedelta(days=49): {"name": "Pfingstsonntag", "note": "Feiertag in Brandenburg"},
        e + timedelta(days=50): {"name": "Pfingstmontag", "note": "Gesetzlicher Feiertag"},
        e + timedelta(days=60): {"name": "Fronleichnam", "note": "Feiertag in Bayern, BW, HE, NRW, RP, SL"},
    }


# Bavarian school holidays (Schulferien Bayern)
SCHOOL_HOLIDAYS_BAYERN: list[tuple[date, date, str]] = [
    # 2025
    (date(2025, 2, 28), date(2025, 3, 8), "Winterferien Bayern"),
    (date(2025, 4, 14), date(2025, 4, 25), "Osterferien Bayern"),
    (date(2025, 6, 10), date(2025, 6, 20), "Pfingstferien Bayern"),
    (date(2025, 7, 28), date(2025, 9, 8), "Sommerferien Bayern"),
    (date(2025, 10, 27), date(2025, 10, 31), "Herbstferien Bayern"),
    (date(2025, 12, 22), date(2026, 1, 5), "Weihnachtsferien Bayern"),
    # 2026
    (date(2026, 2, 16), date(2026, 2, 20), "Winterferien Bayern"),
    (date(2026, 3, 30), date(2026, 4, 10), "Osterferien Bayern"),
    (date(2026, 5, 26), date(2026, 6, 5), "Pfingstferien Bayern"),
    (date(2026, 7, 27), date(2026, 9, 7), "Sommerferien Bayern"),
    (date(2026, 10, 26), date(2026, 10, 30), "Herbstferien Bayern"),
    (date(2026, 12, 23), date(2027, 1, 7), "Weihnachtsferien Bayern"),
    # 2027
    (date(2027, 2, 15), date(2027, 2, 19), "Winterferien Bayern"),
    (date(2027, 3, 22), date(2027, 4, 2), "Osterferien Bayern"),
    (date(2027, 5, 18), date(2027, 5, 28), "Pfingstferien Bayern"),
]


def _parse_special_dates(raw: str) -> dict[tuple[int, int], str]:
    """Parse user-configured special dates from config textarea.

    Format per line: DD.MM=Description
    Returns {(month, day): description}
    """
    result: dict[tuple[int, int], str] = {}
    if not raw:
        return result
    for line in raw.strip().splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        date_part, desc = line.split("=", 1)
        desc = desc.strip()
        if not desc:
            continue
        try:
            parts = date_part.strip().split(".")
            day, month = int(parts[0]), int(parts[1])
            if 1 <= month <= 12 and 1 <= day <= 31:
                result[(month, day)] = desc
        except (ValueError, IndexError):
            _log.warning("Could not parse special date: %s", line)
    return result


class GermanCalendarSkill(BaseSkill):
    """German calendar awareness — public holidays, bridge days, special dates."""

    name = "de_calendar_awareness"
    display_name = "German Calendar Awareness"

    def __init__(self, config: dict | None = None, **kwargs):
        self._config = config or {}

    def is_available(self) -> bool:
        return True

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "SPECIAL_DATES",
                "label": "Special dates",
                "type": "textarea",
                "placeholder": "15.06=Birthday CEO\n24.12=Company Christmas party",
                "hint": "Format: DD.MM=Description, one per line.",
                "secret": False,
            },
        ]

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "check_today",
                "description": "Check today's date and whether it is a public holiday or special day in Germany.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "get_upcoming_holidays",
                "description": "List upcoming German public holidays, bridge days, and special dates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "How many days to look ahead (default: 30)",
                            "default": 30,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "check_bridge_days",
                "description": "Find potential bridge days (Brueckentage) — single workdays between a holiday and a weekend.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "check_today":
            return self._check_today()
        elif tool_name == "get_upcoming_holidays":
            days = arguments.get("days", 30)
            return self._get_upcoming(days=int(days))
        elif tool_name == "check_bridge_days":
            return self._check_bridge_days()
        return await super().execute_tool(tool_name, arguments)

    async def enrich_context(self, user_text: str) -> str:
        """Automatically inject today's calendar info into context."""
        info = self._check_today()
        if "Kein Feiertag" in info and "Kein besonderer" in info:
            return ""
        return f"[German Calendar] {info}"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _all_holidays(self, year: int) -> dict[date, dict]:
        """Get all holidays for a year (fixed + moveable)."""
        holidays: dict[date, dict] = {}
        for (m, d), info in FIXED_HOLIDAYS.items():
            try:
                holidays[date(year, m, d)] = info
            except ValueError:
                pass
        holidays.update(_get_moveable_holidays(year))
        return holidays

    def _check_today(self) -> str:
        today = date.today()
        day_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        month_names = ["", "Januar", "Februar", "Maerz", "April", "Mai", "Juni",
                       "Juli", "August", "September", "Oktober", "November", "Dezember"]

        date_str = f"{day_names[today.weekday()]}, {today.day}. {month_names[today.month]} {today.year}"

        holidays = self._all_holidays(today.year)
        special = _parse_special_dates(self._config.get("SPECIAL_DATES", ""))

        lines = [f"Today: {date_str}"]

        if today in holidays:
            h = holidays[today]
            lines.append(f"  Holiday: {h['name']} ({h['note']})")

        # School holidays
        for start, end, name in SCHOOL_HOLIDAYS_BAYERN:
            if start <= today <= end:
                remaining = (end - today).days
                lines.append(f"  School holiday: {name} (until {end.strftime('%d.%m.')}, {remaining} days left)")
                lines.append("  Note: Many employees may be on vacation. Service response times could be longer.")
                break

        key = (today.month, today.day)
        if key in special:
            lines.append(f"  Special: {special[key]}")

        if today not in holidays and key not in special and not any(s <= today <= e for s, e, _ in SCHOOL_HOLIDAYS_BAYERN):
            lines.append("  Regular business day.")

        return "\n".join(lines)

    def _get_upcoming(self, days: int = 30) -> str:
        today = date.today()
        end = today + timedelta(days=days)
        upcoming: list[tuple[date, str, str]] = []

        for year in (today.year, today.year + 1):
            for d, info in self._all_holidays(year).items():
                if today < d <= end:
                    upcoming.append((d, info["name"], info["note"]))

        special = _parse_special_dates(self._config.get("SPECIAL_DATES", ""))
        for (m, dy), desc in special.items():
            for year in (today.year, today.year + 1):
                try:
                    d = date(year, m, dy)
                except ValueError:
                    continue
                if today < d <= end:
                    upcoming.append((d, desc, "Special date"))

        if not upcoming:
            return f"No holidays or special dates in the next {days} days."

        upcoming.sort(key=lambda x: x[0])
        lines = [f"Upcoming in the next {days} days:"]
        for d, name, note in upcoming:
            remaining = (d - today).days
            lines.append(f"  {d.strftime('%d.%m.')} ({remaining}d) — {name} ({note})")
        return "\n".join(lines)

    def _check_bridge_days(self) -> str:
        """Find bridge days: workdays that, if taken off, create a long weekend."""
        today = date.today()
        end = today + timedelta(days=120)
        holidays = self._all_holidays(today.year)
        holidays.update(self._all_holidays(today.year + 1))

        bridges: list[tuple[date, str]] = []
        for d, info in holidays.items():
            if d < today or d > end:
                continue
            # Check day before holiday: if it's a workday and the day before that is weekend
            before = d - timedelta(days=1)
            if before.weekday() < 5 and (before - timedelta(days=1)).weekday() >= 5:
                bridges.append((before, f"Bridge day before {info['name']} ({d.strftime('%d.%m.')})"))
            # Check day after holiday: if it's a workday and the day after that is weekend
            after = d + timedelta(days=1)
            if after.weekday() < 5 and (after + timedelta(days=1)).weekday() >= 5:
                bridges.append((after, f"Bridge day after {info['name']} ({d.strftime('%d.%m.')})"))

        if not bridges:
            return "No bridge days found in the next 4 months."

        bridges.sort(key=lambda x: x[0])
        # Deduplicate
        seen = set()
        unique = []
        for d, desc in bridges:
            if d not in seen:
                seen.add(d)
                unique.append((d, desc))

        lines = ["Potential bridge days (Brueckentage):"]
        for d, desc in unique:
            lines.append(f"  {d.strftime('%a %d.%m.%Y')} — {desc}")
        return "\n".join(lines)
