"""
EmailSkill – Sichere E-Mail-Integration (SMTP/IMAP) fuer AIMOS-Agenten.

Sicherheit:
  - IMAP: Port 993, SSL/TLS, mindestens TLS 1.2
  - SMTP: Port 587 STARTTLS (primaer), Port 465 implicit SSL (Fallback)
  - TLS < 1.2 explizit verboten (OP_NO_TLSv1 | OP_NO_TLSv1_1)
  - FROM-Adresse = EMAIL_ADDRESS (verhindert GMX-Ablehnungen)

Credentials (in env_secrets):
  EMAIL_ADDRESS     – E-Mail-Adresse (= Login UND Absender)
  EMAIL_PASSWORD    – Passwort / App-Passwort
  EMAIL_IMAP_HOST   – IMAP-Server (default: imap.gmx.net)
  EMAIL_IMAP_PORT   – IMAP-Port (default: 993)
  EMAIL_SMTP_HOST   – SMTP-Server (default: mail.gmx.net)
  EMAIL_SMTP_PORT   – SMTP-Port (default: 587, Fallback: 465)

Aktivierung:
  Skill 'email' in der Agent-Konfiguration listen.
"""

import email
import email.header
import email.mime.multipart
import email.mime.text
import email.mime.base
import email.encoders
import imaplib
import logging
import os
import smtplib
import ssl
from pathlib import Path
from typing import Optional

from .base import BaseSkill

logger = logging.getLogger("EmailSkill")


# ── TLS Context Factory ─────────────────────────────────────────────────────

def _create_tls_context() -> ssl.SSLContext:
    """Create a hardened SSL context that enforces TLS >= 1.2.

    Uses minimum_version (the modern Python 3.10+ API) instead of the
    deprecated OP_NO_TLSv1 flags. Prefers TLS 1.3 when available.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class EmailSkill(BaseSkill):
    """Sichere E-Mail-Integration (SMTP/IMAP) mit TLS >= 1.2."""

    name = "email"
    display_name = "E-Mail (IMAP/SMTP)"

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {"key": "EMAIL_ADDRESS", "label": "Email Address", "type": "email",
             "placeholder": "agent@example.com", "hint": "Login and sender address", "secret": True},
            {"key": "EMAIL_PASSWORD", "label": "Email Password", "type": "password",
             "placeholder": "", "hint": "App password recommended", "secret": True},
            {"key": "EMAIL_IMAP_HOST", "label": "IMAP Host", "type": "text",
             "placeholder": "imap.gmx.net", "hint": "IMAP server (SSL, port 993)", "secret": True},
            {"key": "EMAIL_SMTP_HOST", "label": "SMTP Host", "type": "text",
             "placeholder": "mail.gmx.net", "hint": "SMTP server (STARTTLS, port 587)", "secret": True},
        ]

    def __init__(self, agent_name: str = "", config: dict | None = None,
                 secrets: dict[str, str] | None = None, **kwargs) -> None:
        self._init_secrets(secrets)
        self._agent_name = agent_name
        self._config = config or {}
        self._workspace = self.workspace_path(agent_name) if agent_name else Path("storage/agents/_default")

    def is_available(self) -> bool:
        return bool(self._secret("EMAIL_ADDRESS"))

    def _credentials_complete(self) -> tuple[bool, str]:
        required = {
            "EMAIL_ADDRESS": self._secret("EMAIL_ADDRESS"),
            "EMAIL_PASSWORD": self._secret("EMAIL_PASSWORD"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            return False, (
                f"Email credentials missing: {', '.join(missing)}. "
                f"Please configure in the dashboard wizard under 'Email Settings'."
            )
        return True, ""

    async def enrich_context(self, user_text: str) -> str:
        ok, msg = self._credentials_complete()
        if not ok:
            return (
                f"[Email-Skill]\n"
                f"WARNING: {msg}\n"
                f"Tell the user to configure email settings in the dashboard.\n"
            )
        addr = self._secret("EMAIL_ADDRESS")
        imap = self._secret("EMAIL_IMAP_HOST")
        smtp = self._secret("EMAIL_SMTP_HOST")
        return (
            f"[Email-Skill]\n"
            f"Email account: {addr}\n"
            f"IMAP: {imap}:993 (SSL) | SMTP: {smtp}:587 (STARTTLS)\n"
            f"You can read and send emails.\n"
        )

    # ── Tool Definitions (fuer LLM Tool-Calling) ───────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "read_emails",
                "description": "Liest E-Mails aus dem Posteingang. Gibt Betreff, Absender, Datum und Text zurueck.",
                "parameters": {
                    "folder":    {"type": "string",  "description": "IMAP-Ordner", "default": "INBOX"},
                    "limit":     {"type": "integer", "description": "Max. Anzahl E-Mails", "default": 5},
                    "unread_only": {"type": "boolean", "description": "Nur ungelesene", "default": True},
                },
            },
            {
                "name": "send_email",
                "description": "Sendet eine E-Mail, optional mit Dateianhang aus dem Workspace.",
                "parameters": {
                    "to":          {"type": "string", "description": "Empfaenger-Adresse", "required": True},
                    "subject":     {"type": "string", "description": "Betreff", "required": True},
                    "body":        {"type": "string", "description": "Nachrichtentext", "required": True},
                    "attachments": {"type": "string", "description": "Dateinamen aus dem Workspace, kommagetrennt (optional)", "default": ""},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        logger.info(f"[EMAIL] execute_tool: {tool_name}({arguments})")
        if tool_name == "read_emails":
            results = self.read_emails(
                folder=arguments.get("folder", "INBOX"),
                limit=arguments.get("limit", 5),
                unread_only=arguments.get("unread_only", True),
            )
            if not results:
                return "No emails found."
            if results and "error" in results[0]:
                return f"Error: {results[0]['error']}"
            lines = []
            for i, m in enumerate(results, 1):
                lines.append(f"--- Mail {i} ---")
                lines.append(f"Von: {m.get('from', '?')}")
                lines.append(f"Betreff: {m.get('subject', '?')}")
                lines.append(f"Datum: {m.get('date', '?')}")
                body = m.get('body', '')[:500]
                lines.append(f"Text: {body}")
                if m.get('attachments'):
                    lines.append(f"Anhaenge: {', '.join(m['attachments'])}")
            return "\n".join(lines)

        elif tool_name == "send_email":
            to = arguments.get("to", "")
            subject = arguments.get("subject", "")
            body = arguments.get("body", "")
            if not to or not subject or not body:
                return "Error: 'to', 'subject', and 'body' are required."
            # Block known undeliverable test domains — prevent SMTP bounces
            _test_domains = ["example.com", "example.org", "example.net", "test.com", "test.de"]
            _to_domain = to.split("@")[-1].lower() if "@" in to else ""
            if _to_domain in _test_domains:
                logger.info(f"[EMAIL] Simulated send to test address {to} (not actually sent)")
                return f"Email sent to {to}: '{subject}' (simulated — test address)"
            # CR-215: Email recipient allowlist — prevent unauthorized outbound emails
            allowlist = self._config.get("email_allowlist", [])
            if allowlist:
                import re as _re
                to_addrs = [a.strip() for a in to.split(",")]
                for addr in to_addrs:
                    email_match = _re.search(r'[\w.+-]+@[\w.-]+', addr)
                    addr_clean = email_match.group(0).lower() if email_match else addr.lower()
                    allowed = any(
                        addr_clean == a.lower() or addr_clean.endswith("@" + a.lower().lstrip("@"))
                        for a in allowlist
                    )
                    if not allowed:
                        logger.warning(f"[CR-215] Blocked email to {addr_clean} — not in allowlist")
                        return (f"Email to {addr_clean} blocked — not in allowlist. "
                                f"Allowed: {', '.join(allowlist)}. Ask the customer for a valid address.")
            # Parse attachments (comma-separated filenames from workspace)
            att_raw = arguments.get("attachments", "")
            att_list = [a.strip() for a in att_raw.split(",") if a.strip()] if att_raw else None
            # Append email signature on code level
            _sig = self._config.get("email_signature", "")
            if _sig:
                # Strip any LLM-generated mini-signature before appending the real one
                import re as _re_sig
                body = _re_sig.sub(
                    r'\n*(?:^.{0,60}(?:GmbH|Support|Service|Kundenservice|Kundendienst)\s*$)\s*$',
                    '', body, flags=_re_sig.IGNORECASE | _re_sig.MULTILINE
                ).rstrip()
                body = body.rstrip() + _sig
            result = self.send_email(to=to, subject=subject, body=body, attachments=att_list)
            if "error" in result:
                return f"Send failed: {result['error']}"
            att_info = f" + {len(att_list)} attachment(s)" if att_list else ""
            return f"Email sent to {to}: '{subject}'{att_info} (via {result.get('method', 'SMTP')})"

        return f"Unknown tool: {tool_name}"

    # ── IMAP (Empfang) ───────────────────────────────────────────────────────

    def read_emails(
        self,
        folder: str = "INBOX",
        limit: int = 10,
        unread_only: bool = True,
        save_attachments: bool = True,
    ) -> list[dict]:
        """Liest E-Mails via IMAP mit SSL (Port 993, TLS >= 1.2)."""
        ok, msg = self._credentials_complete()
        if not ok:
            return [{"error": msg}]

        addr = self._secret("EMAIL_ADDRESS")
        passwd = self._secret("EMAIL_PASSWORD")
        imap_host = self._secret("EMAIL_IMAP_HOST")
        imap_port = int(self._secret("EMAIL_IMAP_PORT", "993"))

        tls_ctx = _create_tls_context()
        results = []

        try:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=tls_ctx)

            # Log negotiated TLS version
            sock = conn.socket()
            if hasattr(sock, "version"):
                tls_ver = sock.version()
                logger.info(f"[IMAP] Connected via {tls_ver} to {imap_host}:{imap_port}")

            conn.login(addr, passwd)
            conn.select(folder, readonly=True)

            search_criteria = "UNSEEN" if unread_only else "ALL"
            _, msg_ids = conn.search(None, search_criteria)
            id_list = msg_ids[0].split()

            for msg_id in reversed(id_list[-limit:]):
                _, msg_data = conn.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg_obj = email.message_from_bytes(raw)

                subject = self._decode_header(msg_obj.get("Subject", ""))
                from_addr = self._decode_header(msg_obj.get("From", ""))
                date = msg_obj.get("Date", "")

                body = ""
                attachments = []

                if msg_obj.is_multipart():
                    for part in msg_obj.walk():
                        content_type = part.get_content_type()
                        disposition = str(part.get("Content-Disposition", ""))

                        if "attachment" in disposition:
                            filename = part.get_filename()
                            if filename and save_attachments:
                                att_path = self._save_attachment(
                                    self._decode_header(filename),
                                    part.get_payload(decode=True),
                                )
                                attachments.append(att_path)
                        elif content_type == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body = payload.decode(charset, errors="replace")
                        elif content_type == "text/html" and not body:
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body = f"[HTML] {payload.decode(charset, errors='replace')[:2000]}"
                else:
                    payload = msg_obj.get_payload(decode=True)
                    if payload:
                        charset = msg_obj.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")

                results.append({
                    "subject": subject,
                    "from": from_addr,
                    "date": date,
                    "body": body[:4000],
                    "attachments": attachments,
                })

            conn.logout()
            logger.info(f"E-Mails gelesen: {len(results)} aus {folder}")

        except Exception as exc:
            logger.error(f"IMAP fehlgeschlagen: {exc}")
            results.append({"error": f"IMAP error: {exc}"})

        return results

    # ── SMTP (Versand) ───────────────────────────────────────────────────────

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Optional[list[str]] = None,
        html: bool = False,
    ) -> dict:
        """Sendet E-Mail via SMTP.

        Primaer: Port 587 + STARTTLS.
        Fallback: Port 465 + implicit SSL.
        FROM-Header = EMAIL_ADDRESS (GMX erfordert Uebereinstimmung).
        """
        # CR-273: LLM sometimes double-quotes the body ("\"text\"")
        # Strip leading/trailing literal quotes that survived JSON parsing
        body = body.strip()
        if body.startswith('"') and body.endswith('"') and len(body) > 2:
            body = body[1:-1]
        elif body.startswith('"'):
            body = body[1:]
        # Also unescape common JSON artifacts in body text
        body = body.replace('\\n', '\n').replace('\\"', '"')

        # Reject suspiciously short bodies (truncated by JSON parse error)
        if len(body.strip()) < 20:
            logger.warning(f"send_email: body too short ({len(body)} chars), likely truncated")
            return {"error": f"Email body too short ({len(body)} chars) — likely a parsing error. Please regenerate the email text."}

        ok, msg_text = self._credentials_complete()
        if not ok:
            return {"error": msg_text}

        addr = self._secret("EMAIL_ADDRESS")
        passwd = self._secret("EMAIL_PASSWORD")
        smtp_host = self._secret("EMAIL_SMTP_HOST")
        smtp_port = int(self._secret("EMAIL_SMTP_PORT", "587"))

        # Build message — FROM must match EMAIL_ADDRESS exactly (GMX policy)
        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = addr
        msg["To"] = to
        msg["Subject"] = subject

        content_type = "html" if html else "plain"
        msg.attach(email.mime.text.MIMEText(body, content_type, "utf-8"))

        if attachments:
            for att_path in attachments:
                path = Path(att_path)
                if not path.is_absolute():
                    path = self._workspace / path
                if not path.exists():
                    logger.warning(f"Anhang nicht gefunden: {path}")
                    continue
                part = email.mime.base.MIMEBase("application", "octet-stream")
                part.set_payload(path.read_bytes())
                email.encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={path.name}")
                msg.attach(part)

        tls_ctx = _create_tls_context()
        recipients = [r.strip() for r in to.split(",")]
        err_587 = None

        # Attempt 1: Port 587 + STARTTLS (standard for GMX)
        try:
            return self._send_starttls(smtp_host, smtp_port, addr, passwd, recipients, msg, tls_ctx)
        except Exception as exc:
            err_587 = str(exc)
            logger.warning(f"SMTP STARTTLS :{smtp_port} fehlgeschlagen: {exc}")

        # Attempt 2: Port 465 + implicit SSL (fallback)
        try:
            logger.info("Fallback auf SMTP SSL :465")
            return self._send_ssl(smtp_host, 465, addr, passwd, recipients, msg, tls_ctx)
        except Exception as exc:
            logger.error(f"SMTP SSL :465 fehlgeschlagen: {exc}")
            return {"error": f"Send failed (587: {err_587} / 465: {exc})"}

    def _send_starttls(self, host, port, addr, passwd, recipients, msg, tls_ctx) -> dict:
        """SMTP via STARTTLS (Port 587)."""
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=tls_ctx)
            server.ehlo()
            # Log TLS version
            sock = server.sock
            if hasattr(sock, "version"):
                logger.info(f"[SMTP] Connected via {sock.version()} to {host}:{port}")
            server.login(addr, passwd)
            server.sendmail(addr, recipients, msg.as_string())
        logger.info(f"E-Mail gesendet via STARTTLS:{port}: {msg['Subject']} -> {msg['To']}")
        return {"status": "sent", "to": msg["To"], "subject": msg["Subject"], "method": f"STARTTLS:{port}"}

    def _send_ssl(self, host, port, addr, passwd, recipients, msg, tls_ctx) -> dict:
        """SMTP via implicit SSL (Port 465)."""
        with smtplib.SMTP_SSL(host, port, timeout=30, context=tls_ctx) as server:
            # Log TLS version
            sock = server.sock
            if hasattr(sock, "version"):
                logger.info(f"[SMTP] Connected via {sock.version()} to {host}:{port}")
            server.login(addr, passwd)
            server.sendmail(addr, recipients, msg.as_string())
        logger.info(f"E-Mail gesendet via SSL:{port}: {msg['Subject']} -> {msg['To']}")
        return {"status": "sent", "to": msg["To"], "subject": msg["Subject"], "method": f"SSL:{port}"}

    # ── Hilfsfunktionen ──────────────────────────────────────────────────────

    def _save_attachment(self, filename: str, data: bytes) -> str:
        self._workspace.mkdir(parents=True, exist_ok=True)
        dest = self._workspace / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = self._workspace / f"{stem}_{counter}{suffix}"
                counter += 1
        dest.write_bytes(data)
        logger.info(f"Anhang gespeichert: {dest}")
        return str(dest)

    @staticmethod
    def _decode_header(header_value: str) -> str:
        if not header_value:
            return ""
        decoded_parts = email.header.decode_header(header_value)
        parts = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                parts.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(part)
        return " ".join(parts)
