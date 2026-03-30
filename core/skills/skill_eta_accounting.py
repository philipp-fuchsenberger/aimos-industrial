"""
ETAAccountingBase – ETA Muhasebe entegrasyonu icin temel skill sinifi.

ETA muhasebe yazilimina salt-okunur (readonly) erisim saglar.
SQL sorgulari mapping.json dosyasindan yuklenir — kod degisikligi gerekmez.

Guvenlik:
  - Sadece SELECT sorgulari calistirilir (INSERT/UPDATE/DELETE engellenir).
  - Baglanti zaman asimi: 10 saniye.
  - Sonuclar maksimum 50 satir ile sinirlidir.
  - Hassas veriler (tutarlar, musteri adlari) yerel kalir — harici API'ye gonderilmez.

Alt siniflar (Firebird / MSSQL) _connect() metodunu implemente eder.
"""

import json
import logging
import os
import re
from abc import abstractmethod
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("ETAAccountingSkill")

_CONNECT_TIMEOUT = 10  # saniye
_MAX_ROWS = 50
_DEFAULT_MAPPING = Path(__file__).parent / "eta_mapping.json"

# SQL guvenlik: sadece SELECT izin verilir
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


class ETAAccountingBase(BaseSkill):
    """ETA muhasebe entegrasyonu icin temel sinif. Alt siniflar DB baglantisi saglar."""

    name = "eta_accounting"
    display_name = "ETA Accounting"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None,
                 secrets: dict[str, str] | None = None, **kwargs):
        self._init_secrets(secrets)
        self._agent_name = agent_name
        self._host = self._secret("ETA_DB_HOST").strip()
        self._port = int(self._secret("ETA_DB_PORT", str(self._default_port())).strip() or self._default_port())
        self._db_name = self._secret("ETA_DB_NAME").strip()
        self._user = self._secret("ETA_DB_USER").strip()
        self._password = self._secret("ETA_DB_PASSWORD").strip()
        self._mapping = self._load_mapping(agent_name)

    def _default_port(self) -> int:
        """Alt siniflar varsayilan portu override eder."""
        return 0

    def _load_mapping(self, agent_name: str) -> dict:
        """SQL mapping dosyasini yukle. Oncelik: agent workspace, sonra varsayilan."""
        # Agent workspace'te ozel mapping var mi?
        if agent_name:
            try:
                ws = self.workspace_path(agent_name)
                custom = ws / "eta_mapping.json"
                if custom.is_file():
                    data = json.loads(custom.read_text(encoding="utf-8"))
                    logger.info(f"Ozel mapping yuklendi: {custom}")
                    return data
            except Exception as exc:
                logger.warning(f"Ozel mapping okunamadi: {exc}")

        # Varsayilan mapping
        try:
            return json.loads(_DEFAULT_MAPPING.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"Varsayilan mapping okunamadi: {exc}")
            return {}

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "ETA_DB_HOST",
                "label": "Veritabani sunucusu (Tailscale IP)",
                "type": "text",
                "placeholder": "100.64.1.5",
                "hint": "z.B. 100.64.1.5 — Tailscale uygulamasindan bulabilirsiniz",
                "secret": False,
            },
            {
                "key": "ETA_DB_PORT",
                "label": "Veritabani portu",
                "type": "text",
                "placeholder": "",
                "hint": "Firebird: 3050, MSSQL: 1433",
                "secret": False,
            },
            {
                "key": "ETA_DB_NAME",
                "label": "Veritabani adi",
                "type": "text",
                "placeholder": "ETA_DB",
                "hint": "ETA veritabani adi veya dosya yolu (Firebird icin tam yol)",
                "secret": False,
            },
            {
                "key": "ETA_DB_USER",
                "label": "Kullanici adi (sadece okuma yetkisi!)",
                "type": "text",
                "placeholder": "READONLY_USER",
                "hint": "Guvenlik icin sadece SELECT yetkisi olan bir kullanici kullanin",
                "secret": False,
            },
            {
                "key": "ETA_DB_PASSWORD",
                "label": "Sifre",
                "type": "password",
                "placeholder": "",
                "hint": "Veritabani sifresi",
                "secret": True,
            },
        ]

    def is_available(self) -> bool:
        return bool(self._host and self._db_name and self._user)

    @abstractmethod
    def _connect(self):
        """Veritabanina baglanir ve bir connection nesnesi dondurur.

        Alt siniflar (Firebird / MSSQL) bunu implemente eder.
        Baglanti zaman asimi _CONNECT_TIMEOUT saniye olmalidir.
        """

    def _validate_sql(self, sql: str) -> None:
        """SQL sorgusunun sadece SELECT oldugunu dogrular."""
        stripped = sql.strip()
        if not stripped.upper().startswith("SELECT"):
            raise ValueError("Guvenlik hatasi: Sadece SELECT sorgulari calistirabilirsiniz.")
        if _FORBIDDEN_SQL.search(stripped):
            raise ValueError("Guvenlik hatasi: Yasakli SQL komutu tespit edildi.")

    def _execute_query(self, query_key: str, params: list) -> list[tuple]:
        """Mapping'ten SQL sorgusunu calistir ve sonuclari dondur."""
        if query_key not in self._mapping:
            raise ValueError(f"Bilinmeyen sorgu: '{query_key}'. Mapping dosyasini kontrol edin.")

        entry = self._mapping[query_key]
        sql = entry["sql"]
        self._validate_sql(sql)

        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchmany(_MAX_ROWS)
            # Sutun adlarini al
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            cursor.close()
            return columns, rows
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── Tool Tanimlari ────────────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "get_customer_balance",
                "description": "Musteri hesap bakiyesini sorgular. Musteri adina gore arama yapar.",
                "parameters": {
                    "customer_name": {
                        "type": "string",
                        "description": "Musteri adi (kismi arama desteklenir)",
                        "required": True,
                    },
                },
            },
            {
                "name": "list_unpaid_invoices",
                "description": "Vadesi gecmis odenmemis faturalari listeler.",
                "parameters": {
                    "days_overdue": {
                        "type": "integer",
                        "description": "Kac gunden fazla vadesi gecmis (varsayilan: 30)",
                        "required": False,
                    },
                },
            },
            {
                "name": "search_transactions",
                "description": "Islem hareketlerini anahtar kelime ve tarih araligina gore arar.",
                "parameters": {
                    "query": {
                        "type": "string",
                        "description": "Aranacak kelime (aciklama alaninda)",
                        "required": True,
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Baslangic tarihi (YYYY-MM-DD, bos = 1 ay once)",
                        "required": False,
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Bitis tarihi (YYYY-MM-DD, bos = bugun)",
                        "required": False,
                    },
                },
            },
            {
                "name": "get_daily_summary",
                "description": "Bugunun islem ozetini getirir (proaktif gunluk rapor icin).",
                "parameters": {},
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "get_customer_balance":
            return self._get_customer_balance(arguments.get("customer_name", ""))
        elif tool_name == "list_unpaid_invoices":
            return self._list_unpaid_invoices(arguments.get("days_overdue", 30))
        elif tool_name == "search_transactions":
            return self._search_transactions(
                arguments.get("query", ""),
                arguments.get("date_from", ""),
                arguments.get("date_to", ""),
            )
        elif tool_name == "get_daily_summary":
            return self._get_daily_summary()
        return f"Bilinmeyen arac: {tool_name}"

    # ── Tool Implementasyonlari ───────────────────────────────────────────────

    def _get_customer_balance(self, customer_name: str) -> str:
        if not self.is_available():
            return "ETA baglantisi yapilandirilmamis. Lutfen Dashboard'tan ayarlari girin."
        if not customer_name:
            return "Hata: Musteri adi bos olamaz."

        try:
            columns, rows = self._execute_query(
                "customer_balance",
                [f"%{customer_name}%"],
            )
        except Exception as exc:
            logger.error(f"[eta] musteri bakiye hatasi: {exc}")
            return f"Veritabani hatasi: {exc}"

        if not rows:
            return f"'{customer_name}' icin musteri kaydi bulunamadi."

        lines = [f"Musteri bakiye sonuclari ('{customer_name}'):"]
        lines.append("-" * 50)
        for row in rows:
            row_dict = dict(zip(columns, row))
            kod = row_dict.get("CARI_KOD", "?")
            isim = row_dict.get("CARI_ISIM", "?")
            borc = row_dict.get("BORC", 0)
            alacak = row_dict.get("ALACAK", 0)
            bakiye = row_dict.get("BAKIYE", 0)
            lines.append(f"  {kod} — {isim}")
            lines.append(f"    Borc: {borc:,.2f} TL | Alacak: {alacak:,.2f} TL | Bakiye: {bakiye:,.2f} TL")

        if len(rows) >= _MAX_ROWS:
            lines.append(f"\n(Maksimum {_MAX_ROWS} sonuc gosteriliyor, daha fazla olabilir)")
        return "\n".join(lines)

    def _list_unpaid_invoices(self, days_overdue: int = 30) -> str:
        if not self.is_available():
            return "ETA baglantisi yapilandirilmamis. Lutfen Dashboard'tan ayarlari girin."

        try:
            days_overdue = int(days_overdue)
        except (TypeError, ValueError):
            days_overdue = 30

        cutoff = date.today() - timedelta(days=days_overdue)

        try:
            columns, rows = self._execute_query(
                "unpaid_invoices",
                [cutoff.isoformat()],
            )
        except Exception as exc:
            logger.error(f"[eta] odenmemis fatura hatasi: {exc}")
            return f"Veritabani hatasi: {exc}"

        if not rows:
            return f"Son {days_overdue} gunde vadesi gecmis odenmemis fatura bulunamadi."

        lines = [f"Vadesi {days_overdue}+ gun gecmis odenmemis faturalar:"]
        lines.append("-" * 50)
        toplam = 0
        for row in rows:
            row_dict = dict(zip(columns, row))
            fatura_no = row_dict.get("FATURA_NO", "?")
            musteri = row_dict.get("MUSTERI", "?")
            tutar = row_dict.get("TUTAR", 0)
            vade = row_dict.get("VADE_TARIH", "?")
            toplam += float(tutar or 0)
            lines.append(f"  Fatura: {fatura_no} | {musteri} | {tutar:,.2f} TL | Vade: {vade}")

        lines.append("-" * 50)
        lines.append(f"Toplam: {toplam:,.2f} TL ({len(rows)} fatura)")

        if len(rows) >= _MAX_ROWS:
            lines.append(f"(Maksimum {_MAX_ROWS} sonuc gosteriliyor, daha fazla olabilir)")
        return "\n".join(lines)

    def _search_transactions(self, query: str, date_from: str = "", date_to: str = "") -> str:
        if not self.is_available():
            return "ETA baglantisi yapilandirilmamis. Lutfen Dashboard'tan ayarlari girin."
        if not query:
            return "Hata: Arama kelimesi bos olamaz."

        today = date.today()
        if not date_from:
            date_from = (today - timedelta(days=30)).isoformat()
        if not date_to:
            date_to = today.isoformat()

        try:
            columns, rows = self._execute_query(
                "search_transactions",
                [f"%{query}%", date_from, date_to],
            )
        except Exception as exc:
            logger.error(f"[eta] islem arama hatasi: {exc}")
            return f"Veritabani hatasi: {exc}"

        if not rows:
            return f"'{query}' icin {date_from} — {date_to} tarih araliginda islem bulunamadi."

        lines = [f"Islem arama sonuclari ('{query}', {date_from} — {date_to}):"]
        lines.append("-" * 60)
        for row in rows:
            row_dict = dict(zip(columns, row))
            tarih = row_dict.get("TARIH", "?")
            aciklama = row_dict.get("ACIKLAMA", "?")
            tutar = row_dict.get("TUTAR", 0)
            tip = row_dict.get("TIP", "?")
            lines.append(f"  {tarih} | {tip} | {tutar:,.2f} TL | {aciklama}")

        if len(rows) >= _MAX_ROWS:
            lines.append(f"\n(Maksimum {_MAX_ROWS} sonuc gosteriliyor, daha fazla olabilir)")
        return "\n".join(lines)

    def _get_daily_summary(self) -> str:
        if not self.is_available():
            return "ETA baglantisi yapilandirilmamis. Lutfen Dashboard'tan ayarlari girin."

        today = date.today().isoformat()

        try:
            columns, rows = self._execute_query("daily_summary", [today])
        except Exception as exc:
            logger.error(f"[eta] gunluk ozet hatasi: {exc}")
            return f"Veritabani hatasi: {exc}"

        if not rows:
            return f"Bugun ({today}) icin islem kaydi bulunamadi."

        lines = [f"Gunluk islem ozeti ({today}):"]
        lines.append("-" * 40)
        genel_toplam = 0
        genel_adet = 0
        for row in rows:
            row_dict = dict(zip(columns, row))
            tip = row_dict.get("TIP", "?")
            adet = row_dict.get("ADET", 0)
            toplam = row_dict.get("TOPLAM", 0)
            genel_toplam += float(toplam or 0)
            genel_adet += int(adet or 0)
            lines.append(f"  {tip}: {adet} islem, toplam {toplam:,.2f} TL")

        lines.append("-" * 40)
        lines.append(f"Genel: {genel_adet} islem, {genel_toplam:,.2f} TL")
        return "\n".join(lines)
