"""
MailMonitorSkill – POP3-basiertes Mitlesen von User-E-Mails (read-only).

Der Agent ruft E-Mails des Users per POP3 ab, OHNE den Gelesen-Status zu aendern.
Jede Mail wird nur einmal abgerufen (UIDL-Tracking in memory.db).
Gespeicherte Mails liegen im Workspace unter mailbox/ als Textdateien.

POP3 vs IMAP:
  POP3 hat kein Read/Unread-Flag — der Abruf veraendert nichts am Mailserver.
  UIDL (Unique ID Listing) identifiziert jede Mail eindeutig.
  Wir speichern abgerufene UIDs in memory.db/skill_state → kein doppelter Abruf.

Credentials (agent-editable empfohlen):
  POP3_HOST     — POP3-Server (z.B. pop.example.com)
  POP3_PORT     — Port (default: 995 = SSL)
  POP3_USER     — Login (meist die E-Mail-Adresse)
  POP3_PASSWORD  — Passwort / App-Passwort

Aktivierung:
  Skill 'mail_monitor' in der Agent-Konfiguration + Credentials im Wizard oder via Chat.
"""

import email as email_lib
import email.header
import email.policy
import hashlib
import json
import logging
import os
import poplib
import re
import sqlite3
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("MailMonitorSkill")

_MAX_FETCH_PER_RUN = 20  # max emails per fetch cycle
_MAX_BODY_STORE = 50_000  # max chars stored per email body


def _decode_header(raw: str) -> str:
    """Decode RFC2047 encoded header."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


class MailMonitorSkill(BaseSkill):
    """POP3-basiertes E-Mail-Monitoring — liest User-Mails ohne Gelesen-Status zu aendern."""

    name = "mail_monitor"
    display_name = "Mail Monitor (POP3 read-only)"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None,
                 secrets: dict[str, str] | None = None, **kwargs):
        self._init_secrets(secrets)
        self._agent_name = agent_name
        self._mailbox_dir: Path | None = None
        if agent_name:
            self._mailbox_dir = self.workspace_path(agent_name) / "mailbox"
            self._mailbox_dir.mkdir(exist_ok=True)

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {"key": "POP3_HOST", "label": "POP3 Server", "type": "text",
             "placeholder": "pop.example.com", "hint": "POP3-Server des Users (SSL, Port 995)", "secret": True},
            {"key": "POP3_USER", "label": "POP3 User (Email)", "type": "text",
             "placeholder": "user@example.com", "hint": "Login-Name (meist die E-Mail-Adresse)", "secret": True},
            {"key": "POP3_PASSWORD", "label": "POP3 Password", "type": "password",
             "placeholder": "", "hint": "App-Passwort empfohlen", "secret": True},
        ]

    def is_available(self) -> bool:
        return bool(self._secret("POP3_HOST") and self._secret("POP3_USER") and self._secret("POP3_PASSWORD"))

    def _get_known_uids(self) -> set[str]:
        """Load already-fetched UIDs from memory.db/skill_state."""
        db_path = self.memory_db_path(self._agent_name)
        if not db_path.exists():
            return set()
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            row = conn.execute(
                "SELECT value FROM skill_state WHERE skill_name='mail_monitor' AND key='fetched_uids'"
            ).fetchone()
            conn.close()
            if row:
                return set(json.loads(row[0]))
        except Exception:
            pass
        return set()

    def _save_known_uids(self, uids: set[str]):
        """Persist fetched UIDs to memory.db/skill_state."""
        db_path = self.memory_db_path(self._agent_name)
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute(
                "INSERT OR REPLACE INTO skill_state (skill_name, key, value, updated_at) "
                "VALUES ('mail_monitor', 'fetched_uids', ?, datetime('now'))",
                (json.dumps(sorted(uids)),),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"Failed to save UIDs: {exc}")

    def _save_mail_index(self, mail_id: str, meta: dict):
        """Save mail metadata to memory.db for search."""
        db_path = self.memory_db_path(self._agent_name)
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mail_index (
                    mail_id    TEXT PRIMARY KEY,
                    sender     TEXT,
                    subject    TEXT,
                    date       TEXT,
                    body_preview TEXT,
                    file_path  TEXT,
                    fetched_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO mail_index (mail_id, sender, subject, date, body_preview, file_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mail_id, meta.get("from", ""), meta.get("subject", ""),
                 meta.get("date", ""), meta.get("body", "")[:200], meta.get("file_path", "")),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"Failed to index mail: {exc}")

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "fetch_user_mail",
                "description": (
                    "Ruft neue E-Mails des Users per POP3 ab und speichert sie lokal. "
                    "Aendert NICHT den Gelesen-Status. Jede Mail wird nur einmal abgerufen."
                ),
                "parameters": {},
            },
            {
                "name": "search_mail",
                "description": "Durchsucht die lokal gespeicherten User-Mails nach Stichwort in Betreff, Absender oder Text.",
                "parameters": {
                    "query": {"type": "string", "description": "Suchbegriff", "required": True},
                },
            },
            {
                "name": "read_mail",
                "description": "Liest den vollstaendigen Inhalt einer gespeicherten Mail (nach ID aus search_mail).",
                "parameters": {
                    "mail_id": {"type": "string", "description": "Mail-ID aus search_mail", "required": True},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        import asyncio
        if tool_name == "fetch_user_mail":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._fetch_pop3)
        elif tool_name == "search_mail":
            return self._search_mail(arguments.get("query", ""))
        elif tool_name == "read_mail":
            return self._read_mail(arguments.get("mail_id", ""))
        return f"Unbekanntes Tool: {tool_name}"

    def _fetch_pop3(self) -> str:
        """Fetch new emails via POP3 SSL. Does NOT delete or mark as read."""
        host = self._secret("POP3_HOST")
        port = int(self._secret("POP3_PORT", "995"))
        user = self._secret("POP3_USER")
        passwd = self._secret("POP3_PASSWORD")

        if not host or not user or not passwd:
            return "Fehler: POP3-Zugangsdaten nicht konfiguriert (POP3_HOST, POP3_USER, POP3_PASSWORD)."

        known_uids = self._get_known_uids()
        new_count = 0

        tls_ctx = ssl.create_default_context()
        tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        try:
            pop = poplib.POP3_SSL(host, port, context=tls_ctx, timeout=30)
            pop.user(user)
            pop.pass_(passwd)

            # Get UIDL list (unique IDs for dedup)
            resp, uid_list, _ = pop.uidl()
            uid_map = {}  # msg_num → uid
            for entry in uid_list:
                if isinstance(entry, bytes):
                    entry = entry.decode("utf-8", errors="replace")
                parts = entry.strip().split(None, 1)
                if len(parts) == 2:
                    uid_map[int(parts[0])] = parts[1]

            # Fetch only NEW messages (not in known_uids)
            to_fetch = [(num, uid) for num, uid in uid_map.items() if uid not in known_uids]
            to_fetch = to_fetch[-_MAX_FETCH_PER_RUN:]  # limit per run

            for msg_num, uid in to_fetch:
                try:
                    resp, lines, _ = pop.retr(msg_num)
                    raw = b"\r\n".join(lines)
                    msg = email_lib.message_from_bytes(raw, policy=email_lib.policy.default)

                    subject = _decode_header(msg.get("Subject", ""))
                    sender = _decode_header(msg.get("From", ""))
                    date = msg.get("Date", "")

                    # Extract body
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    charset = part.get_content_charset() or "utf-8"
                                    body = payload.decode(charset, errors="replace")
                                    break
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            charset = msg.get_content_charset() or "utf-8"
                            body = payload.decode(charset, errors="replace")

                    body = body[:_MAX_BODY_STORE]

                    # Generate safe filename from UID
                    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", uid)[:60]
                    mail_file = self._mailbox_dir / f"{safe_id}.txt"
                    mail_file.write_text(
                        f"UID: {uid}\n"
                        f"Von: {sender}\n"
                        f"Betreff: {subject}\n"
                        f"Datum: {date}\n"
                        f"{'=' * 40}\n"
                        f"{body}\n",
                        encoding="utf-8",
                    )

                    # Index for search
                    self._save_mail_index(uid, {
                        "from": sender, "subject": subject,
                        "date": date, "body": body, "file_path": str(mail_file),
                    })

                    known_uids.add(uid)
                    new_count += 1
                    logger.info(f"[MailMonitor] Fetched: {subject[:50]} from {sender[:30]}")

                except Exception as exc:
                    logger.warning(f"[MailMonitor] Failed to fetch msg #{msg_num}: {exc}")

            # Do NOT call pop.dele() — we only read, never delete
            pop.quit()

        except Exception as exc:
            logger.error(f"[MailMonitor] POP3 error: {exc}")
            return f"POP3-Fehler: {exc}"

        # Persist known UIDs
        self._save_known_uids(known_uids)

        total = len(known_uids)
        if new_count:
            return f"{new_count} neue Mail(s) abgerufen und gespeichert. Gesamt: {total} Mails im lokalen Archiv."
        return f"Keine neuen Mails. {total} Mails bereits im lokalen Archiv."

    def _search_mail(self, query: str) -> str:
        if not query:
            return "Fehler: 'query' ist ein Pflichtfeld."
        db_path = self.memory_db_path(self._agent_name)
        if not db_path.exists():
            return "Kein Mail-Archiv vorhanden. Zuerst fetch_user_mail aufrufen."
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            q = f"%{query.strip()}%"
            rows = conn.execute(
                "SELECT mail_id, sender, subject, date, body_preview FROM mail_index "
                "WHERE sender LIKE ? OR subject LIKE ? OR body_preview LIKE ? "
                "ORDER BY fetched_at DESC LIMIT 15",
                (q, q, q),
            ).fetchall()
            conn.close()
            if not rows:
                return f"Keine Mails gefunden fuer '{query}'."
            lines = []
            for mid, sender, subject, date, preview in rows:
                lines.append(f"  ID: {mid}\n    Von: {sender}\n    Betreff: {subject}\n    Datum: {date}")
            return f"Gefunden: {len(rows)} Mail(s):\n" + "\n".join(lines)
        except Exception as exc:
            return f"Fehler: {exc}"

    def _read_mail(self, mail_id: str) -> str:
        if not mail_id:
            return "Fehler: 'mail_id' ist ein Pflichtfeld."
        # Find file path from index
        db_path = self.memory_db_path(self._agent_name)
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            row = conn.execute(
                "SELECT file_path FROM mail_index WHERE mail_id=?", (mail_id.strip(),)
            ).fetchone()
            conn.close()
            if not row:
                return f"Mail nicht gefunden: {mail_id}"
            fpath = Path(row[0])
            if not fpath.is_file():
                return f"Mail-Datei nicht mehr vorhanden: {fpath.name}"
            content = fpath.read_text(encoding="utf-8", errors="replace")
            if len(content) > 10_000:
                content = content[:10_000] + "\n...(gekuerzt)"
            return content
        except Exception as exc:
            return f"Fehler: {exc}"
