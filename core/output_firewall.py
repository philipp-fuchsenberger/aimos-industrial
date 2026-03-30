"""
AIMOS Output Firewall — extracted from agent_base.py (CR-221)
==============================================================
CJK filter, thought-leak filter, phantom-action detection,
loop detection with external LLM escalation, confidence check.

Used by AIMOSAgent via OutputFirewallMixin.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from core.config import Config

_log = logging.getLogger("AIMOS.OutputFirewall")

# ── Output Firewall (ported from v3.8.2 chat.py) ─────────────────────────────

# CJK Unified + Extension A + Compat Ideographs + Symbols/Punctuation + Fullwidth Forms
_CJK_RE = re.compile(
    r"[\u3000-\u303f"        # CJK Symbols and Punctuation
    r"\u3400-\u4dbf"         # CJK Extension A
    r"\u4e00-\u9fff"         # CJK Unified Ideographs
    r"\uf900-\ufaff"         # CJK Compatibility Ideographs
    r"\uff00-\uffef]+"       # Fullwidth Forms
)
# CR-114b: Thought-leak filter — only remove system tag leaks, not entire sentences.
_THOUGHT_RE = re.compile(
    r"</?(?:rules|emergency[_a-z]*|system|instructions?|anweisungen?|system_core)>[^\n]*"
    r"|Gem[äa][ß]s? (?:meinen?|den) (?:Anweisungen|System-?[Pp]rompt)[^.!?\n]*[.!?\n]?",
    re.IGNORECASE | re.MULTILINE,
)
_FILTER_FALLBACK = ""  # Empty — caller handles fallback via external API or context
_CHINESE_STOP_TOKENS = ["，", "。", "！", "？", "、", "「", "」"]
STOP_SEQUENCES = ["</tool_call>", "<|im_start|>", "<|im_end|>"]


def clean_llm_response(text: str, tool_was_called: bool = False) -> str:
    """Strip CJK chars and thought-leaks. Returns fallback if result is empty."""
    if Config.CLEAN_CJK:
        text = _CJK_RE.sub("", text)
    text = _THOUGHT_RE.sub("", text)
    text = text.strip()
    if not text:
        return _FILTER_FALLBACK
    return text


class OutputFirewallMixin:
    """Mixin providing output sanitization, phantom-action detection,
    loop detection and confidence checks for AIMOSAgent."""

    # CR-114b: Phantom action claims → action keywords the agent might claim without doing
    _PHANTOM_PATTERNS = {
        "send_to_agent": re.compile(
            r'(?:habe ich (?:an|dem|weitergeleitet|kontaktiert|gesendet|informiert)|'
            r'I have (?:forwarded|contacted|sent|informed)|'
            r'(?:leite|sende|kontaktiere) ich (?:das |die |den )?(?:an |weiter)|'
            r'(?:werde|wird) (?:sich |)(?:mein Kollege|der Vertrieb|unser Innendienst)|'
            r'(?:angebot|offer) (?:wird|werde ich|fuer|für|for)[\w\s]*(?:erstellt|vorbereitet|gesendet|created|prepared|sent)|'
            r'(?:auftrag|anfrage)[\w\s]*(?:an|fuer|für)[\w\s]*(?:innendienst|vertrieb)|'
            r'(?:weiterleiten|weiterleite|weitergeleitet)[\w\s]*(?:innendienst|vertrieb)|'
            r'bearbeitung erfolgt[\w\s]*(?:innendienst|umgehend)|'
            r'(?:werde|ich) (?:das |die |den )?(?:angebot|anfrage)[\w\s]*(?:weiterleiten|erstellen lassen))',
            re.IGNORECASE),
        "remember": re.compile(
            r'(?:habe ich (?:notiert|gespeichert|gemerkt|vermerkt)|'
            r'I have (?:noted|saved|stored|remembered)|'
            r'(?:Ihre Daten|die Information) (?:wurde|habe ich) (?:gespeichert|notiert))',
            re.IGNORECASE),
        "send_email": re.compile(
            r'(?:habe ich (?:per |eine )?(?:E-?Mail|Mail) (?:gesendet|geschickt|versendet)|'
            r'I have (?:sent|emailed)|'
            r'(?:E-?Mail|Mail) (?:wurde|wird) (?:gesendet|verschickt))',
            re.IGNORECASE),
    }

    def _sanitize_reply(self, reply: str) -> str:
        """CR-161: Strip internal data patterns from agent replies before user delivery."""
        import re
        # Remove internal message prefixes
        reply = re.sub(r'\[Nachricht von \w+\]', '', reply)
        # CR-186: Also strip English variant
        reply = re.sub(r'\[Message from \w+\]', '', reply)
        # Remove vault placeholders that weren't de-anonymized
        reply = re.sub(r'__VAULT_\w+_\d+__', '[REDACTED]', reply)
        # Remove system prompt fragments (common patterns)
        reply = re.sub(r'TOOL_(?:START|OK|ERROR|RESULT)\b', '', reply)
        # Remove raw JSON tool outputs that leaked
        reply = re.sub(r'\{"tool_call_id":[^}]+\}', '', reply)
        # Remove raw tool-call fragments (XML and JSON variants)
        reply = re.sub(r'<tool_call>\s*\{[^}]*\}', '', reply, flags=re.DOTALL)
        reply = re.sub(r'\{"name"\s*:\s*"[^"]*"\s*,\s*"arguments"\s*:', '', reply)
        # Remove customer context hints that leaked
        reply = re.sub(r'\[Current customer:[^\]]*\]', '', reply)
        reply = re.sub(r'\[IMPORTANT: This conversation[^\]]*\]', '', reply)
        # Clean up multiple spaces/newlines from removals
        reply = re.sub(r'\n{3,}', '\n\n', reply)
        return reply.strip()

    async def _strip_phantom_actions(self, answer: str, tool_results: list[str]) -> str:
        """Detect phantom actions and attempt self-correction.

        If the agent claims an action but didn't call the tool:
        1. Try to force the tool call via a corrective think() round
        2. If successful -> keep the original answer (claim is now true)
        3. If still not called -> strip the false claim sentence

        Zero token overhead for detection. One extra LLM call only when phantom detected.
        """
        if not answer:
            return answer

        # Determine which tools were actually called
        called_tools = set()
        for tr in tool_results:
            for tool_name in ("send_to_agent", "remember", "recall", "send_email",
                              "send_telegram_message", "write_file", "read_file",
                              "search_in_file", "brave_search", "web_search"):
                if f"Tool '{tool_name}'" in tr or f"[Tool: {tool_name}]" in tr:
                    called_tools.add(tool_name)

        # Check each phantom pattern
        for tool_name, pattern in self._PHANTOM_PATTERNS.items():
            if tool_name not in called_tools and pattern.search(answer):
                self.logger.warning(
                    f"[CR-114b] Phantom action detected: '{tool_name}' claimed but not called. "
                    f"Attempting self-correction..."
                )
                self._audit("PHANTOM_ACTION", f"claimed={tool_name} tools_called={called_tools}")

                # Attempt self-correction: ask the LLM to actually do it
                correction_ok = await self._force_phantom_tool(tool_name, answer)

                if correction_ok:
                    self.logger.info(f"[CR-114b] Self-correction succeeded: '{tool_name}' now called")
                    return answer  # Keep original answer — claim is now fulfilled

                # Self-correction failed — strip the false claim
                self.logger.warning("[CR-114b] Self-correction failed — stripping claim")
                sentences = re.split(r'(?<=[.!?])\s+', answer)
                cleaned = [s for s in sentences if not pattern.search(s)]
                if len(cleaned) < len(sentences):
                    answer = " ".join(cleaned)

        return answer

    async def _force_phantom_tool(self, tool_name: str, original_answer: str) -> bool:
        """Try to force a missed tool call via a corrective LLM round.

        Returns True if the tool was actually called this time.
        """
        try:
            correction_prompt = (
                f"IMPORTANT: In your previous response you said you would use {tool_name}, "
                f"but you did NOT actually call it. Your response was:\n"
                f'"{original_answer[:300]}"\n\n'
                f"Now actually call {tool_name} with the appropriate parameters. "
                f"Do NOT generate any text — ONLY call the tool."
            )

            # Build minimal messages for correction
            messages = [
                {"role": "system", "content": self._CORE_SYSTEM_PROMPT + self._system_prompt},
                {"role": "user", "content": correction_prompt},
            ]

            ollama_tools = self._build_ollama_tools() if hasattr(self, '_build_ollama_tools') else None
            llm_response = await self._llm_chat(messages, tools=ollama_tools)

            # Check if tool was called
            native_tc = llm_response.get("tool_calls", [])
            tool_calls = []
            if native_tc:
                for tc in native_tc:
                    fn = tc.get("function", {})
                    tool_calls.append({"name": fn.get("name", ""), "arguments": fn.get("arguments", {})})

            if not tool_calls:
                # Also try text-based parsing
                tool_calls = self._parse_tool_calls(llm_response.get("content", ""))

            # Execute if the expected tool was called
            for tc in tool_calls:
                if tc.get("name") == tool_name:
                    result = await self._execute_tool(tc)
                    self.logger.info(f"[CR-114b] Forced {tool_name}: {str(result)[:100]}")
                    tool_msg = f"Tool '{tc.get('name')}' returned:\n{result}"
                    await self._persist_message("tool", tool_msg, {"tool": tc.get("name"), "forced": True})
                    return True

            return False

        except Exception as exc:
            self.logger.debug(f"[CR-114b] Force tool failed: {exc}")
            return False

    def _check_confidence(self, reply: str, tool_results: list[str]) -> str:
        """CR-159: Detect potential hallucination patterns and log warnings.

        Monitoring-only — logs but does not modify the reply.
        """
        # Patterns that suggest the agent is making claims it can't verify
        uncertain_patterns = [
            r'(?:ich glaube|I believe|I think|vermutlich|wahrscheinlich|möglicherweise|possibly|probably|if I recall)',
            r'(?:soweit ich weiß|as far as I know|meines Wissens|to my knowledge)',
            r'(?:das müsste|das sollte|that should be|it should be)',
        ]

        has_uncertainty = any(re.search(p, reply, re.IGNORECASE) for p in uncertain_patterns)

        # Check if agent quotes numbers/dates that weren't in any tool result
        numbers_in_reply = set(re.findall(r'\b\d{3,}\b', reply))
        numbers_in_tools = set()
        for tr in tool_results:
            numbers_in_tools.update(re.findall(r'\b\d{3,}\b', tr))

        unverified_numbers = numbers_in_reply - numbers_in_tools

        if has_uncertainty or len(unverified_numbers) > 3:
            self.logger.info(
                f"[{self.agent_name}] CR-159 confidence check: "
                f"uncertainty={has_uncertainty}, unverified_numbers={len(unverified_numbers)}"
            )

        return reply

    async def _check_loop_and_escalate(self, answer: str, user_message: str) -> str:
        """Detect if the local LLM is stuck in a loop and escalate to external API.

        Compares the current answer against the last 2 responses using word overlap.
        If the current answer is >60% similar to the previous one, escalate immediately.
        """
        # Track recent responses (sliding window of 2)
        self._recent_responses.append(answer[:200])
        if len(self._recent_responses) > 2:
            self._recent_responses.pop(0)

        if len(self._recent_responses) < 2:
            return answer  # need at least one previous response

        # Compute word-overlap similarity against the previous response
        current_words = set(answer.lower().split())
        if not current_words:
            return answer

        prev_words = set(self._recent_responses[0].lower().split())
        if not prev_words:
            return answer

        overlap = len(current_words & prev_words) / max(len(current_words), len(prev_words))
        # Short replies (helpdesk) naturally overlap more — use higher threshold
        _threshold = 0.85 if len(current_words) < 30 else 0.6
        if overlap <= _threshold:
            return answer  # not a loop

        # Loop detected — escalate to external LLM
        self.logger.warning(
            f"[{self.agent_name}] LOOP DETECTED: {overlap:.0%} overlap with previous response. Escalating to external LLM."
        )
        self._audit("LOOP_ESCALATION", f"user_msg={user_message[:100]}")

        if "ask_external" not in self._tools:
            self.logger.warning(f"[{self.agent_name}] ask_external not available — cannot escalate")
            return answer

        try:
            ext_result = await self._tools["ask_external"](
                question=user_message,
                context=f"Der lokale Agent konnte diese Frage nicht zufriedenstellend beantworten. "
                        f"Letzte Antwort war: {answer[:200]}",
            )
            self._recent_responses.clear()  # reset loop tracker
            self.logger.info(f"[{self.agent_name}] Loop resolved via external LLM ({len(ext_result)} chars)")
            return ext_result
        except Exception as exc:
            self.logger.error(f"[{self.agent_name}] External escalation failed: {exc}")
            return answer
