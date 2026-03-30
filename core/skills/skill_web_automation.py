"""
WebAutomationSkill – Browser-Automation via Playwright (headless Chromium).

Agenten koennen sich auf Webseiten einloggen, Inhalte extrahieren und
wiederkehrende Checks per Scheduler automatisieren.

Architektur:
  - Playwright headless Chromium (kein GUI, kein X11)
  - Credentials via Wizard oder update_credential (PII-Vault-geschuetzt im Log)
  - Generischer Login-Flow + konfigurierbare Site-Flows
  - Output: Strukturierter Text fuer den Agenten

Site-Flows:
  Site-Flows sind JSON-Konfigurationen die beschreiben wie eine Webseite
  navigiert wird. Sie werden in agents.config unter "web_flows" gespeichert.
  Format:
    {
      "flow_name": {
        "url": "https://portal.example.com/login",
        "steps": [
          {"action": "fill", "selector": "#username", "credential": "PORTAL_USER"},
          {"action": "fill", "selector": "#password", "credential": "PORTAL_PASS"},
          {"action": "click", "selector": "button[type=submit]"},
          {"action": "wait", "selector": ".dashboard"},
          {"action": "extract", "selector": ".content", "output": "text"}
        ]
      }
    }

Credentials:
  WEB_FLOW_* keys in env_secrets (agent-editable empfohlen).
  Credentials referenced in flows via "credential" key are resolved from env.
  NEVER logged in cleartext — PII Vault protects them.

Aktivierung:
  Skill 'web_automation' in der Agent-Konfiguration + mindestens ein web_flow.
"""

import json
import logging
import os
import re
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("WebAutomationSkill")

_TIMEOUT_MS = 30_000  # 30s per action
_MAX_EXTRACT_CHARS = 20_000


class WebAutomationSkill(BaseSkill):
    """Browser-Automation — Playwright headless Chromium."""

    name = "web_automation"
    display_name = "Web Automation (Browser)"

    def __init__(self, agent_name: str = "", agent_config: dict | None = None,
                 secrets: dict[str, str] | None = None, **kwargs):
        self._init_secrets(secrets)
        self._agent_name = agent_name
        cfg = agent_config or {}
        self._flows: dict = cfg.get("web_flows", {})

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "web_flows",
                "label": "Web Flows (JSON)",
                "type": "textarea",
                "placeholder": '{"caterer": {"url": "https://...", "steps": [...]}}',
                "hint": "JSON-Konfiguration fuer Login-Flows. Siehe docs/ARCHITECTURE.md.",
                "secret": False,
            },
        ]

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "web_login_and_extract",
                "description": (
                    "Fuehrt einen konfigurierten Web-Flow aus: Login auf einer Webseite, "
                    "navigiert und extrahiert Text. Parameter: flow_name (Name des Flows aus der Config)."
                ),
                "parameters": {
                    "flow_name": {"type": "string", "description": "Name des konfigurierten Flows", "required": True},
                },
            },
            {
                "name": "web_browse",
                "description": (
                    "Oeffnet eine URL im headless Browser und gibt den sichtbaren Text zurueck. "
                    "Kein Login — nur fuer oeffentliche Seiten."
                ),
                "parameters": {
                    "url": {"type": "string", "description": "Die URL", "required": True},
                    "selector": {"type": "string", "description": "Optionaler CSS-Selektor zum Extrahieren", "default": "body"},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        import asyncio
        if tool_name == "web_login_and_extract":
            flow_name = arguments.get("flow_name", "")
            if not flow_name:
                return "Fehler: 'flow_name' ist ein Pflichtfeld."
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._run_flow, flow_name)
        elif tool_name == "web_browse":
            url = arguments.get("url", "")
            selector = arguments.get("selector", "body")
            if not url:
                return "Fehler: 'url' ist ein Pflichtfeld."
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._browse, url, selector)
        return f"Unbekanntes Tool: {tool_name}"

    def _resolve_credential(self, key: str) -> str:
        """Resolve a credential key from secrets (CR-222) or environment. Never log the value."""
        val = self._secret(key)
        if not val:
            logger.warning(f"[WebAuto] Credential '{key}' not found in secrets/env")
        return val

    def _run_flow(self, flow_name: str) -> str:
        """Execute a named web flow from config."""
        flows = self._flows
        if isinstance(flows, str):
            try:
                flows = json.loads(flows)
            except json.JSONDecodeError:
                # Fallback: might be Python repr with single quotes
                import ast
                try:
                    flows = ast.literal_eval(flows)
                except (ValueError, SyntaxError) as exc:
                    return f"Fehler: web_flows ungueltig: {exc}"

        if flow_name not in flows:
            available = ", ".join(flows.keys()) if flows else "(keine)"
            return f"Flow '{flow_name}' nicht gefunden. Verfuegbar: {available}"

        flow = flows[flow_name]
        url = flow.get("url", "")
        steps = flow.get("steps", [])
        if not url or not steps:
            return f"Flow '{flow_name}' hat keine URL oder Steps."

        logger.info(f"[WebAuto] Running flow '{flow_name}': {url}")

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 AIMOS-Agent/4.2"
                )
                page = context.new_page()
                page.set_default_timeout(_TIMEOUT_MS)

                page.goto(url, wait_until="domcontentloaded")
                logger.info(f"[WebAuto] Loaded: {url}")

                extracted = []

                for i, step in enumerate(steps):
                    action = step.get("action", "")
                    selector = step.get("selector", "")

                    if action == "fill":
                        cred_key = step.get("credential", "")
                        value = step.get("value", "")
                        if cred_key:
                            value = self._resolve_credential(cred_key)
                        if not value:
                            return f"Step {i}: fill — kein Wert fuer '{selector}'"
                        page.fill(selector, value)
                        # Log without credential value (PII protection)
                        logger.info(f"[WebAuto] Step {i}: fill {selector} (credential: {cred_key or 'static'})")

                    elif action == "click":
                        page.click(selector)
                        logger.info(f"[WebAuto] Step {i}: click {selector}")

                    elif action == "wait":
                        page.wait_for_selector(selector, timeout=_TIMEOUT_MS)
                        logger.info(f"[WebAuto] Step {i}: wait {selector}")

                    elif action == "navigate":
                        target_url = step.get("url", "")
                        page.goto(target_url, wait_until="domcontentloaded")
                        logger.info(f"[WebAuto] Step {i}: navigate {target_url}")

                    elif action == "extract":
                        output_type = step.get("output", "text")
                        el = page.query_selector(selector)
                        if el:
                            if output_type == "html":
                                content = el.inner_html()[:_MAX_EXTRACT_CHARS]
                            else:
                                content = el.inner_text()[:_MAX_EXTRACT_CHARS]
                            label = step.get("label", f"extract_{i}")
                            extracted.append(f"[{label}]\n{content}")
                            logger.info(f"[WebAuto] Step {i}: extract {selector} → {len(content)} chars")
                        else:
                            extracted.append(f"[{selector}] — Element nicht gefunden")

                    elif action == "screenshot":
                        ws = self.workspace_path(self._agent_name)
                        filename = step.get("filename", f"screenshot_{flow_name}.png")
                        path = ws / filename
                        page.screenshot(path=str(path))
                        extracted.append(f"[Screenshot] {filename}")
                        logger.info(f"[WebAuto] Step {i}: screenshot → {path}")

                    elif action == "sleep":
                        import time
                        duration = min(int(step.get("seconds", 2)), 10)
                        time.sleep(duration)

                    else:
                        logger.warning(f"[WebAuto] Step {i}: unknown action '{action}'")

                browser.close()

            if extracted:
                return "\n\n".join(extracted)
            return f"Flow '{flow_name}' ausgefuehrt — keine Daten extrahiert."

        except Exception as exc:
            logger.error(f"[WebAuto] Flow '{flow_name}' failed: {exc}")
            return f"Browser-Fehler: {exc}"

    def _browse(self, url: str, selector: str = "body") -> str:
        """Simple browse: open URL, extract text from selector."""
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_default_timeout(_TIMEOUT_MS)
                page.goto(url, wait_until="domcontentloaded")

                el = page.query_selector(selector)
                if el:
                    text = el.inner_text()[:_MAX_EXTRACT_CHARS]
                else:
                    text = page.content()[:_MAX_EXTRACT_CHARS]

                title = page.title()
                browser.close()

                logger.info(f"[WebAuto] Browse: {url} → {len(text)} chars")
                return f"Seite: {title}\nURL: {url}\n\n{text}"

        except Exception as exc:
            logger.error(f"[WebAuto] Browse failed: {exc}")
            return f"Browser-Fehler: {exc}"
