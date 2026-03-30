"""
HybridReasoningSkill – Gateway zu externen LLMs mit PII-Anonymisierung.

Architecture:
  1. Agent will eine komplexe Frage an ein externes LLM senden.
  2. Der Vault (Anonymizer) scannt den Prompt und ersetzt:
     - Credentials: API-Keys, Passwoerter aus der Agent-Config
     - PII: E-Mail-Adressen, Telefonnummern, deutsche Namensstrukturen
  3. Mapping PLACEHOLDER_X <-> Original wird in memory.db gespeichert.
  4. Der anonymisierte Prompt geht an die externe API (OpenRouter/OpenAI/Claude).
  5. Die Antwort wird deanonymisiert: Platzhalter → Originaldaten.

Anonymisierungs-Level (konfigurierbar im Wizard):
  strict   — Alles: Names, Emails, Phones, Credentials
  medium   — Credentials + Emails + Phones (keine Namen)
  minimal  — Nur Credentials (API-Keys, Passwoerter)

Credentials (env_secrets):
  OPENROUTER_API_KEY   — OpenRouter API Key (primaer)
  OPENAI_API_KEY       — OpenAI API Key (fallback)
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("HybridReasoningSkill")

# CR-174: Daily API call budget (module-level counter, resets each day)
_daily_api_calls: dict[str, int] = {}  # {"YYYY-MM-DD": count}

# ── PII Patterns ─────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
_PHONE_RE = re.compile(
    r'(?:^|(?<=\s)|(?<=[.,;:!?]))'             # Must start after whitespace, punctuation, or start of string
    r'(?:'
    r'(?:\+49|0049)\s*[\d\s/\-()]{6,15}\d'    # International German prefix
    r'|'
    r'0[1-9][\d\s/\-()]{5,14}\d'              # Domestic: 0 + non-zero digit + number body
    r')',
    re.MULTILINE,
)
# German name patterns: require honorific prefix (Herr/Frau/Dr./Prof.) to avoid false positives
# on English terms like "Best Practices", "Deep Memory", "Context Budget" etc.
_NAME_RE = re.compile(
    r'(?:Herr|Frau|Dr\.|Prof\.|Hr\.|Fr\.)\s+'
    r'[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?'
)
# API keys / tokens: long alphanumeric strings (>20 chars) or patterns like sk-..., BSA...
_CREDENTIAL_RE = re.compile(
    r'\b(?:sk-[A-Za-z0-9]{20,}|BSA[A-Za-z0-9]{20,}|[A-Za-z0-9_-]{32,})\b'
)

# ── CR-163: Enhanced PII Patterns ────────────────────────────────────────────

# IBAN (international, but especially DE22-char)
_IBAN_RE = re.compile(
    r'[A-Z]{2}\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{0,2}'
)

# German street addresses: Musterstraße 12a, Hauptstr. 5, Bergweg 23
_STREET_RE = re.compile(
    r'\b\w+(?:straße|str\.|weg|gasse|platz|allee)\s+\d+\w?\b', re.IGNORECASE
)

# German postal code + city: 80331 München
_PLZ_CITY_RE = re.compile(r'\b\d{5}\s+\w+\b')

# Common German and Turkish first names for aggressive name detection
_COMMON_FIRST_NAMES = {
    # German (top ~50)
    "Alexander", "Andrea", "Andreas", "Anna", "Bernd", "Birgit", "Christian",
    "Christina", "Daniel", "Dennis", "Dieter", "Eva", "Frank", "Hans",
    "Heike", "Heinz", "Helmut", "Jan", "Jana", "Jens", "Julia", "Jürgen",
    "Karl", "Katharina", "Klaus", "Lars", "Laura", "Lukas", "Manfred",
    "Maria", "Markus", "Martin", "Matthias", "Michael", "Monika", "Nicole",
    "Peter", "Petra", "Ralf", "Sabine", "Sandra", "Sarah", "Sebastian",
    "Stefan", "Stefanie", "Thomas", "Tobias", "Uwe", "Ursula", "Wolfgang",
    # Turkish (top ~50)
    "Ahmet", "Ali", "Ayşe", "Burak", "Cem", "Deniz", "Elif", "Emine",
    "Emre", "Erdoğan", "Fatma", "Gül", "Hakan", "Halil", "Hasan",
    "Hüseyin", "Ibrahim", "Kemal", "Leyla", "Mehmet", "Murat", "Mustafa",
    "Naz", "Nihal", "Nur", "Osman", "Ömer", "Özlem", "Recep", "Selin",
    "Serkan", "Sibel", "Süleyman", "Şerif", "Tarık", "Tolga", "Tuncay",
    "Tülay", "Ufuk", "Umut", "Yasin", "Yıldız", "Yusuf", "Zehra",
    "Zeynep", "Zübeyde", "Baran", "Canan", "Derya", "Esra",
}

# Build regex: FirstName LastName where FirstName is in the known list
# LastName = capitalized word (including German umlauts)
_NAME_LIST_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(n) for n in sorted(_COMMON_FIRST_NAMES)) +
    r')\s+[A-ZÄÖÜ][a-zäöüß]+\b'
)


class Vault:
    """PII Anonymizer with bidirectional mapping stored in SQLite.

    Each anonymization session gets a unique session_id. Mappings are stored
    in the agent's memory.db under the vault_mappings table.
    """

    def __init__(self, memory_db_path: Path, level: str = "strict"):
        self._db_path = memory_db_path
        self._level = level  # strict | medium | minimal
        self._ensure_table()

    def _ensure_table(self):
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vault_mappings (
                    session_id   TEXT NOT NULL,
                    placeholder  TEXT NOT NULL,
                    original     TEXT NOT NULL,
                    category     TEXT NOT NULL,
                    created_at   TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (session_id, placeholder)
                )
            """)
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"Vault table init failed: {exc}")

    def anonymize(self, text: str, session_id: str, extra_secrets: dict | None = None) -> str:
        """Replace PII in text with placeholders. Store mappings in memory.db."""
        mappings: list[tuple[str, str, str]] = []  # (placeholder, original, category)
        counter = 0

        def _replace(match_text: str, category: str) -> str:
            nonlocal counter
            counter += 1
            placeholder = f"__VAULT_{category.upper()}_{counter}__"
            mappings.append((placeholder, match_text, category))
            return placeholder

        # 1. Credentials from config (always, regardless of level)
        if extra_secrets:
            for key, val in extra_secrets.items():
                if val and isinstance(val, str) and len(val) > 4:
                    if val in text:
                        ph = _replace(val, "credential")
                        text = text.replace(val, ph)

        # 2. Credential-like patterns (always)
        text = _CREDENTIAL_RE.sub(lambda m: _replace(m.group(), "credential"), text)

        # 3. Emails + Phones (medium and strict)
        if self._level in ("strict", "medium"):
            text = _EMAIL_RE.sub(lambda m: _replace(m.group(), "email"), text)
            text = _PHONE_RE.sub(lambda m: _replace(m.group(), "phone"), text)

        # 4. Names (strict only)
        if self._level == "strict":
            text = _NAME_RE.sub(lambda m: _replace(m.group(), "name"), text)

        # CR-163: Enhanced PII patterns (strict and medium)
        if self._level in ("strict", "medium"):
            # 5. IBAN numbers
            text = _IBAN_RE.sub(lambda m: _replace(m.group(), "iban"), text)
            # 6. German street addresses
            text = _STREET_RE.sub(lambda m: _replace(m.group(), "address"), text)
            # 7. German postal code + city
            text = _PLZ_CITY_RE.sub(lambda m: _replace(m.group(), "plz_city"), text)

        # CR-163: Aggressive name detection via first-name list (strict only)
        if self._level == "strict":
            text = _NAME_LIST_RE.sub(lambda m: _replace(m.group(), "name"), text)

        # Store mappings
        if mappings:
            self._store_mappings(session_id, mappings)
            logger.info(
                f"[Vault] Anonymized {len(mappings)} items "
                f"(level={self._level}, session={session_id[:12]})"
            )

        return text

    def deanonymize(self, text: str, session_id: str) -> str:
        """Replace placeholders back with original values from memory.db."""
        mappings = self._load_mappings(session_id)
        for placeholder, original in mappings:
            text = text.replace(placeholder, original)
        return text

    def _store_mappings(self, session_id: str, mappings: list[tuple[str, str, str]]):
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            conn.executemany(
                "INSERT OR REPLACE INTO vault_mappings (session_id, placeholder, original, category) "
                "VALUES (?, ?, ?, ?)",
                [(session_id, ph, orig, cat) for ph, orig, cat in mappings],
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"Vault store failed: {exc}")

    def _load_mappings(self, session_id: str) -> list[tuple[str, str]]:
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            cur = conn.execute(
                "SELECT placeholder, original FROM vault_mappings WHERE session_id=?",
                (session_id,),
            )
            result = cur.fetchall()
            conn.close()
            return result
        except Exception:
            return []


_EXTERNAL_SYSTEM_PROMPT = (
    "You are an expert assistant supporting an AIMOS agent (an on-premise AI system). "
    "The agent runs a local LLM (Qwen 3.5:27b) on the user's own server and escalates "
    "complex questions to you. Answer precisely and concisely. The user's data has been "
    "anonymized — placeholders like __VAULT_NAME_1__ represent redacted PII. "
    "Do NOT ask about or try to guess the redacted values. "
    "Respond in the same language as the question."
)


class HybridReasoningSkill(BaseSkill):
    """Gateway zu externen LLMs mit automatischer PII-Anonymisierung."""

    name = "hybrid_reasoning"
    display_name = "Hybrid Reasoning (External LLM)"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None,
                 secrets: dict[str, str] | None = None, **kwargs):
        self._init_secrets(secrets)
        self._agent_name = agent_name
        cfg = agent_config or {}
        self._anon_level = cfg.get("hybrid_anon_level", "strict")
        self._model = cfg.get("hybrid_model", "anthropic/claude-sonnet-4-20250514")
        self._max_daily_api_calls = int(cfg.get("max_daily_api_calls", 100))
        self._vault: Vault | None = None

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "OPENROUTER_API_KEY",
                "label": "OpenRouter API Key",
                "type": "password",
                "placeholder": "sk-or-...",
                "hint": "Primary gateway for external LLMs. Get one at openrouter.ai",
                "secret": True,
            },
            {
                "key": "hybrid_anon_level",
                "label": "Anonymization Level",
                "type": "text",
                "placeholder": "strict",
                "hint": "strict = All PII | medium = Credentials+Emails+Phones | minimal = Credentials only",
                "secret": False,
            },
            {
                "key": "hybrid_model",
                "label": "External Model",
                "type": "text",
                "placeholder": "anthropic/claude-sonnet-4-20250514",
                "hint": "OpenRouter model ID (e.g. anthropic/claude-sonnet-4-20250514, openai/gpt-4o)",
                "secret": False,
            },
        ]

    def is_available(self) -> bool:
        return bool(self._secret("OPENROUTER_API_KEY") or self._secret("OPENAI_API_KEY"))

    def _get_vault(self) -> Vault:
        if self._vault is None:
            db_path = self.memory_db_path(self._agent_name)
            self._vault = Vault(db_path, level=self._anon_level)
        return self._vault

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "ask_external",
                "description": (
                    "Sendet eine Frage an ein externes, leistungsstaerkeres LLM "
                    "(z.B. Claude, GPT-4). Persoenliche Daten werden automatisch "
                    "anonymisiert und nach der Antwort wiederhergestellt."
                ),
                "parameters": {
                    "question": {"type": "string", "description": "Die Frage / der Prompt", "required": True},
                    "context": {"type": "string", "description": "Optionaler Kontext", "default": ""},
                },
            },
            {
                "name": "analyze_image",
                "description": (
                    "Analysiert ein Bild aus dem Workspace (Foto, Rechnung, Dokument, Screenshot). "
                    "Extrahiert Text (OCR), beschreibt den Inhalt, erkennt strukturierte Daten."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Dateiname des Bildes im Workspace", "required": True},
                    "question": {"type": "string", "description": "Spezifische Frage zum Bild", "default": ""},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "ask_external":
            return await self._ask_external(
                arguments.get("question", ""),
                arguments.get("context", ""),
            )
        elif tool_name == "analyze_image":
            from core.skills.base import BaseSkill
            filename = arguments.get("filename", "")
            workspace = BaseSkill.workspace_path(self._agent_name)
            image_path = workspace / filename
            # Check thread directory too
            _thread_id = getattr(self, '_current_thread_id', '') or ''
            if not image_path.exists() and _thread_id:
                _thread_path = Path("storage") / "threads" / _thread_id / filename
                if _thread_path.exists():
                    image_path = _thread_path
            if not image_path.exists():
                return (f"Error: File '{filename}' not found. "
                        f"Do NOT retry. Inform the customer that the image could not be processed.")
            try:
                result = await self.analyze_image(
                    str(image_path),
                    arguments.get("question", ""),
                )
                return result
            except Exception as exc:
                return (f"Error: Image analysis failed ({exc}). "
                        f"Do NOT retry. Inform the customer that the image could not be analyzed at this time.")
        return f"Unbekanntes Tool: {tool_name}"

    async def _ask_external(self, question: str, context: str = "") -> str:
        if not question:
            return "Fehler: 'question' ist ein Pflichtfeld."

        # CR-174: API cost budget — enforce daily call limit
        today = datetime.now().strftime("%Y-%m-%d")
        # Clean stale day entries
        for k in list(_daily_api_calls.keys()):
            if k != today:
                del _daily_api_calls[k]
        current_count = _daily_api_calls.get(today, 0)
        if current_count >= self._max_daily_api_calls:
            logger.warning(
                f"[Hybrid] Daily API budget exceeded: {current_count}/{self._max_daily_api_calls} "
                f"(agent={self._agent_name})"
            )
            return (
                f"Fehler: Tagesbudget fuer externe API-Aufrufe erreicht "
                f"({self._max_daily_api_calls} Aufrufe/Tag). Bitte morgen erneut versuchen."
            )
        _daily_api_calls[today] = current_count + 1

        vault = self._get_vault()
        session_id = hashlib.sha256(
            f"{self._agent_name}:{time.time()}:{question[:50]}".encode()
        ).hexdigest()[:24]

        # Collect secrets for credential anonymization
        agent_secrets = {}
        for key in ("TELEGRAM_BOT_TOKEN", "EMAIL_PASSWORD", "OPENROUTER_API_KEY",
                     "OPENAI_API_KEY", "BRAVE_API_KEY"):
            val = self._secret(key)
            if val:
                agent_secrets[key] = val

        # Build prompt
        full_prompt = question
        if context:
            full_prompt = f"Kontext: {context}\n\nFrage: {question}"

        # Anonymize
        safe_prompt = vault.anonymize(full_prompt, session_id, extra_secrets=agent_secrets)

        anon_count = safe_prompt.count("__VAULT_")
        if anon_count:
            logger.info(f"[Hybrid] {anon_count} items anonymized (session={session_id[:12]})")

        # Compliance audit: log EVERYTHING that goes out (anonymized) and what comes back
        # This is the legally binding proof of what data left the server
        self._audit_log(
            session_id, "SYSTEM_PROMPT",
            f"model={self._model}",
            _EXTERNAL_SYSTEM_PROMPT,
        )
        self._audit_log(
            session_id, "REQUEST",
            f"model={self._model} anon_items={anon_count} prompt_len={len(safe_prompt)}",
            safe_prompt,
        )

        # Call external API
        try:
            raw_response = await self._call_api(safe_prompt)
        except Exception as exc:
            self._audit_log(session_id, "ERROR", str(exc))
            logger.error(f"[Hybrid] API call failed: {exc}")
            # Write alert to global_settings for Dashboard display
            try:
                import psycopg2
                from core.config import Config
                c = psycopg2.connect(
                    host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
                    user=Config.PG_USER, password=Config.PG_PASSWORD, connect_timeout=3)
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO global_settings (key, value, updated_at) "
                        "VALUES ('alert.external_api', %s, NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                        (json.dumps({"error": str(exc), "agent": self._agent_name,
                                     "time": time.strftime("%Y-%m-%d %H:%M:%S")}),) * 2)
                c.commit()
                c.close()
            except Exception:
                pass
            return ("Das kann ich gerade leider nicht beantworten. "
                    "Bitte versuchen Sie es in ein paar Minuten erneut.")

        self._audit_log(
            session_id, "RESPONSE",
            f"model={self._model} response_len={len(raw_response)}",
            raw_response,
        )

        # Deanonymize response
        response = vault.deanonymize(raw_response, session_id)

        logger.info(f"[Hybrid] Response: {len(response)} chars (session={session_id[:12]})")
        return response

    def _audit_log(self, session_id: str, event: str, summary: str, payload: str = ""):
        """Append to the agent's external API audit log for GDPR/GoBD compliance.

        Logs the COMPLETE anonymized payload — no truncation. This file is the
        legally binding proof of exactly what data left (and entered) the server.

        Event types: SYSTEM_PROMPT, REQUEST, RESPONSE, ERROR
        """
        from datetime import datetime, timezone
        audit_dir = self.workspace_path(self._agent_name)
        audit_file = audit_dir / "external_api_audit.log"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{self._agent_name}] session={session_id[:12]} {event} | {summary}"
        if payload:
            # FULL payload — never truncate. Data is already anonymized by PII Vault.
            # For multiline payloads, indent continuation lines for log parsability.
            indented = payload.replace("\n", "\n  | ")
            line += f"\n  PAYLOAD:\n  | {indented}\n  END_PAYLOAD"
        try:
            with audit_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    async def _call_api(self, prompt: str) -> str:
        """Auto-detect key type and call the right API.

        Priority: Anthropic direct (sk-ant-*) → OpenRouter (sk-or-*) → OpenAI.
        """
        openrouter_key = self._secret("OPENROUTER_API_KEY")
        openai_key = self._secret("OPENAI_API_KEY")

        # Auto-detect: Anthropic direct key stored as OPENROUTER_API_KEY
        if openrouter_key.startswith("sk-ant-"):
            return await self._call_anthropic(prompt, openrouter_key)
        elif openrouter_key:
            return await self._call_openrouter(prompt, openrouter_key)
        elif openai_key:
            return await self._call_openai(prompt, openai_key)
        else:
            return "Fehler: Kein API-Key fuer externe LLMs konfiguriert."

    async def _call_anthropic(self, prompt: str, api_key: str) -> str:
        """Call Anthropic Messages API directly (sk-ant-* keys)."""
        import httpx
        model = self._model
        if "/" in model:
            model = model.split("/", 1)[1]
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "system": _EXTERNAL_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    async def analyze_image(self, image_path: str, question: str = "") -> str:
        """CR-207: Send an image to the external API for analysis (OCR, description, extraction).

        Uses Claude Vision to process photos, invoices, documents, screenshots etc.
        The image is sent as base64-encoded content block.
        """
        import base64
        import httpx
        from pathlib import Path

        img_path = Path(image_path)
        if not img_path.exists():
            # Try public/ subfolder
            public_path = img_path.parent / "public" / img_path.name
            if public_path.exists():
                img_path = public_path
            else:
                return f"Fehler: Bilddatei nicht gefunden: {image_path}"

        # Determine MIME type
        suffix = img_path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        media_type = mime_map.get(suffix, "image/jpeg")

        # Read and encode
        img_data = img_path.read_bytes()
        if len(img_data) > 20 * 1024 * 1024:  # 20 MB
            return "Fehler: Bild zu gross (max 20 MB)"
        b64_data = base64.standard_b64encode(img_data).decode("utf-8")

        # Build prompt
        default_q = ("Analysiere dieses Bild. Extrahiere alle sichtbaren Textinhalte (OCR). "
                     "Beschreibe was du siehst. Wenn es ein Dokument ist (Rechnung, Lieferschein, "
                     "Formular), extrahiere die strukturierten Daten als Tabelle.")
        user_question = question.strip() if question.strip() else default_q

        # Get API key
        openrouter_key = self._secret("OPENROUTER_API_KEY")
        if not openrouter_key.startswith("sk-ant-"):
            return "Fehler: Bildanalyse benoetigt Anthropic API Key (sk-ant-*)"

        model = self._model
        if "/" in model:
            model = model.split("/", 1)[1]

        # Audit
        session_id = os.urandom(6).hex()
        self._audit_log(session_id, "IMAGE_REQUEST",
                        f"file={img_path.name} size={len(img_data)} question={user_question[:100]}")

        try:
            timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": openrouter_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 4096,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64_data,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": user_question,
                                },
                            ],
                        }],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                result = data["content"][0]["text"]

            self._audit_log(session_id, "IMAGE_RESPONSE",
                            f"model={model} response_len={len(result)}", result)
            logger.info(f"[Hybrid] Image analysis: {img_path.name} → {len(result)} chars")
            return result

        except Exception as exc:
            self._audit_log(session_id, "IMAGE_ERROR", str(exc))
            logger.error(f"[Hybrid] Image analysis failed: {exc}")
            return f"Bildanalyse fehlgeschlagen: {exc}"

    async def _call_openrouter(self, prompt: str, api_key: str) -> str:
        import httpx
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://aimos.local",
                    "X-Title": "AIMOS Hybrid Reasoning",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _EXTERNAL_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _call_openai(self, prompt: str, api_key: str) -> str:
        import httpx
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": _EXTERNAL_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
