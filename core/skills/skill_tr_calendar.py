"""
skill_tr_calendar – Tuerk takvimi farkindalik skill'i.

Tuerk resmi tatilleri, dini bayramlar ve ozel gunleri takip eder.
Ajanslara bugunun ozel bir gun olup olmadigini, yaklasan tatilleri
ve Ramazan durumunu bildirir.
"""

import logging
from datetime import date, timedelta
from typing import Any

from .base import BaseSkill

_log = logging.getLogger("AIMOS.TurkishCalendar")


# ---------------------------------------------------------------------------
# Holiday database
# ---------------------------------------------------------------------------

_MOOD_BAYRAM = "bayram"
_MOOD_ANMA = "anma"
_MOOD_GURUR = "gurur"

# Fixed holidays: (month, day) -> info
FIXED_HOLIDAYS: dict[tuple[int, int], dict] = {
    (1, 1): {
        "name": "Yilbasi",
        "greeting": "Yeni yiliniz kutlu olsun!",
        "mood": _MOOD_BAYRAM,
    },
    (4, 23): {
        "name": "Ulusal Egemenlik ve Cocuk Bayrami",
        "greeting": "23 Nisan Ulusal Egemenlik ve Cocuk Bayrami kutlu olsun!",
        "mood": _MOOD_GURUR,
    },
    (5, 1): {
        "name": "Emek ve Dayanisma Guenue",
        "greeting": "1 Mayis Emek ve Dayanisma Guenue kutlu olsun!",
        "mood": _MOOD_BAYRAM,
    },
    (5, 19): {
        "name": "Ataturk'u Anma, Genclik ve Spor Bayrami",
        "greeting": "19 Mayis Ataturk'u Anma, Genclik ve Spor Bayrami kutlu olsun!",
        "mood": _MOOD_GURUR,
    },
    (7, 15): {
        "name": "Demokrasi ve Milli Birlik Guenu",
        "greeting": "15 Temmuz Demokrasi ve Milli Birlik Guenu. Sehitlerimizi saygiyla aniyoruz.",
        "mood": _MOOD_ANMA,
    },
    (8, 30): {
        "name": "Zafer Bayrami",
        "greeting": "30 Agustos Zafer Bayrami kutlu olsun!",
        "mood": _MOOD_GURUR,
    },
    (10, 29): {
        "name": "Cumhuriyet Bayrami",
        "greeting": "Cumhuriyet Bayrami kutlu olsun! Yasamasin Cumhuriyet!",
        "mood": _MOOD_GURUR,
    },
    (11, 10): {
        "name": "Ataturk'u Anma Guenu",
        "greeting": "Ataturk'u saygi ve minnetle aniyoruz.",
        "mood": _MOOD_ANMA,
    },
}

# Religious holidays: exact date ranges (approximate, shifts ~11 days/year)
# Format: (start_date, end_date, info)
RELIGIOUS_HOLIDAYS: list[tuple[date, date, dict]] = [
    # --- 2025 ---
    (date(2025, 3, 30), date(2025, 4, 1), {
        "name": "Ramazan Bayrami",
        "greeting": "Ramazan Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
    (date(2025, 6, 6), date(2025, 6, 9), {
        "name": "Kurban Bayrami",
        "greeting": "Kurban Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
    # --- 2026 ---
    (date(2026, 3, 20), date(2026, 3, 22), {
        "name": "Ramazan Bayrami",
        "greeting": "Ramazan Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
    (date(2026, 5, 27), date(2026, 5, 30), {
        "name": "Kurban Bayrami",
        "greeting": "Kurban Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
    # --- 2027 ---
    (date(2027, 3, 10), date(2027, 3, 12), {
        "name": "Ramazan Bayrami",
        "greeting": "Ramazan Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
    (date(2027, 5, 16), date(2027, 5, 19), {
        "name": "Kurban Bayrami",
        "greeting": "Kurban Bayraminiz mubarek olsun!",
        "mood": _MOOD_BAYRAM,
    }),
]

# Approximate Ramadan periods (fasting month, before Ramazan Bayrami)
RAMADAN_PERIODS: list[tuple[date, date]] = [
    (date(2025, 3, 1), date(2025, 3, 29)),
    (date(2026, 2, 18), date(2026, 3, 19)),
    (date(2027, 2, 8), date(2027, 3, 9)),
]


def _parse_family_dates(raw: str) -> dict[tuple[int, int], str]:
    """Parse user-configured family dates from config textarea.

    Format per line: DD.MM=Aciklama
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
        date_part = date_part.strip()
        desc = desc.strip()
        if not desc:
            continue
        try:
            parts = date_part.split(".")
            day, month = int(parts[0]), int(parts[1])
            if 1 <= month <= 12 and 1 <= day <= 31:
                result[(month, day)] = desc
        except (ValueError, IndexError):
            _log.warning("Aile tarihi parse edilemedi: %s", line)
    return result


class TurkishCalendarSkill(BaseSkill):
    """Tuerk takvimi farkindalik skill'i — resmi tatiller, dini bayramlar, ozel gunler."""

    name = "tr_calendar_awareness"
    display_name = "Turkish Calendar Awareness"

    def __init__(self, config: dict | None = None):
        self._config = config or {}

    # ------------------------------------------------------------------
    # BaseSkill interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Her zaman kullanilabilir — harici bagimlilik yok."""
        return True

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "FAMILY_DATES",
                "label": "Aile oezel guenleri",
                "type": "textarea",
                "placeholder": "15.06=Ugur Abi'nin dogum guenu",
                "hint": "Format: GG.AA=Aciklama, her satir bir guen. Oern: 15.06=Ugur Abi'nin dogum guenu",
                "secret": False,
            },
        ]

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "check_today",
                "description": "Buguenuen tarihini ve oezel bir guen olup olmadigini kontrol eder.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "get_upcoming_holidays",
                "description": "Yaklasan tatil ve oezel guenleri listeler.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Kac guen ileriye bakilacak (varsayilan: 30)",
                            "default": 30,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "is_ramadan",
                "description": "Su an Ramazan ayinda olup olmadigimizi kontrol eder.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "check_today":
            return self.check_today()
        elif tool_name == "get_upcoming_holidays":
            days = arguments.get("days", 30)
            return self.get_upcoming_holidays(days=int(days))
        elif tool_name == "is_ramadan":
            return self.is_ramadan()
        return await super().execute_tool(tool_name, arguments)

    async def enrich_context(self, user_text: str) -> str:
        """Otomatik olarak bugunun takvim bilgisini kontekste ekler."""
        parts: list[str] = []
        today_info = self.check_today()
        if today_info:
            parts.append(today_info)
        ramadan_info = self.is_ramadan()
        if "Ramazan" in ramadan_info:
            parts.append(ramadan_info)
        if not parts:
            return ""
        return "[Tuerk Takvimi] " + " | ".join(parts)

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _get_family_dates(self) -> dict[tuple[int, int], str]:
        raw = self._config.get("FAMILY_DATES", "")
        return _parse_family_dates(raw)

    def _lookup_date(self, d: date) -> list[dict]:
        """Return all holidays/special days matching a given date."""
        results: list[dict] = []

        # Fixed holidays
        key = (d.month, d.day)
        if key in FIXED_HOLIDAYS:
            results.append(FIXED_HOLIDAYS[key])

        # Religious holidays
        for start, end, info in RELIGIOUS_HOLIDAYS:
            if start <= d <= end:
                results.append(info)

        # Family dates
        family = self._get_family_dates()
        if key in family:
            results.append({
                "name": family[key],
                "greeting": f"Bugun oezel bir gun: {family[key]}",
                "mood": _MOOD_BAYRAM,
            })

        return results

    def check_today(self) -> str:
        """Buguenuen tarihini ve oezel gun bilgisini doeneduerueer."""
        today = date.today()
        day_names_tr = [
            "Pazartesi", "Sali", "Carsamba", "Persembe",
            "Cuma", "Cumartesi", "Pazar",
        ]
        month_names_tr = [
            "", "Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
            "Temmuz", "Agustos", "Eyluel", "Ekim", "Kasim", "Aralik",
        ]

        day_name = day_names_tr[today.weekday()]
        date_str = f"{today.day} {month_names_tr[today.month]} {today.year}, {day_name}"

        matches = self._lookup_date(today)
        if not matches:
            return f"Bugun {date_str}. Oezel bir gun degil."

        lines = [f"Bugun {date_str}."]
        for m in matches:
            lines.append(f"  * {m['name']} — {m['greeting']}")
        return "\n".join(lines)

    def get_upcoming_holidays(self, days: int = 30) -> str:
        """Yaklasan N guen icindeki tatil ve oezel guenleri listeler."""
        today = date.today()
        end = today + timedelta(days=days)
        upcoming: list[tuple[date, dict]] = []

        month_names_tr = [
            "", "Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
            "Temmuz", "Agustos", "Eyluel", "Ekim", "Kasim", "Aralik",
        ]

        # Fixed holidays in range
        for (month, day), info in FIXED_HOLIDAYS.items():
            for year in (today.year, today.year + 1):
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                if today < d <= end:
                    upcoming.append((d, info))

        # Religious holidays in range
        for start, end_date, info in RELIGIOUS_HOLIDAYS:
            if today < start <= end:
                upcoming.append((start, info))

        # Family dates in range
        family = self._get_family_dates()
        for (month, day), desc in family.items():
            for year in (today.year, today.year + 1):
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                if today < d <= end:
                    upcoming.append((d, {
                        "name": desc,
                        "greeting": f"Oezel gun: {desc}",
                        "mood": _MOOD_BAYRAM,
                    }))

        if not upcoming:
            return f"Oenumuezdeki {days} guen icinde oezel bir gun yok."

        upcoming.sort(key=lambda x: x[0])
        lines = [f"Oenumuezdeki {days} guen icindeki oezel gunler:"]
        for d, info in upcoming:
            remaining = (d - today).days
            d_str = f"{d.day} {month_names_tr[d.month]}"
            lines.append(f"  * {d_str} ({remaining} guen sonra) — {info['name']}")
        return "\n".join(lines)

    def is_ramadan(self) -> str:
        """Ramazan durumunu kontrol eder."""
        today = date.today()

        for start, end in RAMADAN_PERIODS:
            if start <= today <= end:
                remaining = (end - today).days
                return (
                    f"Evet, su an Ramazan ayindayiz. "
                    f"Ramazan'in bitmesine {remaining} guen kaldi. "
                    f"Hayirli Ramazanlar!"
                )
            # Check if Eid is within the next 3 days after Ramadan ends
            eid_start = end + timedelta(days=1)
            eid_end = end + timedelta(days=3)
            if eid_start <= today <= eid_end:
                return "Ramazan yeni bitti, Ramazan Bayrami donemindeyiz! Bayraminiz mubarek olsun!"

        # Check if Ramadan is approaching (within 7 days)
        for start, end in RAMADAN_PERIODS:
            days_until = (start - today).days
            if 0 < days_until <= 7:
                return f"Ramazan'a {days_until} guen kaldi. Hazirliklara baslanabilir."

        return "Su an Ramazan donemi degil."
