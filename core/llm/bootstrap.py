"""LLM Switchboard Bootstrap — initializes providers + switchboard per process.

Called once during agent startup (agent_base.py _startup_sequence).
Safe to call multiple times (idempotent — skips if already initialized).

Usage:
    from core.llm.bootstrap import init_llm_switchboard
    await init_llm_switchboard(config)
"""
from __future__ import annotations

import logging

log = logging.getLogger("AIMOS.llm.bootstrap")

_initialized = False


async def init_llm_switchboard(
    config: dict | None = None,
    db_pool=None,
) -> None:
    """Register providers, create switchboard, wire into router.

    Args:
        config: Agent/system config dict. Switchboard reads from
                config.get("switchboard", {}).
        db_pool: asyncpg pool for key_pool + cost_tracker persistence.
                 If None, key pool uses env vars only, cost tracker is disabled.
    """
    global _initialized
    if _initialized:
        log.debug("[bootstrap] already initialized, skipping")
        return

    config = config or {}
    sb_config = config.get("switchboard", {})

    # 1. Register providers
    from .providers.base import register, is_registered
    from .providers.mistral import MistralProvider
    from .providers.anthropic import AnthropicProvider
    from .providers.ollama import LocalLLMProvider
    # CR-299 (2026-05-05): Cross-Provider-Diversitaet
    from .providers.groq import GroqProvider
    from .providers.deepseek import DeepSeekProvider
    import os as _os

    if not is_registered("mistral"):
        register("mistral", MistralProvider())

    if not is_registered("anthropic"):
        register("anthropic", AnthropicProvider())

    # CR-299: Groq (nur wenn API-Key vorhanden)
    if not is_registered("groq") and _os.environ.get("GROQ_API_KEY"):
        register("groq", GroqProvider())
        log.info("[bootstrap] groq registered (API-Key vorhanden)")

    # CR-299: DeepSeek (nur wenn API-Key vorhanden)
    if not is_registered("deepseek") and _os.environ.get("DEEPSEEK_API_KEY"):
        register("deepseek", DeepSeekProvider())
        log.info("[bootstrap] deepseek registered (API-Key vorhanden)")

    # Local LLM (Ollama or SGLang/vLLM)
    if not is_registered("ollama"):
        local_cfg = sb_config.get("providers", {}).get("ollama", {})
        provider = LocalLLMProvider(
            base_url=local_cfg.get("base_url"),
            backend=local_cfg.get("backend", "ollama"),
            name="ollama",
            api_key=local_cfg.get("api_key"),
        )
        register("ollama", provider)

        # Initial health check to discover models
        try:
            status = await provider.health_check()
            log.info(f"[bootstrap] ollama health: {status.value}")
        except Exception as exc:
            log.info(f"[bootstrap] ollama not available: {exc}")

    # 2. Create and start switchboard
    from .switchboard import Switchboard
    from .router import init_switchboard

    switchboard = Switchboard(sb_config)
    if db_pool:
        switchboard.set_db_pool(db_pool)
    await switchboard.start()
    init_switchboard(switchboard)

    _initialized = True
    log.info("[bootstrap] LLM switchboard initialized")


async def shutdown_llm_switchboard() -> None:
    """Stop the switchboard (flush cost tracker, stop health monitor)."""
    global _initialized
    from .router import get_switchboard
    sb = get_switchboard()
    if sb:
        await sb.stop()
    _initialized = False
    log.info("[bootstrap] LLM switchboard stopped")


def is_switchboard_active() -> bool:
    """Check if the switchboard has been initialized."""
    return _initialized
