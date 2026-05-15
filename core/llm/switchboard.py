"""LLM Switchboard — Central dispatch with Circuit Breaker, Key Pool,
Rate Limiter, Cost Tracking, and Fallback Chain.

Single entrypoint for all LLM calls in AIMOS. Replaces the dispatch logic in
router.py while keeping the same external call() signature.

Responsibilities:
  - Build fallback chain from agent config + available providers
  - Check circuit breaker before dispatching to a provider
  - Select API key from key pool (round-robin with rotation)
  - Rate-limit requests per provider (token bucket)
  - Record success/failure for circuit breaker + key pool
  - Set truncated/degraded/was_fallback flags in response
  - Log calls to llm_call_log (cost tracker)

NOT responsible for (V-05):
  - JSON repair (agent_base._repair_json)
  - Prompt construction
  - Tool interpretation
  - User-facing warnings
"""
from __future__ import annotations

import os
import json
import logging
import time

from .circuit_breaker import CircuitBreaker, is_circuit_breaker_error
from .providers.base import (
    ProviderBase,
    ProviderStatus,
    all_providers,
    get as get_provider,
    is_registered,
)

log = logging.getLogger("AIMOS.llm.switchboard")


class SwitchboardError(RuntimeError):
    """All providers in fallback chain failed."""


# CR-301/F: Pre-Return-Quality-Gate
def _validate_output_quality(
    result: dict,
    response_format: dict | None,
    quality_gate_enabled: bool = True,
) -> tuple[bool, str | None]:
    """Pre-Return-Quality-Gate fuer LLM-Outputs.

    Schuetzt Caller davor, abgeschnittene oder leere Antworten als
    Erfolg zu interpretieren. Zentraler Schutz, damit produktiv-Agenten
    (strict_provider=True) nicht „mist abliefern" sondern lieber FAILEN.

    Returns:
        (True, None) wenn alles ok
        (False, reason) wenn Quality-Issue erkannt

    Reasons:
        - "truncated_max_tokens": finish_reason=length (Output abgeschnitten)
        - "empty_response": kein content + keine tool_calls
        - "invalid_json": response_format=json_object verlangt, aber Content
          nicht parsbar
    """
    if not quality_gate_enabled:
        return True, None

    # 1. Truncation
    finish = result.get("finish_reason")
    if finish == "length":
        return False, "truncated_max_tokens"

    # 2. Leere Antwort
    content = (result.get("content") or "").strip()
    tool_calls = result.get("tool_calls") or []
    if not content and not tool_calls:
        return False, "empty_response"

    # 3. JSON-Parse-Check wenn explizit als JSON angefordert
    if response_format and response_format.get("type") in ("json_object", "json_schema"):
        if content:
            try:
                json.loads(content)
            except (json.JSONDecodeError, TypeError, ValueError):
                return False, "invalid_json"

    return True, None
    pass


class Switchboard:
    """Central LLM dispatch with circuit breaker, key pool, and rate limiter.

    Usage:
        sb = Switchboard(config)
        await sb.start()
        result = await sb.dispatch(provider="mistral", model="...", messages=[...])
        await sb.stop()
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._key_pool = None          # KeyPool, set in start()
        self._rate_limiters = None     # RateLimiterRegistry, set in start()
        self._cost_tracker = None      # CostTracker, set in start() (SW3)
        self._capabilities = None      # Capabilities, set in start() (SW4)
        self._db_pool = None           # asyncpg pool, set via set_db_pool()
        self._started = False

    def set_db_pool(self, db_pool) -> None:
        """Set the DB pool for key_pool and cost_tracker persistence."""
        self._db_pool = db_pool

    async def start(self) -> None:
        """Initialize all subsystems."""
        provider_configs = self._config.get("providers", {})

        # Circuit breakers
        for name in all_providers():
            cb_config = provider_configs.get(name, {}).get("circuit_breaker", {})
            self._circuit_breakers[name] = CircuitBreaker(
                provider=name,
                failure_threshold=cb_config.get("failure_threshold", 3),
                cooldown_s=cb_config.get("cooldown_s", 300.0),
                probe_after_s=cb_config.get("probe_after_s", 120.0),
            )

        # Key pool
        from .key_pool import KeyPool
        self._key_pool = KeyPool()
        if self._db_pool:
            await self._key_pool.load_from_db(self._db_pool)
        self._key_pool.load_from_env()  # fallback for providers without DB keys

        # Rate limiters
        from .rate_limiter import RateLimiterRegistry
        self._rate_limiters = RateLimiterRegistry(provider_configs)

        # Cost tracker (SW3 — initialized here, no-op if not yet implemented)
        try:
            from .cost_tracker import CostTracker
            self._cost_tracker = CostTracker(db_pool=self._db_pool)
            await self._cost_tracker.start()
        except ImportError:
            self._cost_tracker = None

        # Capabilities + aliases
        from .capabilities import Capabilities
        alias_config = self._config.get("aliases", {})
        self._capabilities = Capabilities(switchboard=self, aliases=alias_config)
        self._capabilities.refresh_from_providers()

        self._started = True
        log.info(
            f"[switchboard] started with {len(self._circuit_breakers)} providers: "
            f"{', '.join(sorted(self._circuit_breakers))}"
            + (f", key_pool: {self._key_pool.status_summary()}" if self._key_pool else "")
        )

    async def stop(self) -> None:
        """Shutdown: flush cost tracker."""
        if self._cost_tracker:
            try:
                await self._cost_tracker.flush()
            except Exception as exc:
                log.warning(f"[switchboard] cost tracker flush failed: {exc}")
        self._started = False
        log.info("[switchboard] stopped")

    def get_circuit_breaker(self, provider: str) -> CircuitBreaker | None:
        """Get circuit breaker for a provider (for monitoring)."""
        return self._circuit_breakers.get(provider)

    async def dispatch(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        timeout_s: float = 90.0,
        fallback: list[dict] | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        api_key: str | None = None,
        allow_degraded: bool = True,
        agent_name: str | None = None,
        priority: int = 1,
        strict_provider: bool = False,    # CR-301: produktiv-Agent, kein Cross-Provider-Fallback
        quality_gate: bool = True,        # CR-301: Pre-Return-Validierung
    ) -> dict:
        """Dispatch an LLM call through the switchboard.

        Flow:
        1. Build fallback chain
        2. For each provider in chain:
           a. Circuit breaker check
           b. Rate limiter acquire
           c. Key pool get_key (if no explicit api_key)
           d. Provider.call()
           e. Record success/failure
           f. Log to cost tracker
        """
        # Resolve model alias (e.g. "strong_reasoning" → "magistral-medium-latest")
        if self._capabilities:
            resolved = self._capabilities.resolve_alias(model)
            if resolved:
                provider, model = resolved
                log.info(f"[switchboard] alias resolved → {provider}/{model}")

        # Agent budget check (P3.6)
        if agent_name and self._cost_tracker:
            budget_result = self._check_agent_budget(agent_name, provider, model)
            if budget_result == "blocked":
                raise SwitchboardError(
                    f"Agent '{agent_name}' budget exceeded — call blocked"
                )
            elif budget_result == "downgrade":
                # Switch to cheaper model
                if self._capabilities:
                    cheaper = self._capabilities.resolve_alias("fast_cheap")
                    if cheaper and cheaper != (provider, model):
                        log.warning(
                            f"[switchboard] agent '{agent_name}' at 80%+ budget, "
                            f"downgrading {provider}/{model} → {cheaper[0]}/{cheaper[1]}"
                        )
                        provider, model = cheaper

        # CR-301: strict_provider deaktiviert Cross-Provider-Fallback komplett.
        # Caller bekommt nur Same-Provider-Retry (durch Provider-internen
        # Retry-Loop) oder einen Fehler. Lieber WAITING als unkonsistenter Output.
        # CR-301: tools_required → Capability-Filter im _build_chain
        tools_required = bool(tools)
        chain = self._build_chain(
            provider, model,
            fallback if not strict_provider else None,
            allow_degraded and not strict_provider,
            tools_required=tools_required,
        )

        if not chain:
            raise SwitchboardError(
                f"No providers available (primary={provider}, "
                f"all circuit breakers open)"
            )

        last_error: Exception | None = None
        is_primary = True

        for prov_name, mod in chain:
            # 1. Circuit breaker check
            cb = self._circuit_breakers.get(prov_name)
            if cb and not cb.can_execute():
                log.info(
                    f"[switchboard] skipping {prov_name}/{mod} "
                    f"(circuit {cb.status})"
                )
                is_primary = False
                continue

            # 2. Get provider instance
            try:
                prov_instance = get_provider(prov_name)
            except KeyError:
                log.warning(f"[switchboard] provider '{prov_name}' not registered, skipping")
                is_primary = False
                continue

            # 3. Rate limiter
            if self._rate_limiters:
                rl = self._rate_limiters.get_or_create(prov_name)
                acquired = await rl.acquire(timeout_s=min(timeout_s, 30.0))
                if not acquired:
                    log.warning(f"[switchboard] rate limit timeout for {prov_name}, trying next")
                    is_primary = False
                    continue

            # 4. Key selection
            call_key = api_key
            selected_key = None
            if not call_key and self._key_pool:
                selected_key = await self._key_pool.get_key(prov_name)
                if selected_key:
                    call_key = selected_key.api_key

            # 5. Dispatch call
            t0 = time.monotonic()
            try:
                result = await prov_instance.call(
                    model=mod,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stop=stop,
                    timeout_s=timeout_s,
                    api_key=call_key,
                    reasoning_effort=reasoning_effort,
                    response_format=response_format,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                # 6. Record success
                if cb:
                    cb.record_success()
                if selected_key:
                    self._key_pool.mark_success(selected_key)

                # 7. Enrich response
                result["truncated"] = result.get("finish_reason") == "length"
                result["was_fallback"] = not is_primary
                result["latency_ms"] = latency_ms
                if "degraded" not in result:
                    result["degraded"] = False

                # 8. Cost tracking
                if self._cost_tracker:
                    self._cost_tracker.record(
                        agent_name=agent_name,
                        provider=result.get("provider", prov_name),
                        model=result.get("model", mod),
                        in_tokens=result.get("in_tokens", 0),
                        out_tokens=result.get("out_tokens", 0),
                        cost_usd=result.get("cost_usd", 0),
                        latency_ms=latency_ms,
                        status="ok",
                        was_fallback=not is_primary,
                        key_label=selected_key.label if selected_key else None,
                        priority=priority,
                    )

                if not is_primary:
                    log.info(
                        f"[switchboard] fallback {prov_name}/{mod} succeeded "
                        f"(primary was {provider}/{model})"
                    )
                if result["truncated"]:
                    log.warning(
                        f"[switchboard] {prov_name}/{mod} output truncated "
                        f"(finish_reason=length)"
                    )

                # CR-301/F: Pre-Return-Quality-Gate
                gate_ok, gate_reason = _validate_output_quality(
                    result, response_format, quality_gate,
                )
                if not gate_ok:
                    log.warning(
                        f"[switchboard] Quality-Gate FAIL fuer {prov_name}/{mod}: "
                        f"{gate_reason} (strict_provider={strict_provider})"
                    )
                    last_error = SwitchboardError(
                        f"Quality gate failed for {prov_name}/{mod}: {gate_reason}"
                    )
                    is_primary = False
                    # Nicht returnen — naechste Iteration versucht den naechsten
                    # Provider (im strict-Modus ist die Chain leer ausser Primary,
                    # also wird der Loop danach mit SwitchboardError beendet).
                    continue

                return result

            except Exception as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                exc_str = str(exc)
                log.warning(
                    f"[switchboard] {prov_name}/{mod} failed after {latency_ms}ms: {exc}"
                )

                # Circuit breaker
                if cb and is_circuit_breaker_error(exc):
                    cb.record_failure(exc)

                # Key rotation on 429 or auth failure
                if selected_key:
                    if "429" in exc_str or "Rate" in exc_str:
                        self._key_pool.mark_rate_limited(selected_key, cooldown_s=60.0)
                        # Apply backpressure to rate limiter
                        if self._rate_limiters:
                            rl = self._rate_limiters.get_or_create(prov_name)
                            rl.backpressure(factor=0.5)
                    elif "401" in exc_str or "403" in exc_str:
                        self._key_pool.mark_revoked(selected_key)

                # Cost tracking for failures
                if self._cost_tracker:
                    self._cost_tracker.record(
                        agent_name=agent_name,
                        provider=prov_name,
                        model=mod,
                        in_tokens=0,
                        out_tokens=0,
                        cost_usd=0,
                        latency_ms=latency_ms,
                        status="error",
                        error_msg=exc_str[:500],
                        was_fallback=not is_primary,
                        key_label=selected_key.label if selected_key else None,
                        priority=priority,
                    )

                last_error = exc
                is_primary = False
                continue

        raise SwitchboardError(
            f"All providers in chain failed "
            f"(chain={[(p, m) for p, m in chain]}). "
            f"Last error: {last_error}"
        )

    def _build_chain(
        self,
        provider: str,
        model: str,
        fallback: list[dict] | None,
        allow_degraded: bool,
        tools_required: bool = False,   # CR-301/K: Capability-Filter
    ) -> list[tuple[str, str]]:
        """Build the fallback chain from primary + explicit fallback + Ollama.

        CR-301/K: Wenn `tools_required=True`, werden Modelle ohne
        Tool-Calling-Support aus der Chain gefiltert (z.B. deepseek-reasoner,
        magistral-small ohne Tools). Sonst koennte ein Fallback auf so ein
        Modell silent das Tool-Call-Ergebnis verschlucken.
        """
        chain: list[tuple[str, str]] = [(provider, model)]

        if fallback:
            for fb in fallback:
                prov = fb.get("provider", "")
                mod = fb.get("model", "")
                if prov and mod and (prov, mod) != (provider, model):
                    chain.append((prov, mod))

        if allow_degraded and is_registered("ollama"):
            ollama_in_chain = any(p == "ollama" for p, _ in chain)
            if not ollama_in_chain:
                ollama_prov = get_provider("ollama")
                ollama_models = getattr(ollama_prov, "available_models", [])
                if ollama_models:
                    chain.append(("ollama", ollama_models[0]))

        # CR-301/K: Capability-Filter — wenn tools im Aufruf, schmeiss Modelle
        # ohne Tool-Support raus. Primary bleibt drin (auch wenn er kein Tool
        # hat — Caller-Verantwortung), aber Fallbacks werden gefiltert.
        if tools_required and self._capabilities:
            primary = chain[0]
            filtered_fallbacks = []
            for prov, mod in chain[1:]:
                model_info = self._capabilities.get_model(mod)
                if model_info is None:
                    # unbekanntes Modell — durchlassen, vielleicht hat es Tools
                    filtered_fallbacks.append((prov, mod))
                elif model_info.supports_tools:
                    filtered_fallbacks.append((prov, mod))
                else:
                    log.info(
                        f"[switchboard] CR-301/K: filter {prov}/{mod} aus Chain — "
                        f"supports_tools=False, aber tools_required=True"
                    )
            chain = [primary] + filtered_fallbacks

        return chain

    # ── Agent Budget (P3.6) ────────────────────────────────────────────────

    # Default budgets (USD). Override per agent via agents.config.budget_limits
    _DEFAULT_DAILY_BUDGET = 1.00
    _DEFAULT_MONTHLY_BUDGET = 20.00

    # Cache for per-agent budget config (loaded from DB lazily)
    _agent_budgets: dict[str, dict] = {}
    _budget_alerts_sent: set[str] = set()  # avoid duplicate alerts

    def _check_agent_budget(
        self, agent_name: str, provider: str, model: str,
    ) -> str:
        """Check agent budget. Returns 'ok', 'downgrade', or 'blocked'."""
        cost_today = self._cost_tracker.agent_cost_today(agent_name)
        budget = self._get_agent_budget(agent_name)
        daily_cap = budget.get("daily_usd", self._DEFAULT_DAILY_BUDGET)

        if daily_cap <= 0:
            return "ok"  # no limit configured

        ratio = cost_today / daily_cap

        if ratio >= 1.0:
            alert_key = f"{agent_name}_blocked_{self._cost_tracker._today_str}"
            if alert_key not in self._budget_alerts_sent:
                self._budget_alerts_sent.add(alert_key)
                log.error(
                    f"[budget] agent '{agent_name}' BLOCKED: "
                    f"${cost_today:.4f} >= cap ${daily_cap:.2f} (100%)"
                )
                self._send_budget_alert(
                    agent_name, cost_today, daily_cap, "BLOCKED"
                )
            return "blocked"

        if ratio >= 0.8:
            alert_key = f"{agent_name}_warning_{self._cost_tracker._today_str}"
            if alert_key not in self._budget_alerts_sent:
                self._budget_alerts_sent.add(alert_key)
                log.warning(
                    f"[budget] agent '{agent_name}' at {ratio:.0%}: "
                    f"${cost_today:.4f} / ${daily_cap:.2f} — downgrading model"
                )
                self._send_budget_alert(
                    agent_name, cost_today, daily_cap, "WARNING 80%"
                )
            return "downgrade"

        return "ok"

    def _get_agent_budget(self, agent_name: str) -> dict:
        """Get budget config for an agent (cached).

        Budget must be preloaded via preload_agent_budget() during agent init.
        If not preloaded, returns empty dict (= use defaults).
        """
        return self._agent_budgets.get(agent_name, {})

    async def _load_agent_budget(self, agent_name: str) -> dict:
        """Load budget_limits from agents.config JSONB."""
        try:
            async with self._db_pool.acquire(timeout=3) as conn:
                row = await conn.fetchrow(
                    "SELECT config FROM agents WHERE name=$1", agent_name
                )
                if row and row["config"]:
                    cfg = row["config"] if isinstance(row["config"], dict) else {}
                    return cfg.get("budget_limits", {})
        except Exception as exc:
            log.warning(f"[budget] failed to load budget for {agent_name}: {exc}")
        return {}

    def _send_budget_alert(
        self, agent_name: str, cost: float, cap: float, level: str,
    ) -> None:
        """Send budget alert via Telegram (fire-and-forget)."""
        if not self._db_pool:
            return
        try:
            import asyncio
            asyncio.ensure_future(self._async_budget_alert(
                agent_name, cost, cap, level
            ))
        except Exception:
            pass

    async def _async_budget_alert(
        self, agent_name: str, cost: float, cap: float, level: str,
    ) -> None:
        """Insert budget alert as outbound Telegram message."""
        try:
            async with self._db_pool.acquire(timeout=3) as conn:
                await conn.execute(
                    "INSERT INTO pending_messages "
                    "(agent_name, sender_id, content, kind) "
                    "VALUES ($1, $2, $3, 'outbound_telegram')",
                    agent_name,
                    int(os.getenv("AIMOS_OPERATOR_TELEGRAM_CHAT_ID", "0")),  # configurable
                    f"💰 BUDGET {level}: Agent '{agent_name}'\n"
                    f"Kosten heute: ${cost:.4f} / Cap: ${cap:.2f}\n"
                    f"Ratio: {cost/cap:.0%}",
                )
        except Exception as exc:
            log.warning(f"[budget] alert send failed: {exc}")

    async def preload_agent_budget(self, agent_name: str) -> None:
        """Preload budget config for an agent (call during init)."""
        if self._db_pool:
            self._agent_budgets[agent_name] = await self._load_agent_budget(agent_name)

    def status(self) -> dict:
        """Return current switchboard status (for monitoring/health endpoint)."""
        result = {
            "started": self._started,
            "providers": {
                name: {
                    "registered": True,
                    "circuit_breaker": cb.to_dict() if (cb := self._circuit_breakers.get(name)) else None,
                }
                for name in all_providers()
            },
        }
        if self._key_pool:
            result["key_pool"] = self._key_pool.status_summary()
        if self._rate_limiters:
            result["rate_limiters"] = self._rate_limiters.status()
        if self._capabilities:
            result["aliases"] = self._capabilities.list_aliases()
            result["available_models"] = len(self._capabilities.available_models())
        return result
