"""
BaseSkill – Abstrakte Basisklasse fuer alle AIMOS-Skills.

Jeder Skill implementiert mindestens:
  is_available()    – True wenn der Skill einsatzbereit ist (API-Key vorhanden etc.)
  enrich_context()  – Liefert einen optionalen Kontext-Block fuer den System-Prompt.

Optionale Erweiterungspunkte:
  get_tools()        – Gibt Tool-Definitionen zurueck fuer LLM-Tool-Calling
  execute_tool()     – Fuehrt einen Tool-Call aus und gibt das Ergebnis zurueck
  on_session_start() – Einmalig beim Start der Sitzung
  on_session_end()   – Einmalig beim Ende der Sitzung

Workspace:
  Jeder Skill hat Zugriff auf den Workspace des Agenten via workspace_path.
  Der /public Unterordner ist fuer andere Agenten lesend zugaenglich.
"""

import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

_log = logging.getLogger("AIMOS.BaseSkill")

_STORAGE_ROOT = Path("storage") / "agents"


class BaseSkill(ABC):
    """Abstrakte Basisklasse fuer alle funktionalen AIMOS-Skills."""

    name: str           # Eindeutiger Skill-Bezeichner, muss in der Subklasse gesetzt werden
    display_name: str = ""  # Human-readable name for Dashboard UI

    @staticmethod
    def _sanitize_agent_name(name: str) -> str:
        """Sanitize agent name for filesystem use. Prevents path traversal."""
        safe = re.sub(r"[^a-z0-9_-]", "", name.lower().strip())
        if not safe:
            raise ValueError(f"Invalid agent name: {name!r}")
        return safe

    @classmethod
    def workspace_path(cls, agent_name: str) -> Path:
        """Return the workspace root for an agent: storage/agents/{agent_name}/

        Creates the directory and /public subdirectory if they don't exist.
        """
        safe_name = cls._sanitize_agent_name(agent_name)
        base = _STORAGE_ROOT / safe_name
        public = base / "public"
        base.mkdir(parents=True, exist_ok=True)
        public.mkdir(exist_ok=True)
        return base

    @classmethod
    def public_path(cls, agent_name: str) -> Path:
        """Return the public folder for an agent (readable by other agents)."""
        ws = cls.workspace_path(agent_name)
        return ws / "public"

    @classmethod
    def read_public(cls, agent_name: str, filename: str) -> bytes | None:
        """Read a file from another agent's /public folder.

        Returns None if the file does not exist or the name is invalid.
        Prevents path traversal by rejecting filenames with slashes or '..'.
        """
        # Input validation: reject path traversal attempts
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            _log.warning(f"Rejected invalid public filename: {filename!r}")
            return None
        target = cls.public_path(agent_name) / filename
        if not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError as exc:
            _log.error(f"Failed to read public file {target}: {exc}")
            return None

    @classmethod
    def memory_db_path(cls, agent_name: str) -> Path:
        """Return path to per-agent SQLite memory DB."""
        return cls.workspace_path(agent_name) / "memory.db"

    @classmethod
    def config_fields(cls) -> list[dict]:
        """Return config fields the Dashboard wizard shows when this skill is enabled.

        Each dict:
          key:         Field identifier (e.g. "EMAIL_ADDRESS")
          label:       Human-readable label for the form
          type:        "text" | "password" | "textarea"
          placeholder: Placeholder text
          hint:        Help text below the field
          secret:      True → stored in env_secrets, False → stored in config (default: True)
        """
        return []

    @abstractmethod
    def is_available(self) -> bool:
        """Gibt True zurueck wenn der Skill einsatzbereit ist."""

    async def enrich_context(self, user_text: str) -> str:
        """Liefert einen optionalen Kontext-Block fuer den System-Prompt."""
        return ""

    def get_tools(self) -> list[dict]:
        """Returns tool definitions for LLM tool-calling.

        Each tool is a dict:
          {"name": "send_email", "description": "...", "parameters": {...}}

        Override in subclasses that provide callable tools.
        Default: empty list (skill has no callable tools).
        """
        return []

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool call and return the result as text.

        Args:
            tool_name:  Name of the tool (from get_tools()).
            arguments:  Dict of parameter values.

        Returns:
            Human-readable result string.
        """
        return f"[Fehler: Tool '{tool_name}' nicht implementiert in {self.name}]"

    async def on_session_start(self) -> None:
        """Optionaler Hook – wird einmalig beim Sitzungsstart aufgerufen."""

    async def on_session_end(self) -> None:
        """Optionaler Hook – wird einmalig beim Sitzungsende aufgerufen."""
