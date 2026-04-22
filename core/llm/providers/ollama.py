"""SW1-4: Local/Self-Hosted LLM Provider (Ollama, SGLang, vLLM).

Used as last-resort fallback when all API providers are down (degraded mode),
or as primary when running on rented GPU hardware (vast.ai, runpod).

Supports two backends:
  - "ollama": Ollama REST API (/api/chat, /api/tags)
  - "openai": OpenAI-compatible API (/v1/chat/completions, /v1/models)
              Used by SGLang, vLLM, and other inference servers.

The backend can be local (localhost) or remote (GPU server in the network).
is_local reflects whether an API key is needed, not physical location.

Design constraints:
  - max_concurrent=1 (GPU lock — one request at a time per server)
  - Tool calls are unreliable on most open models → degraded flag
  - Auto-Discovery via /api/tags (Ollama) or /v1/models (OpenAI-compat)
  - Cost is 0.0 for self-hosted (electricity/GPU rental tracked elsewhere)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

import httpx

from .base import ProviderBase, ProviderInfo, ProviderStatus

log = logging.getLogger("AIMOS.llm.local_llm")

_DEFAULT_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL",
    os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
_DEFAULT_BACKEND = os.environ.get("LOCAL_LLM_BACKEND", "ollama")


class LocalLLMProvider(ProviderBase):
    """Provider for self-hosted LLMs (Ollama, SGLang, vLLM).

    Args:
        base_url: Server URL (e.g. "http://localhost:11434" or "http://gpu-server:8000")
        backend: "ollama" or "openai" (SGLang/vLLM use OpenAI-compatible API)
        name: Registry name (default: "ollama" for backwards compat)
        api_key: Optional API key (for remote servers with auth)
    """

    def __init__(
        self,
        base_url: str | None = None,
        backend: Literal["ollama", "openai"] = None,
        name: str = "ollama",
        api_key: str | None = None,
    ):
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.backend = backend or _DEFAULT_BACKEND
        self._name = name
        self._api_key = api_key or os.environ.get("LOCAL_LLM_API_KEY")
        self._available_models: list[str] = []
        self._gpu_lock = asyncio.Semaphore(1)
        self._info = ProviderInfo(
            name=name,
            is_local=(self._api_key is None),  # local if no auth needed
            supports_tools=False,       # unreliable on most open models
            supports_streaming=False,
            supports_response_format=(self.backend == "openai"),
            supports_reasoning_effort=False,
            max_concurrent=1,           # GPU lock
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
        timeout_s: float = 120.0,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
    ) -> dict:
        """Call local/self-hosted LLM. Routes to correct backend API."""
        if self.backend == "openai":
            return await self._call_openai(
                model=model, messages=messages, tools=tools,
                temperature=temperature, max_tokens=max_tokens,
                stop=stop, timeout_s=timeout_s,
                api_key=api_key or self._api_key,
                response_format=response_format,
            )
        return await self._call_ollama(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stop=stop, timeout_s=timeout_s,
        )

    async def _call_ollama(
        self, *, model, messages, temperature, max_tokens, stop, timeout_s,
    ) -> dict:
        """Ollama-native API: POST /api/chat."""
        payload: dict = {
            "model": model,
            "messages": _convert_messages_ollama(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if stop:
            payload["options"]["stop"] = stop

        timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
        async with self._gpu_lock:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat", json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

        return _normalize_ollama(data, model, self._name)

    async def _call_openai(
        self, *, model, messages, tools, temperature, max_tokens, stop,
        timeout_s, api_key, response_format,
    ) -> dict:
        """OpenAI-compatible API (SGLang/vLLM): POST /v1/chat/completions."""
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict = {
            "model": model,
            "messages": messages,  # OpenAI format, no conversion needed
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if response_format:
            payload["response_format"] = response_format

        timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
        async with self._gpu_lock:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers=headers, json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

        return _normalize_openai(data, model, self._name)

    def info(self) -> ProviderInfo:
        return self._info

    async def health_check(self) -> ProviderStatus:
        """Check server availability and refresh model list."""
        try:
            if self.backend == "openai":
                return await self._health_openai()
            return await self._health_ollama()
        except Exception as exc:
            log.debug(f"[health] {self._name} offline: {exc}")
            self._available_models = []
            return ProviderStatus.OFFLINE

    async def _health_ollama(self) -> ProviderStatus:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                self._available_models = [m for m in models if m]
                if self._available_models:
                    log.info(
                        f"[health] {self._name} healthy, "
                        f"{len(self._available_models)} models: "
                        f"{', '.join(self._available_models[:5])}"
                    )
                    return ProviderStatus.HEALTHY
                log.warning(f"[health] {self._name} running but no models loaded")
                return ProviderStatus.DEGRADED
        return ProviderStatus.DEGRADED

    async def _health_openai(self) -> ProviderStatus:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(
                f"{self.base_url}/v1/models", headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                self._available_models = [m for m in models if m]
                if self._available_models:
                    log.info(
                        f"[health] {self._name} (openai-compat) healthy, "
                        f"{len(self._available_models)} models: "
                        f"{', '.join(self._available_models[:5])}"
                    )
                    return ProviderStatus.HEALTHY
                log.warning(f"[health] {self._name} running but no models")
                return ProviderStatus.DEGRADED
        return ProviderStatus.DEGRADED

    @property
    def available_models(self) -> list[str]:
        """Models discovered during last health_check."""
        return list(self._available_models)

    def has_model(self, model: str) -> bool:
        """Check if a specific model is available."""
        return model in self._available_models


# Keep OllamaProvider as alias for backwards compatibility
OllamaProvider = LocalLLMProvider


# ── Message conversion ───────────────────────────────────────────────────

def _convert_messages_ollama(messages: list[dict]) -> list[dict]:
    """Convert AIMOS messages to Ollama format.

    Ollama uses the same role/content format but doesn't support tool messages
    in the same way. Tool messages are converted to user context.
    """
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "tool":
            name = m.get("name", "tool")
            out.append({
                "role": "user",
                "content": f"[Tool result from {name}]: {content}",
            })
        elif role == "assistant" and m.get("tool_calls"):
            if content:
                out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": role, "content": content})
    return out


# ── Response normalization ───────────────────────────────────────────────

def _normalize_ollama(data: dict, model: str, provider_name: str) -> dict:
    """Translate Ollama response to AIMOS dict shape."""
    msg = data.get("message", {})
    content = msg.get("content", "")
    done_reason = data.get("done_reason", "stop")
    finish_reason = "length" if done_reason == "length" else "stop"
    in_tokens = data.get("prompt_eval_count", 0)
    out_tokens = data.get("eval_count", 0)

    return {
        "content": content,
        "tool_calls": [],
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": 0.0,
        "provider": provider_name,
        "model": model,
        "finish_reason": finish_reason,
        "degraded": True,
    }


def _normalize_openai(data: dict, model: str, provider_name: str) -> dict:
    """Translate OpenAI-compatible response (SGLang/vLLM) to AIMOS dict shape."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content", "") or ""
    finish_reason = choice.get("finish_reason", "stop") or "stop"
    usage = data.get("usage", {})
    in_tokens = usage.get("prompt_tokens", 0)
    out_tokens = usage.get("completion_tokens", 0)

    return {
        "content": content,
        "tool_calls": [],
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": 0.0,
        "provider": provider_name,
        "model": model,
        "finish_reason": finish_reason,
        "degraded": True,
    }
