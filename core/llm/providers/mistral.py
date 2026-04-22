"""CR-283: Mistral AI provider for the LLM router.

Translates the AIMOS-internal payload format (Ollama-compatible) to Mistral's
chat completion API and back. Returns the same dict shape `_llm_chat` returns:
{"content": str, "tool_calls": list, "in_tokens": int, "out_tokens": int}.
"""
import asyncio
import json
import logging
import os
import random

import httpx

from ..cost import calc_cost_usd
from .base import ProviderBase, ProviderInfo, ProviderStatus

_MISTRAL_BASE = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
# Retry-bare HTTP-Codes:
# 408 = Request Timeout, 425 = Too Early
# 429 = Rate Limit
# 500/502/503/504 = Server-Fehler
# 520-524 = Cloudflare-Fehler (Mistral nutzt CF)
# 529 = Overloaded (Anthropic-/Mistral-Shared-Code)
_RETRY_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
_MAX_RETRIES = 4  # Erhöht von 2 — bei Cloudflare-Hiccups oft 1-2 schnelle Retries nötig


def _backoff_sleep_s(attempt: int) -> float:
    """§173 Stufe 1: Exponential-Backoff mit Jitter (2026-04-15)."""
    base = 2 ** attempt
    jitter = base * random.uniform(-0.3, 0.3)
    return max(0.5, base + jitter)


class MistralProviderError(RuntimeError):
    pass


async def call(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    stop: list[str] | None = None,
    timeout_s: float = 90.0,  # §134: reduced from 180s — triggers Anthropic fallback faster
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    response_format: dict | None = None,
    random_seed: int | None = None,
    safe_prompt: bool = False,
) -> dict:
    """Call Mistral chat completion. Returns normalized AIMOS dict.

    Args:
        model: e.g. "mistral-small-latest", "mistral-large-latest", "magistral-small-latest"
        messages: list of {"role", "content"} dicts (system/user/assistant/tool)
        tools: optional OpenAI-compatible tool schema list
        temperature: sampling temperature (0–1.5, but Mistral recommends 0–0.7)
        max_tokens: cap on output tokens
        stop: optional stop sequences (str or list[str])
        timeout_s: HTTP timeout
        api_key: explicit override (else env MISTRAL_API_KEY)
        reasoning_effort: "high" | "none" for reasoning models like Magistral
        response_format: optional structured-output schema (e.g. {"type": "json_object"})
        random_seed: deterministic sampling seed
        safe_prompt: inject Mistral's standard safety prompt
    """
    key = api_key or os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise MistralProviderError("MISTRAL_API_KEY not set in environment")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": _convert_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = _convert_tools(tools)
        payload["tool_choice"] = "auto"
    if stop:
        payload["stop"] = stop
    # Sektion 95 / Phase A+B: As of 2026-04-08, Mistral's `reasoning_effort`
    # parameter is ONLY accepted by `mistral-small-latest` (adjustable
    # reasoning). The native reasoning models `magistral-small-latest` and
    # `magistral-medium-latest` always reason and reject the parameter
    # explicitly with HTTP 400 "reasoning_effort is not enabled for this
    # model". So we whitelist the model AND only forward "high" — anything
    # else is silently dropped (caller intent: "do not send the parameter").
    _REASONING_EFFORT_MODELS = {"mistral-small-latest"}
    if reasoning_effort == "high" and model in _REASONING_EFFORT_MODELS:
        payload["reasoning_effort"] = "high"
    # Mistral rejects response_format when tools are present (HTTP 400:
    # "Cannot use json response type with tools").
    if response_format and not tools:
        payload["response_format"] = response_format
    if random_seed is not None:
        payload["random_seed"] = random_seed
    if safe_prompt:
        payload["safe_prompt"] = True

    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(
                    f"{_MISTRAL_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code in _RETRY_CODES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                resp.raise_for_status()
                data = resp.json()
                return _normalize_response(data, model)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRY_CODES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                raise MistralProviderError(
                    f"Mistral HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except httpx.RequestError as exc:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff_sleep_s(attempt))
                    continue
                raise MistralProviderError(f"Mistral connection error: {exc}") from exc
    raise MistralProviderError("Mistral retry budget exhausted")


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Convert to Mistral message format (OpenAI-compatible with tool extensions).

    Sanitizes message ordering for Mistral's strict requirements:
    - Tool messages must follow assistant with tool_calls (Mistral 3230 part 1)
    - Assistant messages need content OR tool_calls (Mistral 3240)
    - Number of tool_calls must match number of subsequent tool responses
      (Mistral 3230 part 2: "Not the same number of function calls and responses")
    """
    # Pass 1: standard conversion + filter orphaned/empty messages
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "tool":
            if out and out[-1].get("role") == "assistant" and out[-1].get("tool_calls"):
                out.append({
                    "role": "tool",
                    "name": m.get("name", "unknown"),
                    "content": content if isinstance(content, str) else json.dumps(content),
                    "tool_call_id": m.get("tool_call_id", ""),
                })
            # else: drop orphaned tool message
        elif role == "assistant" and m.get("tool_calls"):
            msg = {"role": "assistant", "content": content, "tool_calls": m["tool_calls"]}
            out.append(msg)
        elif role == "assistant":
            if not content or (isinstance(content, str) and not content.strip()):
                continue
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": role, "content": content})

    # Pass 2: ensure tool_call/response counts match.
    # For each assistant with N tool_calls, the next N messages must be tool responses.
    # If counts don't match, drop the assistant.tool_calls message AND any orphaned tools.
    sanitized = []
    i = 0
    while i < len(out):
        msg = out[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            n_calls = len(msg["tool_calls"])
            # Count consecutive tool responses immediately after
            j = i + 1
            n_responses = 0
            while j < len(out) and out[j].get("role") == "tool":
                n_responses += 1
                j += 1
            if n_responses == n_calls:
                # Valid pair — keep all
                sanitized.append(msg)
                sanitized.extend(out[i+1:i+1+n_responses])
                i = j
            else:
                # Mismatch — convert assistant to plain message (drop tool_calls)
                # and skip all the orphaned tool responses
                if msg.get("content") and msg["content"].strip():
                    sanitized.append({"role": "assistant", "content": msg["content"]})
                # Skip orphaned tool responses
                i = j
        else:
            sanitized.append(msg)
            i += 1

    return sanitized


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Convert AIMOS tool schemas (Ollama style) to Mistral (OpenAI style).

    Both formats are nearly identical: {type: function, function: {name, description, parameters}}.
    AIMOS already stores them in this shape, so this is largely a pass-through.
    """
    out = []
    for t in tools:
        if "function" in t:
            out.append({"type": "function", "function": t["function"]})
        elif "name" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            })
    return out


def _normalize_response(data: dict, model: str) -> dict:
    """Translate Mistral response to AIMOS dict shape."""
    import logging as _log
    _logger = _log.getLogger("AIMOS.llm.mistral")
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})

    # H-1: finish_reason auswerten und problematische Fälle loggen
    finish_reason = choice.get("finish_reason", "stop")
    if finish_reason == "length":
        _logger.warning(
            "[%s] finish_reason=length — Output abgeschnitten bei max_tokens. "
            "Output ist vermutlich unvollständig.",
            model,
        )
    elif finish_reason == "content_filter":
        _logger.error(
            "[%s] finish_reason=content_filter — Mistral Safety-Filter hat geblockt. "
            "Kein verwertbarer Output.",
            model,
        )
    elif finish_reason not in ("stop", "tool_calls", None):
        _logger.warning("[%s] finish_reason=%s (unerwartet)", model, finish_reason)
    raw_content = msg.get("content")
    # Per OpenAPI spec, content can be string, null, or list[ContentChunk] for multimodal.
    if raw_content is None:
        content = ""
    elif isinstance(raw_content, list):
        # Concatenate text chunks; ignore non-text chunks (image_url, audio, etc.)
        content = "".join(
            c.get("text", "") for c in raw_content
            if isinstance(c, dict) and c.get("type") in (None, "text")
        )
    else:
        content = str(raw_content)

    # Tool calls in Mistral format: list of {id, type, function: {name, arguments}}
    raw_tcs = msg.get("tool_calls") or []
    tool_calls = []
    for tc in raw_tcs:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {"_raw": args_raw}
        else:
            args = args_raw
        tool_calls.append({
            "id": tc.get("id", ""),
            "function": {
                "name": fn.get("name", ""),
                "arguments": args,
            },
        })

    usage = data.get("usage", {})
    in_tokens = usage.get("prompt_tokens", 0)
    out_tokens = usage.get("completion_tokens", 0)
    cost_usd = calc_cost_usd(model, in_tokens, out_tokens)

    return {
        "content": content,
        "tool_calls": tool_calls,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": cost_usd,
        "provider": "mistral",
        "model": model,
        "finish_reason": finish_reason,
    }


# ── SW1-2: ProviderBase Adapter ──────────────────────────────────────────

_log = logging.getLogger("AIMOS.llm.mistral")


class MistralProvider(ProviderBase):
    """Thin adapter wrapping the module-level call() for the Switchboard."""

    _INFO = ProviderInfo(
        name="mistral",
        is_local=False,
        supports_tools=True,
        supports_streaming=False,
        supports_response_format=True,
        supports_reasoning_effort=True,
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
        key = os.environ.get("MISTRAL_API_KEY")
        if not key:
            return ProviderStatus.OFFLINE
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    f"{_MISTRAL_BASE}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code == 200:
                    return ProviderStatus.HEALTHY
                return ProviderStatus.DEGRADED
        except Exception as exc:
            _log.warning(f"[health] Mistral health check failed: {exc}")
            return ProviderStatus.OFFLINE
