"""
RemoteStorageSkill – SFTP-Zugriff auf entfernte Rechner (z.B. ueber Tailscale).

Agenten koennen Dateien auf entfernten Maschinen lesen, schreiben und auflisten.
Die Verbindungsdaten werden im Dashboard-Wizard konfiguriert.

Sicherheit:
  - Zugriff nur innerhalb des konfigurierten REMOTE_SFTP_BASE_PATH.
  - Path-Traversal-Schutz via normpath + startswith-Check.
  - Max. Dateigroesse: 10 MB beim Lesen.
  - Verbindungs-Timeout: 10 Sekunden.
"""

import logging
import os
import posixpath
import stat
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("RemoteStorageSkill")

_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB
_CONNECT_TIMEOUT = 10  # seconds


class RemoteStorageSkill(BaseSkill):
    """SFTP-Dateizugriff auf entfernte Maschinen (z.B. ueber Tailscale-Netzwerk)."""

    name = "remote_storage"
    display_name = "Remote Storage (SFTP)"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None, **kwargs):
        self._agent_name = agent_name
        self._host = os.getenv("REMOTE_SFTP_HOST", "").strip()
        self._port = int(os.getenv("REMOTE_SFTP_PORT", "22"))
        self._user = os.getenv("REMOTE_SFTP_USER", "").strip()
        self._password = os.getenv("REMOTE_SFTP_PASSWORD", "").strip() or None
        self._key_path = os.getenv("REMOTE_SFTP_KEY_PATH", "").strip() or None
        self._base_path = os.getenv("REMOTE_SFTP_BASE_PATH", "~/aimos-shared").strip()

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "REMOTE_SFTP_HOST",
                "label": "Tailscale IP oder Hostname",
                "type": "text",
                "placeholder": "100.64.1.5",
                "hint": "z.B. 100.64.1.5 — findest du in der Tailscale-App",
                "secret": False,
            },
            {
                "key": "REMOTE_SFTP_PORT",
                "label": "SSH Port",
                "type": "text",
                "placeholder": "22",
                "hint": "Standard: 22",
                "secret": False,
            },
            {
                "key": "REMOTE_SFTP_USER",
                "label": "SSH Benutzername",
                "type": "text",
                "placeholder": "philipp",
                "hint": "Benutzername auf dem entfernten Rechner",
                "secret": False,
            },
            {
                "key": "REMOTE_SFTP_PASSWORD",
                "label": "SSH Passwort (optional)",
                "type": "password",
                "placeholder": "",
                "hint": "Nur noetig wenn kein SSH-Key verwendet wird",
                "secret": True,
            },
            {
                "key": "REMOTE_SFTP_KEY_PATH",
                "label": "Pfad zum SSH Private Key (optional)",
                "type": "text",
                "placeholder": "~/.ssh/id_ed25519",
                "hint": "Alternativ zum Passwort — Pfad zur privaten Schluesseldatei",
                "secret": False,
            },
            {
                "key": "REMOTE_SFTP_BASE_PATH",
                "label": "Basis-Pfad auf dem Remote-Rechner",
                "type": "text",
                "placeholder": "~/aimos-shared",
                "hint": "Ordner auf dem entfernten Rechner (wird automatisch angelegt)",
                "secret": False,
            },
        ]

    def is_available(self) -> bool:
        return bool(self._host and self._user)

    # ── SFTP Connection ───────────────────────────────────────────────────────

    def _connect(self):
        """Erstellt eine SFTP-Verbindung und gibt (transport, sftp) zurueck."""
        try:
            import paramiko
        except ImportError:
            raise RuntimeError(
                "paramiko ist nicht installiert. "
                "Bitte installieren: pip install paramiko"
            )

        transport = paramiko.Transport((self._host, self._port))
        transport.connect(timeout=_CONNECT_TIMEOUT)

        # Authentifizierung
        if self._key_path:
            key_file = os.path.expanduser(self._key_path)
            if not os.path.isfile(key_file):
                transport.close()
                raise RuntimeError(f"SSH-Key nicht gefunden: {key_file}")
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(key_file)
            except Exception:
                try:
                    pkey = paramiko.RSAKey.from_private_key_file(key_file)
                except Exception:
                    try:
                        pkey = paramiko.ECDSAKey.from_private_key_file(key_file)
                    except Exception:
                        transport.close()
                        raise RuntimeError(
                            f"SSH-Key konnte nicht geladen werden: {key_file}"
                        )
            transport.auth_publickey(self._user, pkey)
        elif self._password:
            transport.auth_password(self._user, self._password)
        else:
            transport.close()
            raise RuntimeError(
                "Kein Passwort und kein SSH-Key konfiguriert. "
                "Bitte eines von beiden im Dashboard eintragen."
            )

        sftp = paramiko.SFTPClient.from_transport(transport)
        return transport, sftp

    def _resolve_remote_path(self, sftp, relative_path: str) -> str:
        """Resolve relative path against base_path, with traversal protection."""
        # Expand ~ on the remote side
        base = self._base_path
        if base.startswith("~"):
            try:
                base = sftp.normalize(".")
                suffix = self._base_path[1:].lstrip("/")
                if suffix:
                    base = posixpath.join(base, suffix)
                else:
                    base = posixpath.join(base, "aimos-shared")
            except Exception:
                base = self._base_path

        if not relative_path or relative_path in (".", "/", ""):
            return base

        # Normalize to prevent traversal
        combined = posixpath.normpath(posixpath.join(base, relative_path))
        if not combined.startswith(base):
            raise ValueError(
                f"Zugriff verweigert: Pfad '{relative_path}' liegt ausserhalb von {base}"
            )
        return combined

    def _ensure_base_dir(self, sftp) -> None:
        """Erstellt den Basis-Ordner auf dem Remote-Rechner falls noetig."""
        base = self._resolve_remote_path(sftp, "")
        try:
            sftp.stat(base)
        except FileNotFoundError:
            try:
                sftp.mkdir(base)
                logger.info(f"Remote-Ordner erstellt: {base}")
            except Exception as exc:
                logger.warning(f"Konnte Remote-Ordner nicht erstellen: {exc}")

    # ── Tool Definitions ──────────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "remote_list_files",
                "description": "Listet Dateien in einem Verzeichnis auf dem entfernten Rechner auf (via SFTP).",
                "parameters": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad innerhalb des Basis-Ordners (leer = Basis-Ordner)",
                        "required": False,
                    },
                },
            },
            {
                "name": "remote_read_file",
                "description": "Liest eine Textdatei vom entfernten Rechner (via SFTP).",
                "parameters": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad zur Datei innerhalb des Basis-Ordners",
                        "required": True,
                    },
                },
            },
            {
                "name": "remote_write_file",
                "description": "Schreibt eine Datei auf den entfernten Rechner (via SFTP).",
                "parameters": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad zur Datei innerhalb des Basis-Ordners",
                        "required": True,
                    },
                    "content": {
                        "type": "string",
                        "description": "Dateiinhalt (Text)",
                        "required": True,
                    },
                },
            },
            {
                "name": "remote_setup_guide",
                "description": "Zeigt eine Schritt-fuer-Schritt-Anleitung zum Einrichten des Remote-Zugriffs via Tailscale + SSH.",
                "parameters": {
                    "os_type": {
                        "type": "string",
                        "description": "Betriebssystem: macos, windows oder linux",
                        "required": False,
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "remote_list_files":
            return self._remote_list_files(arguments.get("path", ""))
        elif tool_name == "remote_read_file":
            return self._remote_read_file(arguments.get("path", ""))
        elif tool_name == "remote_write_file":
            return self._remote_write_file(
                arguments.get("path", ""),
                arguments.get("content", ""),
            )
        elif tool_name == "remote_setup_guide":
            return self._remote_setup_guide(arguments.get("os_type", ""))
        return f"Unbekanntes Tool: {tool_name}"

    # ── Tool Implementations ──────────────────────────────────────────────────

    def _remote_list_files(self, path: str) -> str:
        if not self.is_available():
            return "Remote Storage ist nicht konfiguriert. Bitte Host und Benutzer im Dashboard eintragen."
        transport = None
        try:
            transport, sftp = self._connect()
            self._ensure_base_dir(sftp)
            remote_path = self._resolve_remote_path(sftp, path)

            try:
                entries = sftp.listdir_attr(remote_path)
            except FileNotFoundError:
                return f"Verzeichnis nicht gefunden: {path or '(Basis-Ordner)'}"

            entries.sort(key=lambda e: e.filename)
            lines = []
            for entry in entries[:100]:
                if stat.S_ISDIR(entry.st_mode or 0):
                    kind = "DIR"
                    size = ""
                else:
                    kind = "FILE"
                    size = f" ({entry.st_size} bytes)" if entry.st_size is not None else ""
                lines.append(f"  [{kind}] {entry.filename}{size}")

            result = f"Inhalt von {remote_path} ({len(entries)} Eintraege):\n" + "\n".join(lines)
            if len(entries) > 100:
                result += f"\n  ... und {len(entries) - 100} weitere"
            return result

        except Exception as exc:
            logger.error(f"[remote_storage] list error: {exc}")
            return f"Fehler bei Verbindung zu {self._host}: {exc}"
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass

    def _remote_read_file(self, path: str) -> str:
        if not self.is_available():
            return "Remote Storage ist nicht konfiguriert. Bitte Host und Benutzer im Dashboard eintragen."
        if not path:
            return "Fehler: 'path' ist ein Pflichtfeld."
        transport = None
        try:
            transport, sftp = self._connect()
            remote_path = self._resolve_remote_path(sftp, path)

            try:
                file_stat = sftp.stat(remote_path)
            except FileNotFoundError:
                return f"Datei nicht gefunden: {path}"

            if file_stat.st_size and file_stat.st_size > _MAX_READ_BYTES:
                return f"Datei zu gross: {file_stat.st_size} bytes (max {_MAX_READ_BYTES // 1024 // 1024} MB)"

            with sftp.open(remote_path, "r") as f:
                content = f.read().decode("utf-8", errors="replace")

            logger.info(f"[remote_storage] read: {remote_path} ({len(content)} chars)")
            return content

        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            logger.error(f"[remote_storage] read error: {exc}")
            return f"Fehler beim Lesen von {path}: {exc}"
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass

    def _remote_write_file(self, path: str, content: str) -> str:
        if not self.is_available():
            return "Remote Storage ist nicht konfiguriert. Bitte Host und Benutzer im Dashboard eintragen."
        if not path or not content:
            return "Fehler: 'path' und 'content' sind Pflichtfelder."

        # Reject path traversal
        if ".." in path:
            return f"Ungueltiger Pfad: {path}"

        transport = None
        try:
            transport, sftp = self._connect()
            self._ensure_base_dir(sftp)
            remote_path = self._resolve_remote_path(sftp, path)

            # Erstelle uebergeordnete Verzeichnisse falls noetig
            parent = posixpath.dirname(remote_path)
            try:
                sftp.stat(parent)
            except FileNotFoundError:
                # Rekursiv Ordner anlegen
                parts = []
                check = parent
                base = self._resolve_remote_path(sftp, "")
                while check != base and check != "/":
                    try:
                        sftp.stat(check)
                        break
                    except FileNotFoundError:
                        parts.append(check)
                        check = posixpath.dirname(check)
                for d in reversed(parts):
                    sftp.mkdir(d)

            with sftp.open(remote_path, "w") as f:
                f.write(content.encode("utf-8"))

            logger.info(f"[remote_storage] write: {remote_path} ({len(content)} chars)")
            return f"Datei geschrieben: {posixpath.basename(remote_path)} ({len(content)} Zeichen) auf {self._host}"

        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            logger.error(f"[remote_storage] write error: {exc}")
            return f"Fehler beim Schreiben von {path}: {exc}"
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass

    def _remote_setup_guide(self, os_type: str) -> str:
        os_type = os_type.lower().strip() if os_type else ""

        guide = (
            "Einrichtung: Remote-Zugriff ueber Tailscale + SSH\n"
            "================================================\n\n"
        )

        # Step 1: Tailscale
        guide += (
            "Schritt 1: Tailscale installieren\n"
            "---\n"
            "Lade Tailscale herunter von https://tailscale.com/download\n"
            "Installiere es und melde dich an (oder verwende den Einladungslink "
            "den dir dein Admin schickt).\n"
            "Nach der Anmeldung bekommt dein Rechner eine Tailscale-IP "
            "(z.B. 100.64.x.x) — die brauchst du gleich.\n\n"
        )

        # Step 2: SSH aktivieren (OS-specific)
        guide += "Schritt 2: SSH aktivieren\n---\n"

        if os_type == "macos":
            guide += (
                "macOS:\n"
                "Oeffne Systemeinstellungen → Allgemein → Teilen → "
                "\"Remote Login\" aktivieren.\n"
                "Stelle sicher dass dein Benutzer in der Liste der "
                "erlaubten Benutzer steht.\n\n"
            )
        elif os_type == "windows":
            guide += (
                "Windows:\n"
                "1. Oeffne Einstellungen → Apps → Optionale Features\n"
                "2. Klicke auf \"Feature hinzufuegen\" und suche \"OpenSSH Server\"\n"
                "3. Installieren und dann den Dienst starten:\n"
                "   PowerShell (als Admin):\n"
                "   Start-Service sshd\n"
                "   Set-Service -Name sshd -StartupType Automatic\n\n"
            )
        elif os_type == "linux":
            guide += (
                "Linux:\n"
                "Terminal oeffnen und ausfuehren:\n"
                "sudo apt install openssh-server && sudo systemctl enable ssh\n"
                "sudo systemctl start ssh\n\n"
            )
        else:
            guide += (
                "macOS:\n"
                "  Systemeinstellungen → Allgemein → Teilen → "
                "\"Remote Login\" aktivieren\n\n"
                "Windows:\n"
                "  Einstellungen → Apps → Optionale Features → "
                "\"OpenSSH Server\" hinzufuegen → Dienst starten:\n"
                "  PowerShell (Admin): Start-Service sshd\n\n"
                "Linux:\n"
                "  sudo apt install openssh-server && sudo systemctl enable ssh\n\n"
            )

        # Step 3: Ordner anlegen
        guide += "Schritt 3: Shared-Ordner anlegen\n---\n"

        if os_type == "macos" or os_type == "linux":
            guide += "mkdir -p ~/aimos-shared\n\n"
        elif os_type == "windows":
            guide += (
                "PowerShell:\n"
                "New-Item -ItemType Directory -Path $HOME\\aimos-shared -Force\n\n"
            )
        else:
            guide += (
                "macOS / Linux:  mkdir -p ~/aimos-shared\n"
                "Windows:        New-Item -ItemType Directory -Path "
                "$HOME\\aimos-shared -Force\n\n"
            )

        # Step 4: Test
        guide += (
            "Schritt 4: Verbindung testen\n"
            "---\n"
            "Sage mir deine Tailscale-IP (findest du in der Tailscale-App "
            "unter \"My Devices\") und deinen Benutzernamen auf dem Rechner.\n"
            "Ich teste dann die Verbindung und richte alles ein.\n\n"
            "Tipp: Du kannst die IP auch selbst testen:\n"
            "  ssh deinuser@deine-tailscale-ip\n"
            "Wenn das klappt, funktioniert auch der Remote-Zugriff von AIMOS."
        )

        return guide
