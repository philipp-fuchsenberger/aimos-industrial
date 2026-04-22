"""SW2-4: Token-Bucket Rate Limiter for LLM Providers.

In-memory rate limiter per provider. Queues requests when tokens are exhausted
rather than rejecting them.

Usage:
    rl = RateLimiter(rate=5.0, burst=10)  # 5 req/s, burst of 10
    acquired = await rl.acquire(timeout_s=30.0)
    if not acquired:
        raise RateLimitTimeout("...")
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("AIMOS.llm.rate_limiter")


class RateLimitTimeout(RuntimeError):
    """Raised when a request cannot be served within the timeout."""
    pass


class TokenBucket:
    """Token-Bucket Rate Limiter (asyncio, single-process).

    Args:
        rate: tokens replenished per second (e.g. 5.0 = 5 req/s)
        burst: maximum tokens in bucket (burst capacity)
    """

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Add tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, timeout_s: float = 30.0) -> bool:
        """Wait until a token is available or timeout.

        Returns True if token acquired, False if timeout.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            # Calculate wait time until next token
            async with self._lock:
                wait = (1.0 - self._tokens) / self.rate if self.rate > 0 else 1.0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            await asyncio.sleep(min(wait, remaining, 0.1))

    @property
    def tokens_available(self) -> float:
        """Current fill level (for monitoring)."""
        self._refill()
        return self._tokens

    def backpressure(self, factor: float = 0.5) -> None:
        """Temporarily reduce rate (called on 429 from provider)."""
        old_rate = self.rate
        self.rate = max(0.5, self.rate * factor)
        log.warning(f"[rate_limiter] backpressure: rate {old_rate:.1f} → {self.rate:.1f}")

    def restore_rate(self, original_rate: float) -> None:
        """Restore rate after backpressure period."""
        self.rate = original_rate


# ── Provider Rate Limiter Registry ───────────────────────────────────────

# Default rate limits per provider (req/s, burst)
_DEFAULTS: dict[str, tuple[float, int]] = {
    "mistral": (5.0, 10),
    "anthropic": (4.0, 8),
    "ollama": (1.0, 1),
}


class RateLimiterRegistry:
    """Manages rate limiters for all providers."""

    def __init__(self, config: dict | None = None):
        self._limiters: dict[str, TokenBucket] = {}
        self._config = config or {}

    def get_or_create(self, provider: str) -> TokenBucket:
        """Get or create a rate limiter for a provider."""
        if provider not in self._limiters:
            cfg = self._config.get(provider, {}).get("rate_limit", {})
            defaults = _DEFAULTS.get(provider, (5.0, 10))
            rate = cfg.get("rate", defaults[0])
            burst = cfg.get("burst", defaults[1])
            self._limiters[provider] = TokenBucket(rate=rate, burst=burst)
            log.info(f"[rate_limiter] created for {provider}: rate={rate}, burst={burst}")
        return self._limiters[provider]

    def status(self) -> dict[str, dict]:
        """Status for monitoring."""
        return {
            name: {
                "rate": limiter.rate,
                "burst": limiter.burst,
                "tokens": round(limiter.tokens_available, 1),
            }
            for name, limiter in self._limiters.items()
        }
