"""
BraveSearchSkill – Web-Suche via Brave Search API (aiohttp).

Aktivierung: BRAVE_API_KEY Umgebungsvariable setzen.
Ohne API-Key bleibt der Skill inaktiv (is_available() == False).

Architekturprinzip: Reiner Capability-Skill.
Gibt Suchergebnisse als Markdown-Block in den System-Prompt ein.
Keine Agenten-Logik, kein State.
"""

import logging
import os

from .base import BaseSkill

logger = logging.getLogger("BraveSearchSkill")

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_COUNT    = 3
_REQUEST_TIMEOUT  = 10  # Sekunden


class BraveSearchSkill(BaseSkill):
    """Reichert den System-Prompt mit Brave-Web-Suchergebnissen an.

    Benötigt: aiohttp (pip install aiohttp)
    """

    name = "brave_search"
    display_name = "Web-Search (Brave)"

    def __init__(self, **kwargs):
        # Accept agent_name etc. but don't need them
        pass

    def is_available(self) -> bool:
        """True wenn BRAVE_API_KEY gesetzt ist."""
        return bool(os.getenv("BRAVE_API_KEY"))

    async def enrich_context(self, user_text: str) -> str:
        """Führt eine Brave-Suche durch und gibt Top-Ergebnisse als Kontext zurück.

        Args:
            user_text: Nutzer-Eingabe, wird direkt als Suchanfrage verwendet.

        Returns:
            Markdown-Block mit Suchergebnissen, oder leerer String bei Fehler.
        """
        if not self.is_available():
            return ""
        try:
            snippets = await self.search(user_text)
        except Exception as exc:
            logger.warning(f"Brave-Suche fehlgeschlagen: {exc}")
            return ""

        if not snippets:
            return ""

        lines = "\n".join(f"- {s}" for s in snippets)
        return f"## Aktuelle Web-Ergebnisse (Brave Search)\n{lines}"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "web_search",
                "description": "Sucht im Internet via Brave Search und gibt die Top-Ergebnisse zurueck.",
                "parameters": {
                    "query": {"type": "string", "description": "Suchanfrage", "required": True},
                    "count": {"type": "integer", "description": "Anzahl Ergebnisse", "default": 3},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            query = arguments.get("query", "")
            count = arguments.get("count", 3)
            if not query:
                return "Fehler: 'query' ist ein Pflichtfeld."
            logger.info(f"[TOOL] web_search: {query!r}")
            results = await self.search(query, count)
            if not results:
                return "Keine Suchergebnisse gefunden."
            return "\n".join(results)
        return f"Unbekanntes Tool: {tool_name}"

    async def search(self, query: str, count: int = _DEFAULT_COUNT) -> list[str]:
        """Sucht via Brave Search API und gibt formatierte Snippet-Strings zurück.

        Jedes Ergebnis: **Titel**: Snippet (URL)

        Args:
            query: Suchanfrage.
            count: Maximale Anzahl Ergebnisse (Standard: 3).

        Returns:
            Liste von formatierten Snippet-Strings. Leer wenn API nicht erreichbar.

        Raises:
            ImportError: Wenn aiohttp nicht installiert ist.
            aiohttp.ClientError: Bei HTTP-Fehlern (propagiert an enrich_context).
        """
        try:
            import aiohttp
        except ImportError:
            logger.error(
                "aiohttp ist nicht installiert. "
                "Bitte 'pip install aiohttp' ausführen."
            )
            return []

        api_key = os.getenv("BRAVE_API_KEY", "")
        if not api_key:
            return []

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        params = {
            "q": query,
            "count": count,
            "safesearch": "moderate",
            "search_lang": "de",
        }

        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _BRAVE_SEARCH_URL, headers=headers, params=params
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

        results = data.get("web", {}).get("results", [])
        snippets: list[str] = []

        for r in results[:count]:
            title   = (r.get("title")       or "").strip()
            snippet = (r.get("description") or "").strip()
            url     = (r.get("url")         or "").strip()

            if not snippet:
                # Fallback: extra_snippets Feld (Brave gibt manchmal hier mehr Info)
                extras  = r.get("extra_snippets") or []
                snippet = extras[0].strip() if extras else ""

            if title and snippet:
                snippets.append(f"**{title}**: {snippet} ({url})")
            elif title and url:
                snippets.append(f"**{title}** ({url})")

        logger.info(f"Brave Search: {len(snippets)} Ergebnis(se) für '{query[:60]}'")
        return snippets
