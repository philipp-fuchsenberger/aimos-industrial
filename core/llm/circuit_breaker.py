"""SW1-3: Circuit Breaker for LLM Providers.

State machine per provider (not per model — a provider outage affects all models).
Pattern taken from output_firewall.py (P1.2) and generalized.

States:
    CLOSED → Normal operation. Failures are counted.
    OPEN   → Provider is considered down. No requests dispatched.
             After cooldown_s: transitions to HALF_OPEN.
    HALF_OPEN → One probe request allowed. Success → CLOSED, Failure → OPEN.

What counts as a failure:
    - HTTP 5xx, Timeout, Connection Error → YES
    - HTTP 429 (Rate Limit) → NO (handled by Rate Limiter / Key Pool)
    - HTTP 4xx (Client Error) → NO (caller's fault)
    - Empty/malformed response body → YES
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("AIMOS.llm.circuit_breaker")

# HTTP status codes that count as circuit breaker failures
_FAILURE_STATUS_CODES = {500, 502, 503, 504, 520, 521, 522, 523, 524, 529}


@dataclass
class CircuitState:
    """In-memory state for one provider's circuit breaker."""
    status: Literal["closed", "open", "half_open"] = "closed"
    failure_count: int = 0
    last_failure_at: float = 0.0        # time.monotonic()
    last_success_at: float = 0.0
    opened_at: float = 0.0             # when circuit was opened


class CircuitBreaker:
    """Circuit breaker for a single LLM provider.

    Usage:
        cb = CircuitBreaker("mistral")

        if not cb.can_execute():
            # skip this provider, try next in fallback chain
            ...

        try:
            result = await provider.call(...)
            cb.record_success()
        except SomeError as exc:
            cb.record_failure(exc)
    """

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 3,
        cooldown_s: float = 300.0,
        probe_after_s: float = 120.0,
    ):
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.probe_after_s = probe_after_s
        self._state = CircuitState()

    @property
    def status(self) -> Literal["closed", "open", "half_open"]:
        # Check for automatic transition OPEN → HALF_OPEN
        if self._state.status == "open":
            elapsed = time.monotonic() - self._state.opened_at
            if elapsed >= self.probe_after_s:
                self._state.status = "half_open"
                log.info(
                    f"[{self.provider}] circuit OPEN → HALF_OPEN "
                    f"after {elapsed:.0f}s (probe_after_s={self.probe_after_s})"
                )
        return self._state.status

    @property
    def failure_count(self) -> int:
        return self._state.failure_count

    def can_execute(self) -> bool:
        """Can a request be dispatched to this provider?

        CLOSED → always yes
        HALF_OPEN → yes (one probe allowed)
        OPEN → only if probe_after_s has elapsed (auto-transitions to HALF_OPEN)
        """
        s = self.status  # triggers auto-transition check
        if s == "closed":
            return True
        if s == "half_open":
            return True
        # s == "open"
        return False

    def record_success(self) -> None:
        """Record a successful request. Resets failure count, closes circuit."""
        prev = self._state.status
        self._state.failure_count = 0
        self._state.last_success_at = time.monotonic()
        if prev in ("half_open", "open"):
            self._state.status = "closed"
            log.info(f"[{self.provider}] circuit {prev.upper()} → CLOSED (success)")
        self._state.status = "closed"

    def record_failure(self, error: Exception | None = None) -> None:
        """Record a failed request. Opens circuit if threshold reached."""
        now = time.monotonic()
        self._state.failure_count += 1
        self._state.last_failure_at = now

        if self._state.status == "half_open":
            # Probe failed → back to OPEN with fresh cooldown
            self._state.status = "open"
            self._state.opened_at = now
            log.warning(
                f"[{self.provider}] circuit HALF_OPEN → OPEN "
                f"(probe failed: {error})"
            )
        elif self._state.failure_count >= self.failure_threshold:
            self._state.status = "open"
            self._state.opened_at = now
            log.warning(
                f"[{self.provider}] circuit CLOSED → OPEN "
                f"({self._state.failure_count} failures, "
                f"cooldown={self.cooldown_s}s, error: {error})"
            )

    def reset(self) -> None:
        """Force-reset to CLOSED. For testing or manual override."""
        self._state = CircuitState()
        log.info(f"[{self.provider}] circuit force-reset to CLOSED")

    def time_until_probe(self) -> float | None:
        """Seconds until next probe attempt. None if not OPEN."""
        if self._state.status != "open":
            return None
        elapsed = time.monotonic() - self._state.opened_at
        remaining = self.probe_after_s - elapsed
        return max(0.0, remaining)

    def to_dict(self) -> dict:
        """Serialize state for monitoring/debugging."""
        return {
            "provider": self.provider,
            "status": self.status,
            "failure_count": self._state.failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_s": self.cooldown_s,
            "time_until_probe": self.time_until_probe(),
        }


def is_circuit_breaker_error(exc: Exception) -> bool:
    """Determine if an exception should count as a circuit breaker failure.

    Returns True for: server errors, timeouts, connection errors.
    Returns False for: rate limits (429), client errors (4xx), budget errors.
    """
    # httpx.HTTPStatusError — check status code
    exc_str = str(exc)
    # Check for known non-failure patterns
    if "429" in exc_str or "Rate" in exc_str:
        return False
    if "BudgetExceeded" in type(exc).__name__:
        return False
    # Check for 4xx client errors (except 408 Timeout which IS retryable)
    for code in (400, 401, 403, 404, 422):
        if str(code) in exc_str:
            return False
    # Everything else (5xx, timeout, connection error) → failure
    return True
