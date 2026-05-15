"""Groq provider for the LLM Switchboard.

Groq nutzt OpenAI-kompatibles API mit Bearer-Auth. Sehr hohe Geschwindigkeit
(~1000 Tokens/s), ideal für Routine-LLM-Aufgaben und als Cross-Provider-
Fallback bei Mistral-Ausfällen.

Translates the AIMOS-internal payload format to Groq's chat completion API
and back. Returns the same dict shape as mistral.py / anthropic.py.

Hinzugefügt: 2026-05-05 (CR-299, Switchboard-Provider-Diversifikation).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random

import httpx

from ..cost import calc_cost_usd
from .base import ProviderBase, ProviderInfo, ProviderStatus

_GROQ_BASE = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# Retry-bare HTTP-Codes (gleicher Satz wie mistral)
_RETRY_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
_MAX_RETRIES = 3

log = logging.getLogger("AIMOS.llm.groq")


def _backoff_sleep_s(attempt: int) -> float:
    base = 2 ** attempt
    jitter = base * random.uniform(-0.3, 0.3)
    return max(0.5, base + jitter)


class GroqProviderError(RuntimeError):
    pass


async def call(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    stop: list[str] | None = None,
    timeout_s: float = 60.0,  # Groq ist schnell, niedriges Timeout OK
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    response_format: dict | None = None,
) -> dict:
    """Call Groq chat completion. Returns normalized AIMOS dict."""
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise GroqProviderError("GROQ_API_KEY not set in environment")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    if stop:
        payload["stop"] = stop
    if response_format:
        payload["response_format"] = response_format

    url = f"{_GROQ_BASE}/chat/completions"

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return _normalize(data, model)
            if resp.status_code in _RETRY_CODES and attempt < _MAX_RETRIES - 1:
                wait = _backoff_sleep_s(attempt)
                log.warning(
                    "[groq] %s — retry %d/%d after %.1fs",
                    resp.status_code, attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue
            raise GroqProviderError(
                f"Groq HTTP {resp.status_code}: {resp.text[:300]}"
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _backoff_sleep_s(attempt)
                log.warning(
                    "[groq] connection error — retry %d/%d after %.1fs: %s",
                    attempt + 1, _MAX_RETRIES, wait, exc,
                )
                await asyncio.sleep(wait)
                continue
            break

    raise GroqProviderError(
        f"Groq retry budget exhausted: {last_exc}"
    ) if last_exc else GroqProviderError("Groq retry budget exhausted")


def _normalize(data: dict, model: str) -> dict:
    """Convert Groq OpenAI-compatible response to AIMOS dict shape."""
    choices = data.get("choices", [])
    if not choices:
        return {
            "content": "", "tool_calls": [],
            "in_tokens": 0, "out_tokens": 0,
            "cost_usd": 0.0, "provider": "groq",
            "model": model, "finish_reason": "empty",
        }
    msg = choices[0].get("message", {})
    content = msg.get("content") or ""
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": args,
        })

    usage = data.get("usage", {})
    in_t = usage.get("prompt_tokens", 0)
    out_t = usage.get("completion_tokens", 0)
    cost = calc_cost_usd(model, in_t, out_t)

    return {
        "content": content,
        "tool_calls": tool_calls,
        "in_tokens": in_t,
        "out_tokens": out_t,
        "cost_usd": cost,
        "provider": "groq",
        "model": model,
        "finish_reason": choices[0].get("finish_reason", "stop"),
    }


class GroqProvider(ProviderBase):
    """Thin adapter wrapping the module-level call() for the Switchboard."""

    _INFO = ProviderInfo(
        name="groq",
        is_local=False,
        supports_tools=True,
        supports_streaming=False,
        supports_response_format=True,
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
        timeout_s: float = 60.0,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
    ) -> dict:
        return await call(
            model=model, messages=messages, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
            stop=stop, timeout_s=timeout_s, api_key=api_key,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
        )

    def info(self) -> ProviderInfo:
        return self._INFO

    async def health_check(self) -> ProviderStatus:
        """Lightweight health check via models endpoint."""
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            return ProviderStatus.OFFLINE
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{_GROQ_BASE}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code == 200:
                return ProviderStatus.HEALTHY
            if resp.status_code in _RETRY_CODES:
                return ProviderStatus.DEGRADED
            return ProviderStatus.OFFLINE
        except Exception:
            return ProviderStatus.OFFLINE
