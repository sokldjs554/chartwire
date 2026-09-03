"""Poller against the real PostgreSQL/Redis (§7.1): poison message → exactly one DLQ row after 8
attempts with other rows unaffected; idempotent re-delivery; rollback of effects with the ledger;
lease reclaim after a simulated crash; two pollers never double-process; unknown event types wait in
the retry path; the run loop wakes on ``outbox:wake`` and stops claiming on drain.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_g CHARTWIRE_TEST_REDIS_DB=7``.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chartwire.core.clock import FakeClock, SystemClock
from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.db.models import AuditEvent, DeadLetter, OutboxEvent, ProcessedEvent
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops.drain import Drainer
from chartwire.ops.metrics import REGISTRY as METRICS
from chartwire.outbox import dlq, writer
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.context import OutboxEvent as Event
from chartwire.outbox.poller import Poller
from chartwire.outbox.registry import Registry
from chartwire.redis.keys import OUTBOX_WAKE

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- helpers


async def db_now(engine: AsyncEngine) -> datetime:
    async with AsyncSession(engine) as session:
        return (await session.execute(select(func.now()))).scalar_one()


async def anchored_clock(engine: AsyncEngine, ahead_s: float = 2.0) -> FakeClock:
    """``next_attempt_at`` defaults to the emitting transaction's ``now()``; a fake clock anchored a
    little *after* the database clock sees rows emitted in the next few statements as due."""
    return FakeClock(await db_now(engine) + timedelta(seconds=ahead_s))


def make_ctx(engine: AsyncEngine, clock: FakeClock | SystemClock, redis=None) -> HandlerContext:
    return HandlerContext(
        engine=engine,
        redis=redis,
        objectstore=None,
        clock=clock,
        settings=Settings(),
        kek=None,
        keycache=None,
    )


async def emit(engine: AsyncEngine, tenant_id: UUID, event_type: str, payload: dict | None = None) -> int:
    aggregate = uuid7()
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
        event_id = await writer.emit(
            session,
            tenant_id=tenant_id,
            aggregate_type="test",
            aggregate_id=aggregate,
            event_type=event_type,
            payload=payload or {},
            idempotency_key=writer.idempotency_key(event_type, aggregate, 1),
        )
    assert event_id is not None
    return event_id


async def row(engine: AsyncEngine, tenant_id: UUID, event_id: int) -> OutboxEvent:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
        return (await session.scalars(select(OutboxEvent).where(OutboxEvent.id == event_id))).one()


async def count(engine: AsyncEngine, tenant_id: UUID, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
        return int((await session.execute(stmt)).scalar_one())


async def audit_effect(ctx: HandlerContext, event: Event) -> None:
    """A real DB effect inside the handler transaction (joins the poller's tx via ``ctx.tenant_tx``)."""
    from chartwire.audit import service as audit

    async with ctx.tenant_tx(event.tenant_id) as session:
        await audit.record(
            session,
            tenant_id=event.tenant_id,
            actor_id=None,
            actor_role="service",
            action="session.transcribed",
            resource_type="outbox_event",
            resource_id=str(event.id),
            detail={"attempts": event.attempts},
        )


def poller(ctx: HandlerContext, registry: Registry, worker_id: str = "w1", **kw) -> Poller:
    return Poller(ctx, worker_id=worker_id, registry=registry, rng=random.Random(7), **kw)


# --------------------------------------------------------------------------- tests


async def test_poison_message_dies_after_eight_attempts_others_unaffected(app_engine, tenant_a, tenant_b):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    registry = Registry()
    ok_calls: list[int] = []

    @registry.handler("test.boom", max_attempts=8)
    async def boom(ctx: HandlerContext, event: Event) -> None:
        raise RuntimeError("always fails 010-1234-5678")

    @registry.handler("test.ok")
    async def ok(ctx: HandlerContext, event: Event) -> None:
        ok_calls.append(event.id)
        await audit_effect(ctx, event)

    poison = await emit(app_engine, tenant_a.id, "test.boom")
    good = [await emit(app_engine, tenant_a.id, "test.ok") for _ in range(3)]
    good.append(await emit(app_engine, tenant_b.id, "test.ok"))
    p = poller(ctx, registry)

    first = await p.run_once(reclaim=False, sample_stats=False)
    assert (first.claimed, first.processed, first.failed, first.dead) == (5, 4, 1, 0)
    for attempt in range(2, 9):
        clock.advance(400)  # > max backoff (300 s × 1.2 jitter)
        stats = await p.run_once(reclaim=False, sample_stats=False)
        assert stats.claimed == 1, f"attempt {attempt}: only the poison row is due"
        assert (stats.failed, stats.dead) == ((1, 0) if attempt < 8 else (0, 1))

    dead = await row(app_engine, tenant_a.id, poison)
    assert dead.status == "dead" and dead.attempts == 8 and dead.locked_by is None
    assert "RuntimeError" in dead.last_error and "[REDACTED]" in dead.last_error
    assert await count(app_engine, tenant_a.id, DeadLetter) == 1
    assert sorted(ok_calls) == sorted(good)
    for tenant, event_id in ((tenant_a, good[0]), (tenant_b, good[3])):
        assert (await row(app_engine, tenant.id, event_id)).status == "done"
    assert await count(app_engine, tenant_a.id, AuditEvent) == 3
    assert METRICS.get_sample_value("handler_failures_total", {"event_type": "test.boom"}) >= 8

    # replay: attempts reset, next pass runs it again (still failing → attempts 1, pending)
    assert await dlq.replay(app_engine, poison, now=clock.now()) is True
    assert await dlq.replay(app_engine, poison, now=clock.now()) is False
    replayed = await row(app_engine, tenant_a.id, poison)
    assert (replayed.status, replayed.attempts, replayed.last_error) == ("pending", 0, None)
    stats = await p.run_once(reclaim=False, sample_stats=False)
    assert (stats.claimed, stats.failed) == (1, 1)
    assert (await row(app_engine, tenant_a.id, poison)).attempts == 1
    letters = await dlq.list_dead(app_engine, tenant_id=tenant_a.id)
    assert len(letters) == 1 and letters[0].replayed_at is not None


async def test_redelivery_after_crash_before_mark_done_is_skipped(app_engine, tenant_a):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    registry = Registry()
    calls: list[int] = []

    @registry.handler("test.effect", name="effect_handler")
    async def effect(ctx: HandlerContext, event: Event) -> None:
        calls.append(event.id)
        await audit_effect(ctx, event)

    event_id = await emit(app_engine, tenant_a.id, "test.effect")
    p = poller(ctx, registry)
    assert (await p.run_once(reclaim=False, sample_stats=False)).processed == 1
    assert await count(app_engine, tenant_a.id, ProcessedEvent, handler="effect_handler") == 1

    # crash window: handler tx committed, mark_done never ran → the lease expires and the row is re-delivered
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(status="pending", locked_by=None, lease_until=None, done_at=None)
        )
    stats = await p.run_once(reclaim=False, sample_stats=False)
    assert (stats.claimed, stats.skipped, stats.processed) == (1, 1, 0)
    assert calls == [event_id]
    assert await count(app_engine, tenant_a.id, AuditEvent) == 1
    assert (await row(app_engine, tenant_a.id, event_id)).status == "done"


async def test_failed_handler_rolls_back_effects_and_ledger_together(app_engine, tenant_a):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    registry = Registry()

    @registry.handler("test.half")
    async def half(ctx: HandlerContext, event: Event) -> None:
        await audit_effect(ctx, event)
        raise ValueError("after the effect")

    event_id = await emit(app_engine, tenant_a.id, "test.half")
    stats = await poller(ctx, registry).run_once(reclaim=False, sample_stats=False)
    assert stats.failed == 1
    assert await count(app_engine, tenant_a.id, AuditEvent) == 0
    assert await count(app_engine, tenant_a.id, ProcessedEvent) == 0
    r = await row(app_engine, tenant_a.id, event_id)
    assert (r.status, r.attempts) == ("pending", 1)
    assert r.next_attempt_at > clock.now()


async def test_lease_reclaim_after_simulated_crash(app_engine, tenant_a):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    registry = Registry()
    calls: list[int] = []

    @registry.handler("test.lease", lease_s=5)
    async def lease(ctx: HandlerContext, event: Event) -> None:
        calls.append(event.attempts)

    event_id = await emit(app_engine, tenant_a.id, "test.lease")
    # a worker claims the row and dies (never processes, never releases)
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        claimed = await outbox_repo.claim_batch(session, "dead-worker", lease_s=5, now=clock.now())
    assert [e.id for e in claimed] == [event_id]

    p = poller(ctx, registry, worker_id="survivor")
    stats = await p.run_once(reclaim=True, sample_stats=False)
    assert (stats.reclaimed, stats.claimed) == (0, 0), "lease still valid: nothing to do"
    clock.advance(6)
    stats = await p.run_once(reclaim=True, sample_stats=False)
    assert (stats.reclaimed, stats.claimed, stats.processed) == (1, 1, 1)
    assert calls == [0], "reclaim does not count as an attempt"
    r = await row(app_engine, tenant_a.id, event_id)
    assert (r.status, r.attempts, r.locked_by) == ("done", 0, None)


async def test_two_pollers_never_double_process(app_engine, tenant_a, tenant_b):
    ctx = make_ctx(app_engine, SystemClock())
    registry = Registry()
    seen: list[int] = []

    @registry.handler("test.race")
    async def race(ctx: HandlerContext, event: Event) -> None:
        seen.append(event.id)
        await asyncio.sleep(0.005)
        await audit_effect(ctx, event)

    ids = [await emit(app_engine, tenant_a.id, "test.race") for _ in range(25)]
    ids += [await emit(app_engine, tenant_b.id, "test.race") for _ in range(15)]
    a = poller(ctx, registry, worker_id="wa", batch=7, concurrency=4)
    b = poller(ctx, registry, worker_id="wb", batch=7, concurrency=4)
    for _ in range(10):
        await asyncio.gather(
            a.run_once(reclaim=False, sample_stats=False), b.run_once(reclaim=False, sample_stats=False)
        )
    assert sorted(seen) == sorted(ids), "every event exactly once across both workers"
    assert await count(app_engine, tenant_a.id, AuditEvent) == 25
    assert await count(app_engine, tenant_b.id, AuditEvent) == 15
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        stats = await outbox_repo.stats(session)
    assert (stats["done"], stats["pending"], stats["in_flight"]) == (25, 0, 0)


async def test_unknown_event_type_goes_through_the_retry_path(app_engine, tenant_a):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    event_id = await emit(app_engine, tenant_a.id, "test.unknown")
    stats = await poller(ctx, Registry()).run_once(reclaim=False, sample_stats=False)
    assert (stats.claimed, stats.failed) == (1, 1)
    r = await row(app_engine, tenant_a.id, event_id)
    assert (r.status, r.attempts) == ("pending", 1)
    assert r.last_error.startswith("UnknownEventTypeError")


async def test_run_loop_wakes_on_publish_and_stops_claiming_on_drain(app_engine, tenant_a, redis):
    ctx = make_ctx(app_engine, SystemClock(), redis=redis)
    registry = Registry()
    done = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()

    @registry.handler("test.wake")
    async def wake(ctx: HandlerContext, event: Event) -> None:
        started.set()
        await release.wait()  # holds the handler so the drain must wait for it
        done.set()

    drainer = Drainer(deadline_s=5)
    p = poller(ctx, registry, drainer=drainer, tick_s=30.0)  # only a wake can trigger a pass quickly
    task = asyncio.create_task(p.run())
    await asyncio.sleep(0.3)  # first pass (empty) done, loop now sleeping on tick/wake
    event_id = await emit(app_engine, tenant_a.id, "test.wake")
    await redis.publish(OUTBOX_WAKE, "1")
    await asyncio.wait_for(started.wait(), timeout=3.0)

    drainer.begin("test")
    assert not p.accepting and drainer.in_flight == 1 and not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert done.is_set() and drainer.drained
    assert (await row(app_engine, tenant_a.id, event_id)).status == "done"


async def test_sample_stats_publishes_pending_and_lag_gauges(app_engine, tenant_a, tenant_b):
    clock = await anchored_clock(app_engine, ahead_s=90)
    ctx = make_ctx(app_engine, clock)
    for _ in range(3):
        await emit(app_engine, tenant_a.id, "test.gauge")
    await emit(app_engine, tenant_b.id, "test.gauge")
    sample = await poller(ctx, Registry()).sample_stats()
    assert sample["pending"] == 4 and 89 < sample["lag_seconds"] < 100
    assert METRICS.get_sample_value("outbox_pending") == 4
    assert METRICS.get_sample_value("outbox_lag_seconds") == sample["lag_seconds"]
    totals = (await dlq.stats_all(app_engine, now=clock.now()))["total"]
    assert totals["pending"] == 4 and totals["dead"] == 0
