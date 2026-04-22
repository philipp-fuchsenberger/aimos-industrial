"""SW2-2: API Key Pool — Multi-Key Management with Round-Robin Rotation.

Manages multiple API keys per provider. Falls back to environment variables
when the api_key_pool table is empty or DB is unavailable.

Key selection: Round-Robin by last_used_at (oldest first).
On 429/Rate-Limit: key gets cooldown, next key is selected.
On 401/403: key is revoked, next key is selected.

Thread-safety: asyncio.Lock per provider (single-process, no DB locking needed).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("AIMOS.llm.key_pool")


@dataclass
class KeyInfo:
    """Represents a single API key."""
    id: int | None       # DB id (None for env-var keys)
    provider: str
    api_key: str
    label: str
    status: str          # active, exhausted, revoked, rate_limited
    cooldown_until: datetime | None = None

    @property
    def is_available(self) -> bool:
        if self.status != "active":
            return False
        if self.cooldown_until and datetime.now(timezone.utc) < self.cooldown_until:
            return False
        return True


class KeyPool:
    """Manages API keys per provider with rotation and cooldown.

    Usage:
        pool = KeyPool()
        await pool.load_from_db(db_pool)  # or load_from_env()

        key = pool.get_key("mistral")  # returns KeyInfo or None
        pool.mark_rate_limited(key, cooldown_s=60)
        pool.mark_revoked(key)
    """

    def __init__(self):
        self._keys: dict[str, list[KeyInfo]] = {}  # provider → [KeyInfo]
        self._locks: dict[str, asyncio.Lock] = {}
        self._round_robin_idx: dict[str, int] = {}

    def _get_lock(self, provider: str) -> asyncio.Lock:
        if provider not in self._locks:
            self._locks[provider] = asyncio.Lock()
        return self._locks[provider]

    async def load_from_db(self, db_pool) -> int:
        """Load keys from api_key_pool table. Returns count loaded."""
        try:
            async with db_pool.acquire(timeout=5) as conn:
                rows = await conn.fetch(
                    "SELECT id, provider, api_key, label, status, cooldown_until "
                    "FROM api_key_pool WHERE status != 'revoked' "
                    "ORDER BY provider, last_used_at ASC NULLS FIRST"
                )
            for row in rows:
                provider = row["provider"]
                if provider not in self._keys:
                    self._keys[provider] = []
                    self._round_robin_idx[provider] = 0
                self._keys[provider].append(KeyInfo(
                    id=row["id"],
                    provider=provider,
                    api_key=row["api_key"],
                    label=row["label"] or "",
                    status=row["status"],
                    cooldown_until=row["cooldown_until"],
                ))
            total = sum(len(v) for v in self._keys.values())
            if total:
                log.info(
                    f"[key_pool] loaded {total} keys from DB: "
                    + ", ".join(f"{k}={len(v)}" for k, v in self._keys.items())
                )
            return total
        except Exception as exc:
            log.warning(f"[key_pool] DB load failed, using env vars: {exc}")
            return 0

    def load_from_env(self) -> None:
        """Load keys from environment variables (fallback)."""
        env_keys = {
            "mistral": "MISTRAL_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        for provider, env_var in env_keys.items():
            key = os.environ.get(env_var)
            if key and provider not in self._keys:
                self._keys[provider] = [KeyInfo(
                    id=None, provider=provider, api_key=key,
                    label="env", status="active",
                )]
                self._round_robin_idx[provider] = 0
                log.info(f"[key_pool] loaded {provider} key from env var")

    async def get_key(self, provider: str) -> KeyInfo | None:
        """Get the next available key for a provider (round-robin).

        Returns None if no keys available (all revoked/on cooldown).
        """
        async with self._get_lock(provider):
            keys = self._keys.get(provider, [])
            if not keys:
                return None

            n = len(keys)
            start_idx = self._round_robin_idx.get(provider, 0)

            for i in range(n):
                idx = (start_idx + i) % n
                key = keys[idx]
                # Check if cooldown has expired
                if key.status == "rate_limited" and key.cooldown_until:
                    if datetime.now(timezone.utc) >= key.cooldown_until:
                        key.status = "active"
                        key.cooldown_until = None
                if key.is_available:
                    self._round_robin_idx[provider] = (idx + 1) % n
                    return key

            return None  # all keys exhausted/on cooldown

    def mark_rate_limited(self, key: KeyInfo, cooldown_s: float = 60.0) -> None:
        """Mark a key as rate-limited with cooldown."""
        key.status = "rate_limited"
        key.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)
        log.warning(
            f"[key_pool] {key.provider}/{key.label} rate-limited, "
            f"cooldown {cooldown_s}s"
        )

    def mark_revoked(self, key: KeyInfo) -> None:
        """Mark a key as permanently revoked (401/403)."""
        key.status = "revoked"
        log.error(f"[key_pool] {key.provider}/{key.label} REVOKED (auth failure)")

    def mark_success(self, key: KeyInfo) -> None:
        """Record a successful use of a key."""
        key.status = "active"
        key.cooldown_until = None

    async def persist_key_status(self, key: KeyInfo, db_pool) -> None:
        """Persist key status change to DB (fire-and-forget)."""
        if key.id is None:
            return  # env-var key, nothing to persist
        try:
            async with db_pool.acquire(timeout=5) as conn:
                await conn.execute(
                    "UPDATE api_key_pool SET status=$1, cooldown_until=$2, "
                    "last_used_at=NOW(), error_count=error_count+1 WHERE id=$3",
                    key.status, key.cooldown_until, key.id,
                )
        except Exception as exc:
            log.warning(f"[key_pool] persist failed for key {key.id}: {exc}")

    def available_count(self, provider: str) -> int:
        """Count of currently available keys for a provider."""
        return sum(1 for k in self._keys.get(provider, []) if k.is_available)

    def status_summary(self) -> dict[str, dict[str, int]]:
        """Status summary for monitoring."""
        result = {}
        for provider, keys in self._keys.items():
            result[provider] = {
                "total": len(keys),
                "active": sum(1 for k in keys if k.status == "active"),
                "rate_limited": sum(1 for k in keys if k.status == "rate_limited"),
                "revoked": sum(1 for k in keys if k.status == "revoked"),
            }
        return result
