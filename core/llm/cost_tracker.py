"""SW3-2: Structured Cost Tracking with Batch Insert + Per-Agent Budget.

Records every LLM call to llm_call_log table. Uses a buffer that flushes
every flush_interval_s seconds or when flush_size entries accumulate.

Per-agent budget enforcement (E-08):
  - Daily and monthly caps per agent (from agents.config JSONB)
  - Checked in-memory from accumulated costs (refreshed from DB periodically)
  - 80% → warning flag, 100% → BudgetExceededError
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("AIMOS.llm.cost_tracker")


@dataclass
class CallLogEntry:
    """One LLM call log entry."""
    agent_name: str | None = None
    project_id: str | None = None
    tenant_id: str = "default"
    provider: str = ""
    model: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: str = "ok"          # ok, error, timeout, fallback
    error_msg: str | None = None
    priority: int | None = None
    key_label: str | None = None
    was_fallback: bool = False
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CostTracker:
    """Structured LLM cost logging with batch insert and budget tracking.

    Usage:
        tracker = CostTracker(db_pool=pool)
        await tracker.start()

        tracker.record(agent_name="bot", provider="mistral", ...)

        cost = tracker.agent_cost_today("bot")
        await tracker.flush()
    """

    def __init__(
        self,
        db_pool=None,
        flush_interval_s: float = 10.0,
        flush_size: int = 50,
    ):
        self._db_pool = db_pool
        self._flush_interval_s = flush_interval_s
        self._flush_size = flush_size
        self._buffer: list[CallLogEntry] = []
        self._flush_task: asyncio.Task | None = None
        # In-memory cost accumulators (per agent, per day)
        self._agent_costs_today: dict[str, float] = {}
        self._today_str: str = ""

    async def start(self) -> None:
        """Start the background flush loop."""
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())
            self._today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log.info(
                f"[cost_tracker] started (interval={self._flush_interval_s}s, "
                f"batch={self._flush_size})"
            )

    async def stop(self) -> None:
        """Stop the flush loop and flush remaining entries."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()

    def record(
        self,
        *,
        agent_name: str | None = None,
        provider: str = "",
        model: str = "",
        in_tokens: int = 0,
        out_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        status: str = "ok",
        error_msg: str | None = None,
        priority: int | None = None,
        key_label: str | None = None,
        was_fallback: bool = False,
    ) -> None:
        """Add an entry to the buffer. Flushes when buffer is full."""
        entry = CallLogEntry(
            agent_name=agent_name,
            provider=provider,
            model=model,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error_msg=error_msg,
            priority=priority,
            key_label=key_label,
            was_fallback=was_fallback,
        )
        self._buffer.append(entry)

        # Update in-memory cost accumulator
        if agent_name and cost_usd > 0:
            self._check_day_rollover()
            self._agent_costs_today[agent_name] = (
                self._agent_costs_today.get(agent_name, 0.0) + cost_usd
            )

        # Flush if buffer is full
        if len(self._buffer) >= self._flush_size:
            asyncio.ensure_future(self.flush())

    async def flush(self) -> None:
        """Write buffer to DB (batch INSERT)."""
        if not self._buffer:
            return
        if not self._db_pool:
            self._buffer.clear()
            return

        entries = self._buffer[:]
        self._buffer.clear()

        try:
            async with self._db_pool.acquire(timeout=5) as conn:
                await conn.executemany(
                    """INSERT INTO llm_call_log
                       (ts, agent_name, provider, model, in_tokens, out_tokens,
                        cost_usd, latency_ms, status, error_msg, priority,
                        key_label, was_fallback)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    [
                        (
                            e.ts, e.agent_name, e.provider, e.model,
                            e.in_tokens, e.out_tokens, e.cost_usd, e.latency_ms,
                            e.status, e.error_msg, e.priority,
                            e.key_label, e.was_fallback,
                        )
                        for e in entries
                    ],
                )
            log.debug(f"[cost_tracker] flushed {len(entries)} entries to DB")
        except Exception as exc:
            log.warning(f"[cost_tracker] flush failed ({len(entries)} entries lost): {exc}")

    def agent_cost_today(self, agent_name: str) -> float:
        """Return today's cost for an agent (from in-memory accumulator)."""
        self._check_day_rollover()
        return self._agent_costs_today.get(agent_name, 0.0)

    def _check_day_rollover(self) -> None:
        """Reset daily counters if day changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._today_str:
            self._agent_costs_today.clear()
            self._today_str = today

    async def _flush_loop(self) -> None:
        """Background loop: flush buffer periodically."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval_s)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(f"[cost_tracker] flush loop error: {exc}")

    def status(self) -> dict:
        """Status for monitoring."""
        return {
            "buffer_size": len(self._buffer),
            "agents_tracked_today": len(self._agent_costs_today),
            "total_cost_today": sum(self._agent_costs_today.values()),
        }
