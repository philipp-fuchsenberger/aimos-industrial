"""CR-283: LLM Routing Layer.

Single entrypoint for hosted-API LLM calls. Decides which provider to dispatch
to based on the agent config (and later: data_class tags). Returns a normalized
dict compatible with what `_llm_chat` returns from the legacy Ollama path.

Today's behavior (v0):
  - `provider` is read directly from the call site (typically agent config)
  - `model` is read directly from the call site
  - Fallback chain is hardcoded: provider → optional fallback_provider → exception
  - Cost is computed and returned in the response dict
  - §126/A H10: persisted cost tracking with HARD cap per project_id

Sektion 108.1: Cost-Cap Runtime (process-local, in-memory)
  - Per-project cost accumulator with warning at 80% of configurable limit
  - track_cost / check_budget / reset_project_cost helpers

§126/A H10: Persisted Cost-Cap (DB-backed, hard cap)
  - project_cost table with cost_cents, cap_cents, blocked flag
  - persist_cost_cents / check_persisted_cap / set_project_cap helpers
  - BudgetExceededError raised before each LLM call when over cap
  - USD→EUR conversion uses fixed rate (configurable)
"""
import asyncio
import logging
import random
import threading

from .providers import mistral as _mistral
from .providers import anthropic as _anthropic

log = logging.getLogger("AIMOS.llm.router")

_KNOWN_PROVIDERS = {"mistral", "anthropic"}

# ── Sektion 108.1: Process-local Cost-Cap (legacy, kept for warnings) ───
_project_costs: dict[str, float] = {}
_cost_lock = threading.Lock()
DEFAULT_BUDGET_LIMIT_EUR = 1.0


def track_cost(project_id: str, cost: float) -> None:
    """Add *cost* (USD) to the running total for *project_id*."""
    if not project_id or cost <= 0:
        return
    with _cost_lock:
        _project_costs[project_id] = _project_costs.get(project_id, 0.0) + cost
        total = _project_costs[project_id]
    if total >= 0.8 * DEFAULT_BUDGET_LIMIT_EUR:
        log.warning(
            f"[108.1] project '{project_id}' cost ${total:.4f} reached "
            f"≥80 %% of default limit ({DEFAULT_BUDGET_LIMIT_EUR} EUR)"
        )


def check_budget(project_id: str, limit: float = DEFAULT_BUDGET_LIMIT_EUR) -> bool:
    """Return True if the project is still within budget, False if over."""
    with _cost_lock:
        return _project_costs.get(project_id, 0.0) <= limit


def reset_project_cost(project_id: str) -> None:
    """Clear the in-memory cost accumulator for *project_id*."""
    with _cost_lock:
        _project_costs.pop(project_id, None)


def _track_cost(cost: float) -> None:
    """Internal helper: auto-detect project_id and track cost."""
    try:
        from core.skills.base import get_active_project
        pid = get_active_project()
    except Exception:
        pid = None
    if pid:
        track_cost(pid, cost)


# ── §126/A H10: Persisted Cost-Cap (hard, multi-process) ────────────────

# USD → EUR conversion rate (fixed). Update periodically.
_USD_EUR_RATE = 0.92

# Hard cap default in EUR-cents (€10). Override per-project via set_project_cap.
DEFAULT_COST_CAP_CENTS = 1000


class BudgetExceededError(RuntimeError):
    """§H10: Raised when an LLM call would exceed the project's cost cap."""

    def __init__(self, project_id: str, current_cents: int, cap_cents: int):
        self.project_id = project_id
        self.current_cents = current_cents
        self.cap_cents = cap_cents
        super().__init__(
            f"§H10 BudgetExceeded: project={project_id} "
            f"current={current_cents}c cap={cap_cents}c "
            f"(€{current_cents/100:.2f} > €{cap_cents/100:.2f})"
        )


def _usd_to_cents(usd: float) -> int:
    """Convert USD to EUR-cents (rounded up)."""
    if usd <= 0:
        return 0
    eur = usd * _USD_EUR_RATE
    return max(1, int(eur * 100 + 0.5))  # round-half-up, min 1c if any cost


def _get_db_conn():
    """Lazy DB connection (avoids importing core.db_pool at module load)."""
    from core.db_pool import db_connection
    return db_connection()


def get_project_cost_cents(project_id: str) -> int:
    """Read persisted cost for a project. Returns 0 if no row exists."""
    if not project_id:
        return 0
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cost_cents FROM project_cost WHERE project_id=%s",
                (project_id,),
            )
            row = cur.fetchone()
            return int(row["cost_cents"]) if row else 0
    except Exception as exc:
        log.warning(f"[H10] get_project_cost_cents failed: {exc}")
        return 0


def get_project_cap_cents(project_id: str) -> int:
    """Read the cost cap for a project. Returns DEFAULT if no row exists."""
    if not project_id:
        return DEFAULT_COST_CAP_CENTS
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cap_cents FROM project_cost WHERE project_id=%s",
                (project_id,),
            )
            row = cur.fetchone()
            return int(row["cap_cents"]) if row else DEFAULT_COST_CAP_CENTS
    except Exception as exc:
        log.warning(f"[H10] get_project_cap_cents failed: {exc}")
        return DEFAULT_COST_CAP_CENTS


def set_project_cap(project_id: str, cap_cents: int, tenant_id: str = "default") -> None:
    """Set or update a project's cost cap. Creates the row if missing."""
    if not project_id or cap_cents < 0:
        return
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO project_cost (project_id, tenant_id, cap_cents) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (project_id) DO UPDATE SET "
                "cap_cents=EXCLUDED.cap_cents, updated_at=NOW()",
                (project_id, tenant_id, cap_cents),
            )
            conn.commit()
    except Exception as exc:
        log.error(f"[H10] set_project_cap failed: {exc}")


def check_persisted_cap(project_id: str) -> None:
    """§H10: raise BudgetExceededError if project is over its persisted cap.

    Idempotent — safe to call before each LLM request.
    """
    if not project_id:
        return
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cost_cents, cap_cents, blocked FROM project_cost "
                "WHERE project_id=%s",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                return  # no cost yet, ok
            current = int(row["cost_cents"])
            cap = int(row["cap_cents"])
            if row["blocked"] or current >= cap:
                raise BudgetExceededError(project_id, current, cap)
    except BudgetExceededError:
        raise
    except Exception as exc:
        log.warning(f"[H10] check_persisted_cap failed (allowing call): {exc}")


def persist_cost_cents(project_id: str, cost_usd: float, tenant_id: str = "default") -> int:
    """§H10: persist a cost increment for a project. Returns new total cents.

    Atomically adds cost (converted to cents) to project_cost. Sets blocked=TRUE
    if the new total reaches or exceeds the cap.
    """
    if not project_id or cost_usd <= 0:
        return 0
    cents = _usd_to_cents(cost_usd)
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO project_cost (project_id, tenant_id, cost_cents, cap_cents) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (project_id) DO UPDATE SET "
                "cost_cents = project_cost.cost_cents + EXCLUDED.cost_cents, "
                "updated_at = NOW() "
                "RETURNING cost_cents, cap_cents",
                (project_id, tenant_id, cents, DEFAULT_COST_CAP_CENTS),
            )
            row = cur.fetchone()
            new_total = int(row["cost_cents"])
            cap = int(row["cap_cents"])
            if new_total >= cap:
                cur.execute(
                    "UPDATE project_cost SET blocked=TRUE WHERE project_id=%s",
                    (project_id,),
                )
                log.warning(
                    f"[H10] project '{project_id}' BLOCKED at "
                    f"€{new_total/100:.2f} ≥ cap €{cap/100:.2f}"
                )
            elif new_total >= 0.8 * cap:
                log.warning(
                    f"[H10] project '{project_id}' at "
                    f"€{new_total/100:.2f} (≥80%% of €{cap/100:.2f})"
                )
            conn.commit()
            return new_total
    except Exception as exc:
        log.error(f"[H10] persist_cost_cents failed: {exc}")
        return 0


def reset_project_cost_persisted(project_id: str) -> None:
    """Clear the DB cost row for a project (e.g. for tests or after retire)."""
    if not project_id:
        return
    try:
        with _get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM project_cost WHERE project_id=%s", (project_id,),
            )
            conn.commit()
    except Exception as exc:
        log.warning(f"[H10] reset_project_cost_persisted failed: {exc}")


class LLMRouterError(RuntimeError):
    pass


# ── SW1-7: Switchboard Integration ──────────────────────────────────────
# When the switchboard is initialized, call() delegates to it.
# Otherwise, the legacy dispatch path is used (unchanged behavior).

_switchboard = None  # type: ignore[assignment]


def init_switchboard(switchboard) -> None:
    """Register the switchboard instance for dispatch delegation."""
    global _switchboard
    _switchboard = switchboard
    log.info("[router] switchboard registered — dispatch will use switchboard")


def get_switchboard():
    """Return the active switchboard instance (or None)."""
    return _switchboard


async def call(
    *,
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    stop: list[str] | None = None,
    timeout_s: float = 90.0,  # §134 Perf-4: match Mistral provider default, trigger fallback faster
    fallback: list[dict] | None = None,
    reasoning_effort: str | None = None,
    response_format: dict | None = None,
) -> dict:
    """Dispatch an LLM call to the requested provider.

    If the switchboard is initialized, delegates to switchboard.dispatch().
    Otherwise, uses the legacy dispatch path (direct provider calls).

    Args:
        provider: "mistral" | "anthropic" | "ollama"
        model: provider-specific model id
        messages: standard role/content list
        tools: optional tool schema list
        fallback: optional list of {"provider": ..., "model": ...} dicts to
                  try in order if the primary call fails

    Returns:
        dict with content, tool_calls, in_tokens, out_tokens, cost_usd,
        provider, model, finish_reason.
        When switchboard is active, also: truncated, degraded, was_fallback, latency_ms.
    """
    # §H10 Pre-flight: hard cap check before spending any money
    try:
        from core.skills.base import get_active_project
        active_pid = get_active_project()
    except Exception:
        active_pid = None
    if active_pid:
        check_persisted_cap(active_pid)  # raises BudgetExceededError if over

    # ── Switchboard path (new) ───────────────────────────────────────────
    if _switchboard is not None:
        from .switchboard import SwitchboardError
        try:
            result = await _switchboard.dispatch(
                provider=provider,
                model=model,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                stop=stop,
                timeout_s=timeout_s,
                fallback=fallback,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
            )
        except SwitchboardError as exc:
            raise LLMRouterError(str(exc)) from exc

        cost_usd = result.get("cost_usd", 0)
        log.info(
            f"[router] {result.get('provider')}/{result.get('model')} ok "
            f"in={result.get('in_tokens', 0)} out={result.get('out_tokens', 0)} "
            f"cost=${cost_usd:.4f}"
            + (" [fallback]" if result.get("was_fallback") else "")
            + (" [degraded]" if result.get("degraded") else "")
            + (" [truncated]" if result.get("truncated") else "")
        )
        _track_cost(cost_usd)
        if active_pid and cost_usd > 0:
            persist_cost_cents(active_pid, cost_usd)
        return result

    # ── Legacy path (unchanged, for backwards compat) ────────────────────
    chain: list[tuple[str, str]] = [(provider, model)]
    if fallback:
        for fb in fallback:
            if fb.get("provider") in _KNOWN_PROVIDERS and fb.get("model"):
                chain.append((fb["provider"], fb["model"]))

    # §173.2: Retry-Loop mit Exponential-Backoff + Jitter
    _MAX_RETRIES = 3
    _BASE_DELAY = 2.0  # Sekunden

    last_error: Exception | None = None
    for prov, mod in chain:
        for attempt in range(_MAX_RETRIES):
            try:
                if prov == "mistral":
                    result = await _mistral.call(
                        model=mod, messages=messages, tools=tools,
                        temperature=temperature, max_tokens=max_tokens,
                        stop=stop, timeout_s=timeout_s,
                        reasoning_effort=reasoning_effort,
                        response_format=response_format,
                    )
                elif prov == "anthropic":
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
                    result = await _anthropic.call(
                        model=mod, messages=_msgs, tools=tools,
                        temperature=temperature, max_tokens=max_tokens,
                        stop=stop, timeout_s=timeout_s,
                    )
                else:
                    raise LLMRouterError(f"Unknown provider '{prov}'")

                cost_usd = result.get("cost_usd", 0)
                log.info(
                    f"[router] {prov}/{mod} ok in={result.get('in_tokens', 0)} "
                    f"out={result.get('out_tokens', 0)} cost=${cost_usd:.4f}"
                )
                _track_cost(cost_usd)
                if active_pid and cost_usd > 0:
                    persist_cost_cents(active_pid, cost_usd)
                return result

            except Exception as exc:
                last_error = exc
                err_str = str(exc).lower()
                # §173.2: Retryable errors — Timeout, Rate-Limit, Server-Fehler
                retryable = any(p in err_str for p in [
                    "timeout", "429", "rate limit", "too many requests",
                    "408", "425", "502", "503", "504", "overloaded",
                    "connection", "reset by peer",
                ])
                if retryable and attempt < _MAX_RETRIES - 1:
                    # Exponential backoff mit Jitter: 2s, 4s, 8s + random(0-1s)
                    delay = _BASE_DELAY * (2 ** attempt) + random.random()
                    log.warning(
                        f"[router] {prov}/{mod} retry {attempt+1}/{_MAX_RETRIES} "
                        f"after {delay:.1f}s: {exc}"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    log.warning(f"[router] {prov}/{mod} failed (no retry): {exc}")
                    break  # Nächster Provider in der Kette

    raise LLMRouterError(
        f"All providers in chain failed (chain={chain}). Last error: {last_error}"
    )


def is_api_provider(provider: str | None) -> bool:
    """Return True if `provider` is one of the hosted API providers handled here."""
    if _switchboard is not None:
        from .providers.base import is_registered
        return is_registered(provider) if provider else False
    return provider in _KNOWN_PROVIDERS
