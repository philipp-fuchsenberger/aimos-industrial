"""SW1-1: Provider Base Interface + Registry for LLM Switchboard.

Defines the abstract base class all LLM providers must implement, plus a
simple registry for provider lookup. Existing module-level providers
(mistral.py, anthropic.py) will be wrapped in thin adapter classes (SW1-2).

Design constraints (derived from existing code, 2026-04-17):
  - call() signature is the UNION of all provider parameters
  - Provider-specific params (reasoning_effort, response_format) are optional
  - Return dict matches the existing AIMOS shape (content, tool_calls, ...)
  - Each provider keeps its own retry/backoff logic internally
  - Registry is in-memory, no DB needed
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("AIMOS.llm.providers")


# ── Provider Status ──────────────────────────────────────────────────────

class ProviderStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"           # erhoehte Latenz oder Fehlerrate
    CIRCUIT_OPEN = "circuit_open"   # Circuit Breaker offen, keine Requests
    OFFLINE = "offline"             # nicht erreichbar


# ── Provider Metadata ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderInfo:
    """Static metadata about a provider. Set once at registration time."""
    name: str                       # "mistral", "anthropic", "ollama"
    is_local: bool = False          # True fuer Ollama/SGLang (kein API-Key noetig)
    supports_tools: bool = True
    supports_streaming: bool = False  # nicht verwendet in v0, reserviert
    supports_response_format: bool = False
    supports_reasoning_effort: bool = False
    max_concurrent: int | None = None  # None = unbegrenzt, 1 = GPU-Lock (Ollama)


# ── Abstract Provider ────────────────────────────────────────────────────

class ProviderBase(ABC):
    """Abstract base for all LLM providers.

    Each provider wraps a specific API (Mistral, Anthropic, Ollama) and
    normalizes the response to the AIMOS dict shape.
    """

    @abstractmethod
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
        # Provider-spezifisch (optional, Provider ignoriert was er nicht kennt):
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
    ) -> dict:
        """Execute an LLM call. Returns normalized AIMOS response dict.

        Required keys in return dict:
            content: str            - generated text
            tool_calls: list[dict]  - tool call requests (kann leer sein)
            in_tokens: int          - input token count
            out_tokens: int         - output token count
            cost_usd: float         - cost in USD (0.0 fuer lokale Modelle)
            provider: str           - provider name
            model: str              - model id used
            finish_reason: str      - "stop", "length", "tool_calls", etc.
        """
        ...

    @abstractmethod
    def info(self) -> ProviderInfo:
        """Return static provider metadata."""
        ...

    @abstractmethod
    async def health_check(self) -> ProviderStatus:
        """Lightweight health check (no token consumption).

        For API providers: HEAD request or minimal completion.
        For Ollama: GET /api/tags.
        """
        ...


# ── Provider Registry ────────────────────────────────────────────────────

_registry: dict[str, ProviderBase] = {}


def register(name: str, provider: ProviderBase) -> None:
    """Register a provider instance under a name."""
    if name in _registry:
        log.warning(f"[registry] overwriting provider '{name}'")
    _registry[name] = provider
    log.info(f"[registry] registered provider '{name}' ({type(provider).__name__})")


def get(name: str) -> ProviderBase:
    """Get a registered provider by name. Raises KeyError if not found."""
    try:
        return _registry[name]
    except KeyError:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise KeyError(
            f"Provider '{name}' not registered. Available: {available}"
        )


def all_providers() -> dict[str, ProviderBase]:
    """Return a copy of the full provider registry."""
    return dict(_registry)


def is_registered(name: str) -> bool:
    """Check if a provider is registered."""
    return name in _registry


def unregister(name: str) -> None:
    """Remove a provider from the registry. No-op if not found."""
    _registry.pop(name, None)


def clear_registry() -> None:
    """Remove all providers. Primarily for testing."""
    _registry.clear()
