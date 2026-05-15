"""SW4: Capability Discovery + Model Aliases.

Static model registry (from cost.py prices) enriched with capability metadata.
Dynamic models from Ollama/SGLang are added via health_check discovery.

Model aliases map abstract roles (strong_reasoning, fast_cheap, code_gen) to
concrete models, with automatic fallback when the primary is unavailable
(circuit breaker open, key exhausted, etc.).

Usage:
    caps = Capabilities(switchboard)
    caps.refresh_from_providers()

    models = caps.available_models()
    provider, model = caps.resolve_alias("strong_reasoning")
    can = caps.can_serve("mistral-small-latest", max_tokens=32000)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("AIMOS.llm.capabilities")


@dataclass(frozen=True)
class ModelInfo:
    """Metadata about a specific model."""
    provider: str
    model_id: str
    context_window: int         # max input tokens
    max_output: int             # max output tokens
    supports_tools: bool
    supports_vision: bool
    supports_reasoning: bool
    cost_tier: Literal["free", "cheap", "medium", "expensive"]


# ── Static Model Registry ───────────────────────────────────────────────
# Verified against provider docs 2026-04-17.

_MODELS: dict[str, ModelInfo] = {
    # Mistral text models
    "mistral-small-latest": ModelInfo(
        provider="mistral", model_id="mistral-small-latest",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=True, cost_tier="cheap",
    ),
    "mistral-medium-latest": ModelInfo(
        provider="mistral", model_id="mistral-medium-latest",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="medium",
    ),
    "mistral-large-latest": ModelInfo(
        provider="mistral", model_id="mistral-large-latest",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="medium",
    ),
    # Reasoning specialists
    "magistral-small-latest": ModelInfo(
        provider="mistral", model_id="magistral-small-latest",
        context_window=128_000, max_output=40_000,
        supports_tools=False, supports_vision=False,
        supports_reasoning=True, cost_tier="medium",
    ),
    "magistral-medium-latest": ModelInfo(
        provider="mistral", model_id="magistral-medium-latest",
        context_window=128_000, max_output=40_000,
        supports_tools=False, supports_vision=False,
        supports_reasoning=True, cost_tier="expensive",
    ),
    # Coding specialists
    "devstral-latest": ModelInfo(
        provider="mistral", model_id="devstral-latest",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="medium",
    ),
    # devstral-small-2507 + devstral-medium-2507 → retiring 2026-05-31
    # (Mistral Deprecation 2026-05-04). Migrated to `devstral-latest` —
    # neuer Preis $0.4/$2.0 (4× / 6.7× teurer als devstral-small-2507).
    "codestral-latest": ModelInfo(
        provider="mistral", model_id="codestral-latest",
        context_window=256_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    # Vision
    "pixtral-large-latest": ModelInfo(
        provider="mistral", model_id="pixtral-large-latest",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=True,
        supports_reasoning=False, cost_tier="expensive",
    ),
    "pixtral-12b": ModelInfo(
        provider="mistral", model_id="pixtral-12b",
        context_window=128_000, max_output=32_000,
        supports_tools=False, supports_vision=True,
        supports_reasoning=False, cost_tier="cheap",
    ),
    # Anthropic
    "claude-sonnet-4-6": ModelInfo(
        provider="anthropic", model_id="claude-sonnet-4-6",
        context_window=200_000, max_output=8_192,
        supports_tools=True, supports_vision=True,
        supports_reasoning=True, cost_tier="expensive",
    ),
    "claude-opus-4-6": ModelInfo(
        provider="anthropic", model_id="claude-opus-4-6",
        context_window=200_000, max_output=32_000,
        supports_tools=True, supports_vision=True,
        supports_reasoning=True, cost_tier="expensive",
    ),
    "claude-haiku-4-5": ModelInfo(
        provider="anthropic", model_id="claude-haiku-4-5",
        context_window=200_000, max_output=8_192,
        supports_tools=True, supports_vision=True,
        supports_reasoning=False, cost_tier="medium",
    ),
    # ── Groq (CR-299, 2026-05-05) — Cross-Provider-Diversitaet ────────
    "llama-3.3-70b-versatile": ModelInfo(
        provider="groq", model_id="llama-3.3-70b-versatile",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    "llama-3.1-70b-versatile": ModelInfo(
        provider="groq", model_id="llama-3.1-70b-versatile",
        context_window=128_000, max_output=32_000,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    "llama-3.1-8b-instant": ModelInfo(
        provider="groq", model_id="llama-3.1-8b-instant",
        context_window=128_000, max_output=8_192,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    "mixtral-8x7b-32768": ModelInfo(
        provider="groq", model_id="mixtral-8x7b-32768",
        context_window=32_000, max_output=8_192,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    "deepseek-r1-distill-llama-70b": ModelInfo(
        provider="groq", model_id="deepseek-r1-distill-llama-70b",
        context_window=128_000, max_output=8_192,
        supports_tools=False, supports_vision=False,
        supports_reasoning=True, cost_tier="medium",
    ),
    # CR-301/Gemma4: Google Gemma 4 (Apache 2.0, released 2026-04-02)
    # Multimodal, exzellent fuer Edge. API-Verfuegbarkeit 2026-05 noch nicht;
    # Eintrag fuer baldigen Google AI Studio + Groq-Support
    "gemma-4-31b": ModelInfo(
        provider="groq", model_id="gemma-4-31b",
        context_window=128_000, max_output=16_384,
        supports_tools=True, supports_vision=True,
        supports_reasoning=True, cost_tier="cheap",
    ),
    "gemma-4-26b-moe": ModelInfo(
        provider="groq", model_id="gemma-4-26b-moe",
        context_window=128_000, max_output=8_192,
        supports_tools=True, supports_vision=True,
        supports_reasoning=False, cost_tier="cheap",
    ),
    # ── DeepSeek (CR-299, 2026-05-05) — guenstig, Code-Spezialist ──────
    "deepseek-chat": ModelInfo(
        provider="deepseek", model_id="deepseek-chat",
        context_window=128_000, max_output=8_192,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    "deepseek-reasoner": ModelInfo(
        provider="deepseek", model_id="deepseek-reasoner",
        context_window=128_000, max_output=8_192,
        supports_tools=False, supports_vision=False,
        supports_reasoning=True, cost_tier="cheap",
    ),
    "deepseek-coder": ModelInfo(
        provider="deepseek", model_id="deepseek-coder",
        context_window=128_000, max_output=8_192,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="cheap",
    ),
    # ── Lokale Modelle (Ollama/SGLang) ─────────────────────────────────
    # §193: Target-LLM-Awareness — Fabrik muss wissen was das Ziel-LLM kann
    "qwen3.5:32b": ModelInfo(
        provider="ollama", model_id="qwen3.5:32b",
        context_window=32_000, max_output=4_096,
        supports_tools=True, supports_vision=False,
        supports_reasoning=True, cost_tier="free",
    ),
    "qwen3.5:27b": ModelInfo(
        provider="ollama", model_id="qwen3.5:27b",
        context_window=24_000, max_output=4_096,
        supports_tools=True, supports_vision=False,
        supports_reasoning=True, cost_tier="free",
    ),
    "qwen2.5:32b": ModelInfo(
        provider="ollama", model_id="qwen2.5:32b",
        context_window=16_000, max_output=4_096,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="free",
    ),
    "qwen2.5:14b": ModelInfo(
        provider="ollama", model_id="qwen2.5:14b",
        context_window=16_000, max_output=2_048,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="free",
    ),
    "qwen2.5:7b": ModelInfo(
        provider="ollama", model_id="qwen2.5:7b",
        context_window=8_000, max_output=2_048,
        supports_tools=False, supports_vision=False,
        supports_reasoning=False, cost_tier="free",
    ),
    "mistral-small:22b": ModelInfo(
        provider="ollama", model_id="mistral-small:22b",
        context_window=32_000, max_output=4_096,
        supports_tools=True, supports_vision=False,
        supports_reasoning=False, cost_tier="free",
    ),
}


@dataclass(frozen=True)
class TargetLLMProfile:
    """§193: Profil das die Fabrik beim Build berücksichtigen muss.

    Bestimmt Prompt-Strategie, Tool-Design und Phase-Struktur.
    """
    model_id: str
    max_prompt_tokens: int      # Max Tokens für System-Prompt (Context - History - Tools)
    tool_calling_quality: Literal["native", "good", "fragile", "none"]
    recommended_phases: int     # 6 für starke LLMs, 3 für schwache
    language_quality_de: Literal["excellent", "good", "poor"]
    max_concurrent_tools: int   # Wie viele Tools gleichzeitig angeboten werden können
    needs_english_prompt: bool  # True wenn DE-Prompt zu schlecht performt


def get_target_profile(model_id: str) -> TargetLLMProfile:
    """Erstellt ein Target-LLM-Profil für die Fabrik.

    Wird von fab3_design genutzt um Prompt und Config an das Ziel-LLM anzupassen.
    """
    info = _MODELS.get(model_id)
    if not info:
        # Fallback: konservatives Profil
        return TargetLLMProfile(
            model_id=model_id,
            max_prompt_tokens=3000,
            tool_calling_quality="fragile",
            recommended_phases=3,
            language_quality_de="poor",
            max_concurrent_tools=5,
            needs_english_prompt=True,
        )

    # Prompt-Budget: 30% des Kontextfensters für System-Prompt
    max_prompt = int(info.context_window * 0.3)

    # Tool-Calling-Qualität
    if info.provider in ("anthropic",):
        tc_quality = "native"
    elif info.provider == "mistral" and "large" in info.model_id:
        tc_quality = "native"
    elif info.provider == "mistral" and info.supports_tools:
        tc_quality = "good"
    elif info.provider == "ollama" and info.supports_tools:
        tc_quality = "fragile"
    elif not info.supports_tools:
        tc_quality = "none"
    else:
        tc_quality = "good"

    # Phasen-Empfehlung
    if info.context_window >= 64_000 and tc_quality in ("native", "good"):
        phases = 6  # Voller OODA-Zyklus
    elif info.context_window >= 16_000:
        phases = 4  # Kompakter Zyklus
    else:
        phases = 3  # Minimal: Observe → Decide → Act

    # Sprach-Qualität
    if info.provider in ("anthropic", "mistral"):
        de_quality = "excellent"
    elif "32b" in info.model_id or "27b" in info.model_id:
        de_quality = "good"
    else:
        de_quality = "poor"

    # Concurrent Tools
    if info.context_window >= 64_000:
        max_tools = 20
    elif info.context_window >= 16_000:
        max_tools = 10
    else:
        max_tools = 5

    return TargetLLMProfile(
        model_id=model_id,
        max_prompt_tokens=max_prompt,
        tool_calling_quality=tc_quality,
        recommended_phases=phases,
        language_quality_de=de_quality,
        max_concurrent_tools=max_tools,
        needs_english_prompt=(de_quality == "poor"),
    )


# ── Model Aliases ────────────────────────────────────────────────────────
# Ordered by preference. First available model is used.

# CR-299 (2026-05-05) — Cross-Provider-Diversitaet:
# Jeder Alias hat jetzt mindestens 3 verschiedene Provider in der Liste.
# Bei einem Provider-Ausfall (Mistral instabil etc.) greift automatisch
# der naechste verfuegbare. Vorher: Mistral-lastige Listen mit nur 1
# Anthropic-Stop, dann wieder Mistral.
_DEFAULT_ALIASES: dict[str, list[str]] = {
    "strong_reasoning": [
        "magistral-medium-latest",            # mistral, primary
        "claude-sonnet-4-6",                  # anthropic, robust
        "deepseek-reasoner",                  # deepseek, R1 — guenstig
        "llama-3.3-70b-versatile",            # groq, schnell
        "magistral-small-latest",             # mistral fallback
    ],
    "fast_cheap": [
        "mistral-small-latest",               # mistral, primary
        "llama-3.1-8b-instant",               # groq, sehr schnell+guenstig
        "deepseek-chat",                      # deepseek, V3
        "claude-haiku-4-5",                   # anthropic
    ],
    "code_gen": [
        "devstral-latest",                    # mistral, primary
        "deepseek-coder",                     # deepseek, Code-Spezialist
        "claude-sonnet-4-6",                  # anthropic, robust
        "codestral-latest",                   # mistral fallback
    ],
    "vision": [
        "pixtral-large-latest",               # mistral
        "claude-sonnet-4-6",                  # anthropic — robust bei Vision
        "pixtral-12b",                        # mistral fallback
    ],
    "balanced_worker": [
        # CR-299: neuer Alias fuer Routine-Aufgaben mit guter Tool-Calling-Qualitaet
        "mistral-medium-latest",              # mistral, primary
        "claude-haiku-4-5",                   # anthropic, robust
        "llama-3.3-70b-versatile",            # groq, schnell
        "deepseek-chat",                      # deepseek, guenstig
    ],
    "local_fallback": [
        "qwen3.5:32b",
        "qwen3.5:27b",
        "qwen2.5:32b",
    ],
}


class Capabilities:
    """Model discovery and alias routing with availability awareness.

    Args:
        switchboard: Switchboard instance (for circuit breaker status)
        aliases: Custom alias overrides (merged with defaults)
    """

    def __init__(self, switchboard=None, aliases: dict | None = None):
        self._switchboard = switchboard
        self._dynamic_models: dict[str, ModelInfo] = {}
        self._aliases = {**_DEFAULT_ALIASES, **(aliases or {})}

    def refresh_from_providers(self) -> None:
        """Discover models from local providers (Ollama/SGLang)."""
        from .providers.base import all_providers, get
        self._dynamic_models.clear()

        for name, provider in all_providers().items():
            if not getattr(provider.info(), "is_local", False):
                continue
            models = getattr(provider, "available_models", [])
            for model_id in models:
                self._dynamic_models[model_id] = ModelInfo(
                    provider=name,
                    model_id=model_id,
                    context_window=32_768,  # conservative default for local models
                    max_output=4_096,
                    supports_tools=False,
                    supports_vision=False,
                    supports_reasoning=False,
                    cost_tier="free",
                )

    def get_model(self, model_id: str) -> ModelInfo | None:
        """Get model info by ID. Checks static then dynamic registry."""
        return _MODELS.get(model_id) or self._dynamic_models.get(model_id)

    def available_models(self) -> list[ModelInfo]:
        """All currently available models (static + discovered).

        Filters out models whose provider has circuit breaker OPEN.
        """
        result = []
        for model in list(_MODELS.values()) + list(self._dynamic_models.values()):
            if self._is_provider_available(model.provider):
                result.append(model)
        return result

    def can_serve(self, model_id: str, max_tokens: int = 0) -> bool:
        """Check if a model is available and can serve the token requirement."""
        info = self.get_model(model_id)
        if info is None:
            return False
        if not self._is_provider_available(info.provider):
            return False
        if max_tokens > 0 and max_tokens > info.max_output:
            return False
        return True

    def resolve_alias(self, alias: str) -> tuple[str, str] | None:
        """Resolve an alias to (provider, model) of the best available model.

        Returns None if no model in the alias chain is available.
        """
        candidates = self._aliases.get(alias)
        if not candidates:
            return None

        for model_id in candidates:
            info = self.get_model(model_id)
            if info and self._is_provider_available(info.provider):
                return (info.provider, info.model_id)

        return None

    def _is_provider_available(self, provider: str) -> bool:
        """Check if a provider is usable (circuit breaker not OPEN)."""
        if not self._switchboard:
            return True  # no switchboard = assume all available
        cb = self._switchboard.get_circuit_breaker(provider)
        if cb and cb.status == "open":
            return False
        return True

    def list_aliases(self) -> dict[str, list[str]]:
        """Return all configured aliases."""
        return dict(self._aliases)
