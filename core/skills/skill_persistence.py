"""
PersistenceSkill – Hartnäckige Nachverfolgung / Israrcı Takip.

Açık kalan sorular ve talepleri takip eder, kullanıcı yanıt vermezse
otomatik hatırlatma oluşturur.

Tools:
  track_open_request(topic, deadline_hours)  — Açık bir talebi kaydeder, hatırlatma kurar.
  list_open_requests()                       — Tüm açık talepleri listeler.
  resolve_request(topic)                     — Bir talebi çözüldü olarak işaretler.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("PersistenceSkill")


class PersistenceSkill(BaseSkill):
    """Açık talepleri takip eder ve hatırlatma mesajları üretir."""

    name = "persistence"
    display_name = "Follow-up Tracking"

    def __init__(self, agent_name: str = "", config: dict | None = None, **kwargs):
        self._agent_name = agent_name
        self._config = config or {}
        self._db_path = self.workspace_path(agent_name) / "persistence.db" if agent_name else None
        if self._db_path:
            self._init_db()

    def _init_db(self) -> None:
        """SQLite tablosunu oluşturur (yoksa)."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                deadline_hours REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _default_deadline_hours(self) -> float:
        """Yapılandırmadan varsayılan hatırlatma süresini okur."""
        try:
            return float(self._config.get("DEFAULT_DEADLINE_HOURS", "2"))
        except (ValueError, TypeError):
            return 2.0

    def is_available(self) -> bool:
        return bool(self._agent_name)

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "DEFAULT_DEADLINE_HOURS",
                "label": "Varsayılan Hatırlatma Süresi",
                "type": "text",
                "placeholder": "2",
                "hint": "Varsayılan hatırlatma süresi (saat)",
                "secret": False,
            },
        ]

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "track_open_request",
                "description": (
                    "Açık bir talebi kaydeder ve belirtilen süre sonra hatırlatma kurar. "
                    "Kullanıcı yanıt vermezse ajan otomatik olarak uyarılır."
                ),
                "parameters": {
                    "topic": {
                        "type": "string",
                        "description": "Takip edilecek konu veya soru",
                        "required": True,
                    },
                    "deadline_hours": {
                        "type": "number",
                        "description": "Hatırlatma süresi (saat). Belirtilmezse varsayılan kullanılır.",
                        "required": False,
                    },
                },
            },
            {
                "name": "list_open_requests",
                "description": "Tüm açık talepleri ve son tarihlerini listeler.",
                "parameters": {},
            },
            {
                "name": "resolve_request",
                "description": "Bir talebi çözüldü olarak işaretler (kullanıcı yanıt verdi).",
                "parameters": {
                    "topic": {
                        "type": "string",
                        "description": "Çözülen konunun adı veya ID'si",
                        "required": True,
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "track_open_request":
            return self._track_open_request(
                topic=arguments.get("topic", ""),
                deadline_hours=arguments.get("deadline_hours"),
            )
        elif tool_name == "list_open_requests":
            return self._list_open_requests()
        elif tool_name == "resolve_request":
            return self._resolve_request(topic=arguments.get("topic", ""))
        return f"[Hata: Bilinmeyen araç '{tool_name}']"

    def _track_open_request(self, topic: str, deadline_hours: float | None = None) -> str:
        if not topic:
            return "Hata: 'topic' alanı zorunludur."

        if not self._db_path:
            return "Hata: Veritabanı yolu yapılandırılmadı."

        hours = deadline_hours if deadline_hours is not None else self._default_deadline_hours()
        now = datetime.now(timezone.utc)
        deadline_at = now + timedelta(hours=hours)

        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT INTO open_requests (topic, created_at, deadline_at, deadline_hours, status) "
            "VALUES (?, ?, ?, ?, 'open')",
            (topic, now.isoformat(), deadline_at.isoformat(), hours),
        )
        conn.commit()
        conn.close()

        deadline_str = deadline_at.strftime("%Y-%m-%d %H:%M UTC")
        reminder_text = f"Abi, {topic} hakkında cevap vermedin, hayırdır?"

        # Süreyi insan-okunabilir formata çevir
        if hours < 1:
            when_str = f"in {int(hours * 60)}m"
        elif hours == int(hours):
            when_str = f"in {int(hours)}h"
        else:
            when_str = f"in {int(hours * 60)}m"

        logger.info(
            f"[Persistence] '{self._agent_name}' için açık talep kaydedildi: "
            f"{topic} (son tarih: {deadline_str})"
        )

        return (
            f"Açık talep kaydedildi: \"{topic}\"\n"
            f"Son tarih: {deadline_str}\n\n"
            f"Lütfen set_reminder ile {when_str} ({when_str}) sonra hatırlatma kur: "
            f"'{reminder_text}'"
        )

    def _list_open_requests(self) -> str:
        if not self._db_path:
            return "Hata: Veritabanı yolu yapılandırılmadı."

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, topic, created_at, deadline_at, status, resolved_at "
            "FROM open_requests ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()

        if not rows:
            return "Açık talep bulunmuyor."

        lines = [f"Açık talepler ({self._agent_name}):"]
        for r in rows:
            status_icon = "✅" if r["status"] == "resolved" else "⏳"
            deadline = r["deadline_at"][:16].replace("T", " ")
            extra = ""
            if r["resolved_at"]:
                resolved = r["resolved_at"][:16].replace("T", " ")
                extra = f" — çözüldü: {resolved}"
            lines.append(
                f"  {status_icon} #{r['id']} [{r['status']}] {r['topic']} "
                f"(son tarih: {deadline}){extra}"
            )

        return "\n".join(lines)

    def _resolve_request(self, topic: str) -> str:
        if not topic:
            return "Hata: 'topic' alanı zorunludur."

        if not self._db_path:
            return "Hata: Veritabanı yolu yapılandırılmadı."

        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(self._db_path))

        # Önce ID ile eşleşmeyi dene
        cursor = None
        try:
            topic_id = int(topic)
            cursor = conn.execute(
                "UPDATE open_requests SET status='resolved', resolved_at=? "
                "WHERE id=? AND status='open'",
                (now.isoformat(), topic_id),
            )
        except ValueError:
            pass

        # ID ile bulunamadıysa, konu adıyla ara
        if cursor is None or cursor.rowcount == 0:
            cursor = conn.execute(
                "UPDATE open_requests SET status='resolved', resolved_at=? "
                "WHERE topic LIKE ? AND status='open'",
                (now.isoformat(), f"%{topic}%"),
            )

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        if affected == 0:
            return f"'{topic}' ile eşleşen açık talep bulunamadı."

        return f"{affected} talep çözüldü olarak işaretlendi: \"{topic}\""
