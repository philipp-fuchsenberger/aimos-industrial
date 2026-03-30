"""
SharedStorageSkill – Zugriff auf lokale/gemountete Ordner (SMB, NFS, Pfade).

Agenten koennen Dateien in konfigurierten Pfaden lesen, schreiben und auflisten.
Die erlaubten Pfade werden pro Agent im Dashboard-Wizard konfiguriert
und in agents.config unter "shared_storage_paths" gespeichert.

Sicherheit:
  - Nur explizit freigegebene Pfade sind zuganglich.
  - Path-Traversal-Schutz via os.path.realpath() + startswith-Check.
  - Symlinks werden nur akzeptiert, wenn sie innerhalb eines erlaubten Pfades bleiben.
  - Max. Dateigroesse: 10 MB beim Lesen.
"""

import logging
import os
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("SharedStorageSkill")

_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB


class SharedStorageSkill(BaseSkill):
    """Lokaler Dateizugriff auf konfigurierte Pfade (SMB-Mounts, NAS, lokale Ordner)."""

    name = "shared_storage"
    display_name = "Shared Folders (Local/SMB)"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None, **kwargs):
        self._agent_name = agent_name
        self._allowed_paths: list[Path] = []
        cfg = agent_config or {}
        raw = cfg.get("shared_storage_paths", "")
        if isinstance(raw, str) and raw.strip():
            for p in raw.split(","):
                p = p.strip()
                if p:
                    resolved = Path(os.path.realpath(os.path.expanduser(p)))
                    if resolved.is_dir():
                        self._allowed_paths.append(resolved)
                    else:
                        logger.warning(f"Shared path not a directory (skipped): {p}")

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "shared_storage_paths",
                "label": "Shared Folder Paths",
                "type": "text",
                "placeholder": "/mnt/nas/shared, /home/philipp/Documents",
                "hint": "Comma-separated local or mounted paths this agent may access.",
                "secret": False,
            },
        ]

    def is_available(self) -> bool:
        return len(self._allowed_paths) > 0

    def _resolve_and_check(self, base_path: str, filename: str = "") -> Path | None:
        """Resolve a path and verify it falls within an allowed path.

        Returns the resolved Path, or None if access is denied.
        """
        if not base_path:
            return None
        target = os.path.realpath(os.path.expanduser(
            os.path.join(base_path, filename) if filename else base_path
        ))
        target_path = Path(target)
        for allowed in self._allowed_paths:
            try:
                target_path.relative_to(allowed)
                return target_path
            except ValueError:
                continue
        logger.warning(f"Access denied — {target} not in allowed paths")
        return None

    # ── Tool Definitions ─────────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "list_shared",
                "description": "Listet Dateien in einem freigegebenen Ordner auf.",
                "parameters": {
                    "path": {"type": "string", "description": "Pfad zum Ordner", "required": True},
                },
            },
            {
                "name": "read_shared",
                "description": "Liest den Inhalt einer Datei aus einem freigegebenen Ordner.",
                "parameters": {
                    "path": {"type": "string", "description": "Pfad zum Ordner", "required": True},
                    "filename": {"type": "string", "description": "Dateiname", "required": True},
                },
            },
            {
                "name": "write_shared",
                "description": "Schreibt eine Datei in einen freigegebenen Ordner.",
                "parameters": {
                    "path": {"type": "string", "description": "Pfad zum Ordner", "required": True},
                    "filename": {"type": "string", "description": "Dateiname", "required": True},
                    "content": {"type": "string", "description": "Dateiinhalt (Text)", "required": True},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_shared":
            return self._list_shared(arguments.get("path", ""))
        elif tool_name == "read_shared":
            return self._read_shared(arguments.get("path", ""), arguments.get("filename", ""))
        elif tool_name == "write_shared":
            return self._write_shared(
                arguments.get("path", ""),
                arguments.get("filename", ""),
                arguments.get("content", ""),
            )
        return f"Unbekanntes Tool: {tool_name}"

    def _list_shared(self, path: str) -> str:
        resolved = self._resolve_and_check(path)
        if not resolved:
            return f"Zugriff verweigert: '{path}' ist kein erlaubter Pfad."
        if not resolved.is_dir():
            return f"Kein Verzeichnis: {path}"
        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for e in entries[:100]:
                kind = "DIR" if e.is_dir() else "FILE"
                size = ""
                if e.is_file():
                    try:
                        size = f" ({e.stat().st_size} bytes)"
                    except OSError:
                        pass
                lines.append(f"  [{kind}] {e.name}{size}")
            result = f"Inhalt von {resolved} ({len(entries)} Eintraege):\n" + "\n".join(lines)
            if len(entries) > 100:
                result += f"\n  ... und {len(entries) - 100} weitere"
            return result
        except PermissionError:
            return f"Keine Berechtigung: {path}"
        except OSError as exc:
            return f"Fehler: {exc}"

    def _read_shared(self, path: str, filename: str) -> str:
        if not filename:
            return "Fehler: 'filename' ist ein Pflichtfeld."
        resolved = self._resolve_and_check(path, filename)
        if not resolved:
            return f"Zugriff verweigert: '{path}/{filename}' ist kein erlaubter Pfad."
        if not resolved.is_file():
            return f"Datei nicht gefunden: {filename}"
        try:
            size = resolved.stat().st_size
            if size > _MAX_READ_BYTES:
                return f"Datei zu gross: {size} bytes (max {_MAX_READ_BYTES // 1024 // 1024} MB)"
            content = resolved.read_text(encoding="utf-8", errors="replace")
            logger.info(f"[shared_storage] read: {resolved} ({len(content)} chars)")
            return content
        except UnicodeDecodeError:
            return f"Binaerdatei — kann nicht als Text gelesen werden: {filename}"
        except PermissionError:
            return f"Keine Leseberechtigung: {filename}"
        except OSError as exc:
            return f"Fehler: {exc}"

    def _write_shared(self, path: str, filename: str, content: str) -> str:
        if not filename or not content:
            return "Fehler: 'filename' und 'content' sind Pflichtfelder."
        # Reject path traversal in filename
        if "/" in filename or "\\" in filename or ".." in filename:
            return f"Ungueltiger Dateiname: {filename}"
        resolved_dir = self._resolve_and_check(path)
        if not resolved_dir:
            return f"Zugriff verweigert: '{path}' ist kein erlaubter Pfad."
        if not resolved_dir.is_dir():
            return f"Kein Verzeichnis: {path}"
        target = resolved_dir / filename
        # Verify the final path is still within allowed
        final = self._resolve_and_check(str(target))
        if not final:
            return "Zugriff verweigert auf Zieldatei."
        try:
            target.write_text(content, encoding="utf-8")
            logger.info(f"[shared_storage] write: {target} ({len(content)} chars)")
            return f"Datei geschrieben: {target.name} ({len(content)} Zeichen)"
        except PermissionError:
            return f"Keine Schreibberechtigung: {path}/{filename}"
        except OSError as exc:
            return f"Fehler: {exc}"
