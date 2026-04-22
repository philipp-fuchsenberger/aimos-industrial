"""CR-283: Anthropic Claude provider for the LLM router (fallback only).

Used as fallback when Mistral is unavailable, or for ADM.3 architecture-judgment
where Opus is justified. NOT a default for any agent — Mistral is primary per
the EU-data-residency strategy.
"""
import asyncio
import json
import logging
import os
import random

import httpx

from ..cost import calc_cost_usd
from .base import ProviderBase, ProviderInfo, ProviderStatus

_ANTHROPIC_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
_API_VERSION = "2023-06-01"
# §173 Stufe 1 (2026-04-15): 408 und 425 (Too Early) als transient behandeln,
# _MAX_RETRIES von 2 auf 4 erhoeht weil Anthropic heute stark flaky war.
_RETRY_CODES = {408, 425, 429, 500, 502, 503, 504, 529}
_MAX_RETRIES = 4


def _backoff_sleep_s(attempt: int) -> float:
    """§173 Stufe 1: Exponential-Backoff mit Jitter.

    base = 2 ** attempt seconds; +/- 30% Jitter vermeidet Herd-Problem bei parallelen Retries.
    attempt 0 → ~1s, 1 → ~2s, 2 → ~4s, 3 → ~8s.
    """
    base = 2 ** attempt
    jitter = base * random.uniform(-0.3, 0.3)
    return max(0.5, base + jitter)


class AnthropicProviderError(RuntimeError):
    pass


async def call(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    stop: list[str] | None = None,
    timeout_s: float = 180.0,
    api_key: str | None = None,
) -> dict:
    """Call Anthropic Messages API. Returns normalized AIMOS dict."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AnthropicProviderError("ANTHROPIC_API_KEY not set in environment")

    headers = {
        "x-api-key": key,
        "anthropic-version": _API_VERSION,
        "Content-Type": "application/json",
    }

    # Anthropic separates system from messages
    system_text, conv_messages = _split_system(messages)

    payload: dict = {
        "model": model,
        "messages": conv_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = _convert_tools(tools)
    if stop:
        payload["stop_sequences"] = stop

    # Timeout direkt vom Aufrufer uebernehmen (kein Kapping mehr).
    # Pipeline-Agents koennen llm_timeout_s=180 setzen fuer grosse Inputs.
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(
                    f"{_ANTHROPIC_BASE}/messages",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code in _RETRY_CODES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                resp.raise_for_status()
                return _normalize_response(resp.json(), model)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRY_CODES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                raise AnthropicProviderError(
                    f"Anthropic HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except httpx.RequestError as exc:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                raise AnthropicProviderError(f"Anthropic connection error: {exc}") from exc
    raise AnthropicProviderError("Anthropic retry budget exhausted")


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts = []
    conv = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            content = m.get("content", "")
            if isinstance(content, str):
                system_parts.append(content)
        else:
            conv.append({"role": role, "content": m.get("content", "")})
    return ("\n\n".join(system_parts), conv)


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Anthropic uses {name, description, input_schema} instead of {function: {...}}."""
    out = []
    for t in tools:
        fn = t.get("function") or t
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _normalize_response(data: dict, model: str) -> dict:
    """Translate Anthropic response to AIMOS dict shape."""
    content_blocks = data.get("content", [])
    text_parts = []
    tool_calls = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "function": {
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                },
            })

    usage = data.get("usage", {})
    in_tokens = usage.get("input_tokens", 0)
    out_tokens = usage.get("output_tokens", 0)
    cost_usd = calc_cost_usd(model, in_tokens, out_tokens)

    # Map Anthropic stop_reason to OpenAI-compatible finish_reason
    stop_reason = data.get("stop_reason", "end_turn")
    finish_reason_map = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    finish_reason = finish_reason_map.get(stop_reason, stop_reason)

    return {
        "content": "".join(text_parts),
        "tool_calls": tool_calls,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": cost_usd,
        "provider": "anthropic",
        "model": model,
        "finish_reason": finish_reason,
    }


# ── SW1-2: ProviderBase Adapter ──────────────────────────────────────────

_log = logging.getLogger("AIMOS.llm.anthropic")


class AnthropicProvider(ProviderBase):
    """Thin adapter wrapping the module-level call() for the Switchboard."""

    _INFO = ProviderInfo(
        name="anthropic",
        is_local=False,
        supports_tools=True,
        supports_streaming=False,
        supports_response_format=False,  # JSON-Hint wird im Router injiziert
        supports_reasoning_effort=False,
        max_concurrent=None,
    )

    async def call(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        timeout_s: float = 90.0,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
    ) -> dict:
        # Anthropic braucht response_format als System-Hint (wie router.py Z.313-324)
        _msgs = messages
        if response_format and response_format.get("type") in ("json_object", "json_schema"):
            _msgs = list(messages)
            _json_hint = (
                "\n\nIMPORTANT: You MUST respond with valid JSON only. "
                "No markdown, no explanation, no code fences — just "
                "the raw JSON object."
            )
            if _msgs and _msgs[0].get("role") == "system":
                _msgs[0] = {**_msgs[0], "content": _msgs[0]["content"] + _json_hint}
            else:
                _msgs.insert(0, {"role": "system", "content": _json_hint.strip()})
        return await call(
            model=model, messages=_msgs, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
            stop=stop, timeout_s=timeout_s, api_key=api_key,
        )

    def info(self) -> ProviderInfo:
        return self._INFO

    async def health_check(self) -> ProviderStatus:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return ProviderStatus.OFFLINE
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    f"{_ANTHROPIC_BASE}/messages",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": _API_VERSION,
                    },
                )
                # Anthropic gibt 405 auf GET /messages — aber die Verbindung steht
                if resp.status_code in (200, 405):
                    return ProviderStatus.HEALTHY
                return ProviderStatus.DEGRADED
        except Exception as exc:
            _log.warning(f"[health] Anthropic health check failed: {exc}")
            return ProviderStatus.OFFLINE
