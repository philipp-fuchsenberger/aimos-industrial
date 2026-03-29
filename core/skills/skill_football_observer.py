"""
FootballObserverSkill – Galatasaray Futbol Takipcisi.

Uecretsiz RSS kaynaklarindan Galatasaray mac sonuclarini ve haberlerini takip eder.
Brave Search API kullanmaz (kota tasarrufu).

Tools:
  check_galatasaray()  — Son mac sonucu ve duygu durumu getirir.
  get_football_mood()  — Son sonuca gore duygu durumu dondueruer.

Duygu durumlari:
  ZAFER   — Galibiyet → oforik
  MAGLUP  — Maglubiyet → sinirli/atesli
  BERABERE — Beraberlik → kararsiz
  NORMAL  — Yakin zamanda mac yok
"""

import json
import logging
import re
import time
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("FootballObserverSkill")

_RSS_URL = "https://www.fanatik.com.tr/rss/galatasaray"
_FETCH_INTERVAL = 3600  # 1 saat (saniye)

# Skor deseni: "2-1", "3 - 0" vb.
_SCORE_PATTERN = re.compile(r"(\d+)\s*[-–]\s*(\d+)")

# Anahtar kelimeler
_WIN_KEYWORDS = ["kazandı", "kazandi", "galip", "zafer", "yendi", "win", "won"]
_LOSE_KEYWORDS = ["kaybetti", "mağlup", "maglup", "yenildi", "yenilgi", "lost", "lose"]
_DRAW_KEYWORDS = ["berabere", "draw", "eşitlik", "esitlik"]


def _parse_feed_xml(xml_text: str) -> list[dict]:
    """RSS XML'i basit regex ile parse et (feedparser yoksa fallback)."""
    items = []
    # <item>...</item> bloklarini bul
    for match in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        block = match.group(1)
        title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", block, re.DOTALL)
        if not title_m:
            title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        desc_m = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", block, re.DOTALL)
        if not desc_m:
            desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
        items.append({
            "title": title_m.group(1).strip() if title_m else "",
            "description": desc_m.group(1).strip() if desc_m else "",
        })
    return items


def _detect_mood(text: str) -> tuple[str, str]:
    """Metinden duygu durumu ve ozet cikar.

    Returns:
        (mood, summary) — mood: ZAFER | MAGLUP | BERABERE | NORMAL
    """
    lower = text.lower()

    # Skor bul
    score_match = _SCORE_PATTERN.search(text)
    score_str = score_match.group(0) if score_match else ""

    # Galatasaray'in galibiyeti, maglubiyeti veya beraberligi
    for kw in _WIN_KEYWORDS:
        if kw in lower:
            return "ZAFER", f"Galatasaray galip! {score_str}".strip()
    for kw in _LOSE_KEYWORDS:
        if kw in lower:
            return "MAGLUP", f"Galatasaray maglup... {score_str}".strip()
    for kw in _DRAW_KEYWORDS:
        if kw in lower:
            return "BERABERE", f"Berabere kaldi. {score_str}".strip()

    # Skor varsa ama anahtar kelime yoksa — skoru yorumla
    if score_match:
        a, b = int(score_match.group(1)), int(score_match.group(2))
        # Galatasaray genelde ilk isimdir ama kesin degiliz; NORMAL don
        if a > b:
            return "ZAFER", f"Skor: {score_str} — muhtemelen galibiyet!"
        elif a < b:
            return "MAGLUP", f"Skor: {score_str} — muhtemelen maglubiyet."
        else:
            return "BERABERE", f"Skor: {score_str} — berabere."

    return "NORMAL", ""


_MOOD_PROMPTS = {
    "ZAFER": (
        "Galatasaray kazandi! Oforik ve gururlu bir ruh halinde. "
        "Konusmalarinda zafer coskusu yansit, pozitif ve enerjik ol."
    ),
    "MAGLUP": (
        "Galatasaray kaybetti. Sinirli ve atesli bir ruh halinde. "
        "Konusmalarinda biraz asabi ol, ama yapici kal."
    ),
    "BERABERE": (
        "Galatasaray berabere kaldi. Kararsiz ve dusunceli bir ruh halinde. "
        "Ne mutlu ne uzgun — analitik ol."
    ),
    "NORMAL": "",
}


class FootballObserverSkill(BaseSkill):
    """Galatasaray Futbol Takipcisi — uecretsiz RSS ile mac sonuclari."""

    name = "football_observer"
    display_name = "Football Observer"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name
        self._last_result: str = ""
        self._last_mood: str = "NORMAL"
        self._last_summary: str = ""
        self._last_fetch_time: float = 0.0
        self._load_state()

    # ---------- state persistence via skill_state (SQLite) ----------

    def _state_db_path(self):
        return self.workspace_path(self._agent_name) / "skill_state.db" if self._agent_name else None

    def _load_state(self):
        """SQLite'den onceki durumu yukle."""
        if not self._agent_name:
            return
        try:
            import sqlite3
            db_path = self._state_db_path()
            if not db_path or not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute(
                "SELECT value FROM skill_state WHERE skill=? AND key=?",
                ("football_observer", "state"),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                data = json.loads(row[0])
                self._last_result = data.get("last_result", "")
                self._last_mood = data.get("last_mood", "NORMAL")
                self._last_summary = data.get("last_summary", "")
                self._last_fetch_time = data.get("last_fetch_time", 0.0)
        except Exception as exc:
            logger.debug(f"State yuekleme basarisiz (ilk calistirma olabilir): {exc}")

    def _save_state(self):
        """Durumu SQLite'e kaydet."""
        if not self._agent_name:
            return
        try:
            import sqlite3
            db_path = self._state_db_path()
            if not db_path:
                return
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS skill_state "
                "(skill TEXT, key TEXT, value TEXT, PRIMARY KEY (skill, key))"
            )
            data = json.dumps({
                "last_result": self._last_result,
                "last_mood": self._last_mood,
                "last_summary": self._last_summary,
                "last_fetch_time": self._last_fetch_time,
            })
            conn.execute(
                "INSERT OR REPLACE INTO skill_state (skill, key, value) VALUES (?, ?, ?)",
                ("football_observer", "state", data),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"State kaydetme hatasi: {exc}")

    # ---------- BaseSkill interface ----------

    def is_available(self) -> bool:
        return True  # Harici API key gerektirmez

    @classmethod
    def config_fields(cls) -> list[dict]:
        return []  # Konfiguerasyon gerektirmez

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "check_galatasaray",
                "description": (
                    "Galatasaray'in son mac sonucunu ve haberlerini RSS ile getirir. "
                    "Sonuc ve duygu durumunu dondueruer."
                ),
                "parameters": {},
            },
            {
                "name": "get_football_mood",
                "description": (
                    "Son bilinen mac sonucuna gore duygu durumunu dondueruer: "
                    "ZAFER (galibiyet), MAGLUP (maglubiyet), BERABERE, NORMAL."
                ),
                "parameters": {},
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "check_galatasaray":
            return await self._check_galatasaray()
        elif tool_name == "get_football_mood":
            return self._get_football_mood()
        return f"Bilinmeyen tool: {tool_name}"

    async def enrich_context(self, user_text: str) -> str:
        """Duygu durumunu system prompt'a enjekte et."""
        if self._last_mood and self._last_mood != "NORMAL":
            prompt = _MOOD_PROMPTS.get(self._last_mood, "")
            if prompt:
                return f"[Futbol Duygu Durumu: {self._last_mood}] {prompt}"
        return ""

    # ---------- tool implementations ----------

    async def _check_galatasaray(self) -> str:
        """RSS'den Galatasaray haberlerini cek ve analiz et."""
        now = time.time()

        # Cache kontrolue — 1 saatten yeni ise tekrar cekme
        if (now - self._last_fetch_time) < _FETCH_INTERVAL and self._last_result:
            return (
                f"Son kontrol {int((now - self._last_fetch_time) / 60)} dk oence yapildi.\n"
                f"Sonuc: {self._last_result}\n"
                f"Duygu durumu: {self._last_mood}"
            )

        items = await self._fetch_rss()
        if not items:
            return "RSS beslemesi alinamadi veya bos. Daha sonra tekrar dene."

        # Tum basliklari birlestir ve analiz et
        all_text = " ".join(item.get("title", "") + " " + item.get("description", "") for item in items[:10])
        mood, summary = _detect_mood(all_text)

        # En son basliklari goster
        headlines = [item.get("title", "?") for item in items[:5]]
        result_text = "Son Galatasaray haberleri:\n" + "\n".join(f"  - {h}" for h in headlines)
        if summary:
            result_text += f"\n\nAnaliz: {summary}"
        result_text += f"\nDuygu durumu: {mood}"

        # State guncelle
        self._last_result = result_text
        self._last_mood = mood
        self._last_summary = summary
        self._last_fetch_time = now
        self._save_state()

        return result_text

    def _get_football_mood(self) -> str:
        """Mevcut duygu durumunu donduer."""
        if not self._last_fetch_time:
            return (
                "Henuz mac kontrolue yapilmadi. "
                "Oence check_galatasaray() ile kontrol et.\n"
                "Duygu durumu: NORMAL"
            )

        age_min = int((time.time() - self._last_fetch_time) / 60)
        mood_desc = _MOOD_PROMPTS.get(self._last_mood, "")
        return (
            f"Duygu durumu: {self._last_mood}\n"
            f"Son kontrol: {age_min} dk oence\n"
            f"{self._last_summary}\n"
            f"{mood_desc}"
        ).strip()

    async def _fetch_rss(self) -> list[dict]:
        """RSS beslemesini cek. Oence feedparser dene, yoksa requests + regex."""
        # feedparser ile dene
        try:
            import feedparser
            feed = feedparser.parse(_RSS_URL)
            if feed.entries:
                logger.info(f"feedparser ile {len(feed.entries)} haber alindi.")
                return [
                    {"title": e.get("title", ""), "description": e.get("summary", "")}
                    for e in feed.entries
                ]
        except ImportError:
            logger.debug("feedparser yueklue degil, requests fallback kullaniliyor.")
        except Exception as exc:
            logger.warning(f"feedparser hatasi: {exc}")

        # requests + basit XML parse fallback
        try:
            import requests
            resp = requests.get(_RSS_URL, timeout=15, headers={
                "User-Agent": "AIMOS-FootballObserver/1.0"
            })
            resp.raise_for_status()
            items = _parse_feed_xml(resp.text)
            logger.info(f"requests+regex ile {len(items)} haber alindi.")
            return items
        except ImportError:
            logger.error("Ne feedparser ne de requests yueklue — RSS alinamaz.")
            return []
        except Exception as exc:
            logger.warning(f"RSS fetch hatasi: {exc}")
            return []
