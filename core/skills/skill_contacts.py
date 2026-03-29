"""
CR-145: Agent Contact Book — structured contact management in workspace.

Contacts are stored as JSON in the agent's workspace (contacts.json).
Auto-populated from Telegram interactions. Agents can search, add,
and update contacts with structured fields.

Tools:
  add_contact(name, company, phone, email, notes)  — Add or update a contact
  find_contact(query)                                — Search contacts by name/company/notes
  list_contacts()                                    — Show all contacts
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("AIMOS.Contacts")


class ContactsSkill(BaseSkill):
    """Structured contact book for agents — stored in workspace."""

    name = "contacts"
    display_name = "Contact Book"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name

    def is_available(self) -> bool:
        return bool(self._agent_name)

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "add_contact",
                "description": (
                    "Add or update a contact in your contact book. "
                    "If a contact with the same name exists, it will be updated."
                ),
                "parameters": {
                    "name": {"type": "string", "description": "Full name", "required": True},
                    "company": {"type": "string", "description": "Company name", "default": ""},
                    "phone": {"type": "string", "description": "Phone number", "default": ""},
                    "email": {"type": "string", "description": "Email address", "default": ""},
                    "role": {"type": "string", "description": "Job title or role", "default": ""},
                    "notes": {"type": "string", "description": "Additional notes", "default": ""},
                    "telegram_id": {"type": "string", "description": "Telegram chat ID", "default": ""},
                },
            },
            {
                "name": "find_contact",
                "description": "Search contacts by name, company, or any field.",
                "parameters": {
                    "query": {"type": "string", "description": "Search term", "required": True},
                },
            },
            {
                "name": "list_contacts",
                "description": "Show all contacts in the contact book.",
                "parameters": {},
            },
        ]

    def _contacts_path(self) -> Path:
        ws = self.workspace_path(self._agent_name)
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "contacts.json"

    def _load(self) -> list[dict]:
        path = self._contacts_path()
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, contacts: list[dict]):
        path = self._contacts_path()
        contacts.sort(key=lambda c: c.get("name", "").lower())
        path.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "add_contact":
            return self._add_contact(arguments)
        elif tool_name == "find_contact":
            return self._find_contact(arguments.get("query", ""))
        elif tool_name == "list_contacts":
            return self._list_contacts()
        return f"Unknown tool: {tool_name}"

    def _add_contact(self, args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            return "Error: 'name' is required."

        contacts = self._load()

        # Find existing contact by name (case-insensitive)
        existing = None
        for c in contacts:
            if c.get("name", "").lower() == name.lower():
                existing = c
                break

        if existing:
            # Update existing — only overwrite non-empty fields
            for field in ("company", "phone", "email", "role", "notes", "telegram_id"):
                val = args.get(field, "").strip()
                if val:
                    existing[field] = val
            existing["updated"] = datetime.now(timezone.utc).isoformat()
            action = "updated"
        else:
            # Create new
            contact = {
                "name": name,
                "company": args.get("company", "").strip() or None,
                "phone": args.get("phone", "").strip() or None,
                "email": args.get("email", "").strip() or None,
                "role": args.get("role", "").strip() or None,
                "notes": args.get("notes", "").strip() or None,
                "telegram_id": args.get("telegram_id", "").strip() or None,
                "created": datetime.now(timezone.utc).isoformat(),
                "updated": datetime.now(timezone.utc).isoformat(),
                "last_interaction": None,
            }
            contacts.append(contact)
            action = "added"

        self._save(contacts)
        logger.info(f"[Contacts] {self._agent_name}: {action} '{name}'")
        return f"Contact {action}: {name}"

    def _find_contact(self, query: str) -> str:
        if not query:
            return "Error: 'query' is required."
        contacts = self._load()
        query_lower = query.lower()

        matches = []
        for c in contacts:
            searchable = " ".join(str(v) for v in c.values() if v).lower()
            if query_lower in searchable:
                matches.append(c)

        if not matches:
            return f"No contacts matching '{query}'."

        lines = [f"Found {len(matches)} contact(s):"]
        for c in matches:
            lines.append(self._format_contact(c))
        return "\n".join(lines)

    def _list_contacts(self) -> str:
        contacts = self._load()
        if not contacts:
            return "Contact book is empty."
        lines = [f"Contact book ({len(contacts)} contacts):"]
        for c in contacts:
            lines.append(self._format_contact(c))
        return "\n".join(lines)

    @staticmethod
    def _format_contact(c: dict) -> str:
        parts = [f"  {c.get('name', '?')}"]
        if c.get("role"):
            parts[0] += f" ({c['role']})"
        if c.get("company"):
            parts.append(f"    Company: {c['company']}")
        if c.get("phone"):
            parts.append(f"    Phone: {c['phone']}")
        if c.get("email"):
            parts.append(f"    Email: {c['email']}")
        if c.get("telegram_id"):
            parts.append(f"    Telegram: {c['telegram_id']}")
        if c.get("notes"):
            parts.append(f"    Notes: {c['notes']}")
        return "\n".join(parts)


def auto_update_contact_from_message(agent_name: str, sender_id: int, kind: str):
    """Called after processing a message — updates last_interaction timestamp.

    Lightweight: just updates the timestamp, doesn't create new contacts.
    """
    if not sender_id or sender_id == 0 or "telegram" not in kind:
        return
    try:
        ws = BaseSkill.workspace_path(agent_name)
        path = ws / "contacts.json"
        if not path.exists():
            return
        contacts = json.loads(path.read_text(encoding="utf-8"))
        sid = str(sender_id)
        for c in contacts:
            if c.get("telegram_id") == sid:
                c["last_interaction"] = datetime.now(timezone.utc).isoformat()
                path.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
                break
    except Exception:
        pass
