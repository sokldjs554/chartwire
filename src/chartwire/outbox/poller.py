"""Outbox poller (§7.1): PostgreSQL is the queue, one claim per tenant, leases, retry, DLQ.

One pass (:meth:`Poller.run_once`) = optional stuck-row reclaim → ``claim_batch`` under every active
tenant's ``app.tenant_id`` (``FOR UPDATE SKIP LOCKED``, so any number of workers may run this) →
execute the claimed events concurrently (bounded) → per event ``mark_done`` or ``mark_failed``.
:meth:`Poller.run` repeats passes every ``tick_s`` or as soon as ``outbox:wake`` is published, and stops
claiming when the :class:`~chartwire.ops.drain.Drainer` begins draining; in-flight handlers finish first.

Logs carry ``id``, ``event_type``, ``attempts`` and the handler name only — never the payload.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.clock import SystemClock
from chartwire.db.models import OutboxEvent as OutboxRow
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops.drain import Drainer
from chartwire.ops.metrics import (
    HANDLER_DURATION_SECONDS,
    HANDLER_FAILURES_TOTAL,
    OUTBOX_DEAD_TOTAL,
    OUTBOX_LAG_SECONDS,
    OUTBOX_PENDING,
)
from chartwire.outbox.context import Clock, HandlerContext, OutboxEvent
from chartwire.outbox.dlq import format_error
from chartwire.outbox.registry import (
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    REGISTRY,
    HandlerSpec,
    Registry,
    UnknownEventTypeError,
)
from chartwire.outbox.runtime import active_tenant_ids, run_handler
from chartwire.redis.keys import OUTBOX_WAKE

log = logging.getLogger(__name__)

DEFAULT_BATCH = 100
DEFAULT_TICK_S = 1.0
DEFAULT_TENANT_CACHE_S = 30.0
DEFAULT_RECLAIM_EVERY_S = 30.0
DEFAULT_STATS_EVERY_S = 10.0
DEFAULT_CONCURRENCY = 8
DEFAULT_DONE_BATCH = 50
"""``mark_done`` is written in batches (one transaction per tenant) as handlers finish, so the
bookkeeping costs a fraction of a transaction per event instead of a whole one."""
CLAIM_SAMPLES_KEPT = 10_000


@dataclass(slots=True)
class PassStats:
    """What one :meth:`Poller.run_once` did (tests and the bench read these)."""

    tenants: int = 0
    claimed: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    dead: int = 0
    reclaimed: int = 0
    claim_ms: list[float] = field(default_factory=list)

    @property
    def executed(self) -> int:
        return self.processed + self.skipped + self.failed + self.dead

    def merge(self, other: PassStats) -> None:
        """Accumulate ``other`` into this instance (``Poller.totals``); claim samples are capped."""
        for name in ("tenants", "claimed", "processed", "skipped", "failed", "dead", "reclaimed"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.claim_ms.extend(other.claim_ms)
        del self.claim_ms[:-CLAIM_SAMPLES_KEPT]


class Poller:
    def __init__(
        self,
        ctx: HandlerContext,
        *,
        worker_id: str,
        registry: Registry = REGISTRY,
        drainer: Drainer | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        batch: int = DEFAULT_BATCH,
        tick_s: float = DEFAULT_TICK_S,
        tenant_cache_s: float = DEFAULT_TENANT_CACHE_S,
        reclaim_every_s: float = DEFAULT_RECLAIM_EVERY_S,
        stats_every_s: float = DEFAULT_STATS_EVERY_S,
        concurrency: int = DEFAULT_CONCURRENCY,
        done_batch: int = DEFAULT_DONE_BATCH,
        wake_channel: str = OUTBOX_WAKE,
        tenants: list[UUID] | None = None,
    ) -> None:
        """``tenants`` pins the tenant set (bench / tests); ``None`` = every active tenant, cached."""
        if batch < 1 or concurrency < 1 or done_batch < 1 or tick_s <= 0:
            raise ValueError("batch, concurrency and done_batch must be >= 1, tick_s > 0")
        self._ctx = ctx
        self._engine: AsyncEngine = ctx.engine
        self._redis: Any = ctx.redis
        self._registry = registry
        self._drainer = drainer
        self._clock: Clock = clock or ctx.clock or SystemClock()
        self._rng = rng or random.Random()
        self._worker_id = worker_id
        self._batch = batch
        self._tick_s = tick_s
        self._tenant_cache_s = tenant_cache_s
        self._reclaim_every_s = reclaim_every_s
        self._stats_every_s = stats_every_s
        self._sem = asyncio.Semaphore(concurrency)
        self._done_batch = done_batch
        self._wake_channel = wake_channel
        self._wake = asyncio.Event()
        self._accepting = True
        self._pinned = tenants is not None
        self._tenants: list[UUID] = list(tenants or [])
        self._tenants_at: float | None = None
        self.totals = PassStats()
        """Everything this poller did since construction (``run_once`` merges each pass into it)."""
        self._reclaimed_at: float | None = None
        self._stats_at: float | None = None
        self._passes = 0
        if drainer is not None:
            drainer.on_begin(self.stop_claiming)

    # --- state --------------------------------------------------------------------------------------
    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def accepting(self) -> bool:
        """False once draining started: no new claims, in-flight handlers still finish."""
        return self._accepting and (self._drainer is None or self._drainer.accepting)

    @property
    def passes(self) -> int:
        return self._passes

    def stop_claiming(self) -> None:
        self._accepting = False
        self._wake.set()

    def wake(self) -> None:
        """Run the next pass now (``outbox:wake`` or an in-process emitter)."""
        self._wake.set()

    def lease_s(self) -> int:
        """One claim query per tenant, so the lease is the longest any registered handler needs."""
        return max((spec.lease_s for spec in self._registry), default=DEFAULT_LEASE_S)

    # --- loop ---------------------------------------------------------------------------------------
    async def run(self) -> None:
        """Poll until draining begins; returns after the in-flight handlers of the last pass finish."""
        listener = asyncio.create_task(self._wake_listener()) if self._redis is not None else None
        try:
            while self.accepting:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # a failed pass (DB blip) must not kill the worker
                    log.exception("outbox pass failed", extra={"worker_id": self._worker_id})
                await self._wait_tick()
        finally:
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener

    async def run_once(self, *, reclaim: bool | None = None, sample_stats: bool | None = None) -> PassStats:
        """One pass. ``reclaim``/``sample_stats`` force or suppress the periodic steps (tests)."""
        stats = PassStats()
        self._passes += 1
        if self._due(reclaim, self._reclaimed_at, self._reclaim_every_s):
            self._reclaimed_at = self._clock.monotonic()
            stats.reclaimed = await self.reclaim_stuck()
        if not self.accepting:
            return stats
        tenants = await self.tenants()
        stats.tenants = len(tenants)
        claimed: list[OutboxEvent] = []
        for tenant_id in tenants:
            if not self.accepting:
                break
            started = self._clock.monotonic()
            claimed.extend(await self._claim(tenant_id))
            stats.claim_ms.append((self._clock.monotonic() - started) * 1000.0)
        stats.claimed = len(claimed)
        if claimed:
            await self._execute_all(claimed, stats)
        if self._due(sample_stats, self._stats_at, self._stats_every_s):
            self._stats_at = self._clock.monotonic()
            await self.sample_stats(tenants)
        self.totals.merge(stats)
        return stats

    async def _execute_all(self, claimed: list[OutboxEvent], stats: PassStats) -> None:
        """Run the claimed events concurrently; ``mark_done`` finished ones in batches as they complete."""
        done: dict[UUID, list[int]] = defaultdict(list)
        pending_done = 0
        for future in asyncio.as_completed([self._guarded(event) for event in claimed]):
            event, outcome = await future
            setattr(stats, outcome, getattr(stats, outcome) + 1)
            if outcome in ("processed", "skipped"):
                done[event.tenant_id].append(event.id)
                pending_done += 1
                if pending_done >= self._done_batch:
                    await self._mark_done(done)
                    done.clear()
                    pending_done = 0
        if done:
            await self._mark_done(done)

    async def _mark_done(self, done: dict[UUID, list[int]]) -> None:
        """One transaction per tenant. A failure here is the §7.1 crash window: effects are committed,
        the lease expires, the row is re-delivered and skipped through ``processed_events``."""
        now = self._clock.now()
        for tenant_id, ids in done.items():
            try:
                async with tenant_tx(self._engine, TenantCtx.service(tenant_id)) as session:
                    await outbox_repo.mark_done_many(session, ids, now=now)
            except Exception:
                log.exception(
                    "outbox mark_done failed",
                    extra={"worker_id": self._worker_id, "tenant_id": str(tenant_id), "count": len(ids)},
                )

    def _due(self, force: bool | None, last: float | None, every_s: float) -> bool:
        if force is not None:
            return force
        return last is None or self._clock.monotonic() - last >= every_s

    async def _wait_tick(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), self._tick_s)
        self._wake.clear()

    async def _wake_listener(self) -> None:
        """``SUBSCRIBE outbox:wake`` → next pass immediately. Redis trouble degrades to 1 s polling."""
        while self.accepting:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._wake_channel)
                try:
                    while self.accepting:
                        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                        if message is not None:
                            self._wake.set()
                finally:
                    await pubsub.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("outbox wake subscription lost; polling only", exc_info=True)
                await asyncio.sleep(self._tick_s)

    # --- steps --------------------------------------------------------------------------------------
    async def tenants(self) -> list[UUID]:
        """Active tenant ids, cached for ``tenant_cache_s`` (ADR-0001: cross-tenant work iterates)."""
        if self._pinned:
            return self._tenants
        now = self._clock.monotonic()
        if self._tenants_at is None or now - self._tenants_at >= self._tenant_cache_s:
            self._tenants = await active_tenant_ids(self._engine)
            self._tenants_at = now
        return self._tenants

    async def _claim(self, tenant_id: UUID) -> list[OutboxEvent]:
        async with tenant_tx(self._engine, TenantCtx.service(tenant_id)) as session:
            rows = await outbox_repo.claim_batch(
                session, self._worker_id, self._batch, lease_s=self.lease_s(), now=self._clock.now()
            )
        events = [_to_event(row) for row in rows]
        if events:
            log.info(
                "outbox claimed",
                extra={"worker_id": self._worker_id, "tenant_id": str(tenant_id), "count": len(events)},
            )
        return events

    async def reclaim_stuck(self) -> int:
        """``in_flight`` rows past ``lease_until`` → ``pending`` (attempts unchanged), every tenant."""
        total = 0
        for tenant_id in await self.tenants():
            async with tenant_tx(self._engine, TenantCtx.service(tenant_id)) as session:
                total += await outbox_repo.reclaim_stuck(session, now=self._clock.now())
        if total:
            log.warning("outbox reclaimed stuck rows", extra={"count": total})
        return total

    async def sample_stats(self, tenants: list[UUID] | None = None) -> dict[str, float]:
        """``outbox_pending`` (sum over tenants) and ``outbox_lag_seconds`` (max over tenants)."""
        pending = 0
        lag = 0.0
        for tenant_id in tenants if tenants is not None else await self.tenants():
            async with tenant_tx(self._engine, TenantCtx.service(tenant_id)) as session:
                row = await outbox_repo.stats(session, now=self._clock.now())
            pending += int(row["pending"])
            lag = max(lag, float(row["lag_seconds"]))
        OUTBOX_PENDING.set(pending)
        OUTBOX_LAG_SECONDS.set(lag)
        return {"pending": float(pending), "lag_seconds": lag}

    async def _guarded(self, event: OutboxEvent) -> tuple[OutboxEvent, str]:
        async with self._sem:
            if self._drainer is None:
                return event, await self._execute(event)
            async with self._drainer.track():
                return event, await self._execute(event)

    async def _execute(self, event: OutboxEvent) -> str:
        """Run one claimed event: handler transaction, or a retry / the DLQ on failure. Returns the
        ``PassStats`` field name; ``processed``/``skipped`` rows are marked done by the caller."""
        spec = self._registry.lookup(event.event_type)
        started = self._clock.monotonic()
        try:
            if spec is None:
                raise UnknownEventTypeError(event.event_type)
            outcome = await run_handler(self._ctx, spec, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._on_failure(event, spec, exc)
        HANDLER_DURATION_SECONDS.labels(event.event_type).observe(self._clock.monotonic() - started)
        return outcome

    async def _on_failure(self, event: OutboxEvent, spec: HandlerSpec | None, exc: BaseException) -> str:
        HANDLER_FAILURES_TOTAL.labels(event.event_type).inc()
        max_attempts = spec.max_attempts if spec is not None else DEFAULT_MAX_ATTEMPTS
        try:
            async with tenant_tx(self._engine, TenantCtx.service(event.tenant_id)) as session:
                status = await outbox_repo.mark_failed(
                    session,
                    event.id,
                    error=format_error(exc),
                    max_attempts=max_attempts,
                    now=self._clock.now(),
                    rng=self._rng,
                )
        except Exception:
            log.exception("outbox mark_failed failed", extra=self._fields(event, spec))
            return "failed"
        fields = self._fields(event, spec) | {
            "attempts": event.attempts + 1,
            "error_type": type(exc).__name__,
        }
        if status == "dead":
            OUTBOX_DEAD_TOTAL.inc()
            log.error("outbox dead", extra=fields)
            return "dead"
        log.warning("outbox handler failed", extra=fields)
        return "failed"

    def _fields(self, event: OutboxEvent, spec: HandlerSpec | None) -> dict[str, object]:
        return {
            "worker_id": self._worker_id,
            "id": event.id,
            "event_type": event.event_type,
            "attempts": event.attempts,
            "handler": spec.name if spec is not None else None,
        }


def _to_event(row: OutboxRow) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=MappingProxyType(dict(row.payload)),
        idempotency_key=row.idempotency_key,
        attempts=row.attempts,
        created_at=row.created_at,
    )
