"""Worker tickers against the real database (§7.2): ``outbox_prune`` (24 h, batches), ``session_reaper``
(idle recording session → ended + end marker + audit; a fresh Redis ``updated_at`` keeps it alive),
``partition_ensure`` (three months present, default partition empty → gauge 0), plus the ``Ticker``
loop itself (failure isolation, drain stops it, in-flight tracking).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chartwire.core.clock import FakeClock
from chartwire.core.config import Settings
from chartwire.db.models import AuditEvent
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops.drain import Drainer
from chartwire.ops.metrics import REGISTRY as METRICS
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys
from chartwire.worker.handlers import outbox_prune, partition_ensure, session_reaper
from chartwire.worker.tickers import Ticker
from tests.integration.test_outbox_poller import anchored_clock, emit

pytestmark = pytest.mark.integration


def make_ctx(engine: AsyncEngine, clock: FakeClock, redis=None, **overrides) -> HandlerContext:
    settings = Settings(database_url=engine.url.render_as_string(hide_password=False), **overrides)
    return HandlerContext(
        engine=engine, redis=redis, objectstore=None, clock=clock, settings=settings, kek=None, keycache=None
    )


# --------------------------------------------------------------------------- Ticker


async def test_ticker_isolates_failures_and_stops_on_drain():
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("first tick fails, ticker survives")

    drainer = Drainer(deadline_s=2)
    ticker = Ticker("flaky", 0.02, flaky, drainer=drainer, run_at_start=True)
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.15)
    drainer.begin("test")
    await asyncio.wait_for(task, timeout=1.0)
    assert ticker.failures == 1 and ticker.ticks >= 3 and len(calls) == ticker.ticks
    assert drainer.drained
    with pytest.raises(ValueError):
        Ticker("bad", 0, flaky)


# --------------------------------------------------------------------------- outbox_prune


async def test_outbox_prune_deletes_only_done_rows_older_than_24h(app_engine, tenant_a, tenant_b):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    old_a = await emit(app_engine, tenant_a.id, "test.prune")
    fresh_a = await emit(app_engine, tenant_a.id, "test.prune")
    pending_a = await emit(app_engine, tenant_a.id, "test.prune")
    old_b = await emit(app_engine, tenant_b.id, "test.prune")
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        await outbox_repo.mark_done(session, old_a, now=clock.now() - timedelta(hours=25))
        await outbox_repo.mark_done(session, fresh_a, now=clock.now() - timedelta(hours=1))
    async with tenant_tx(app_engine, TenantCtx.service(tenant_b.id)) as session:
        await outbox_repo.mark_done(session, old_b, now=clock.now() - timedelta(hours=30))

    assert await outbox_prune.tick(ctx, batch=1) == 2  # batch=1 exercises the loop-until-short-batch path
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        stats = await outbox_repo.stats(session, now=clock.now())
    assert (stats["done"], stats["pending"]) == (1, 1)
    assert await outbox_prune.tick(ctx) == 0
    del pending_a


# --------------------------------------------------------------------------- session_reaper


async def _state(engine: AsyncEngine, tenant_id, session_id) -> str:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
        return (await session.scalars(select(SessionModel.state).where(SessionModel.id == session_id))).one()


async def test_session_reaper_ends_idle_sessions_with_marker_and_audit(
    app_engine, redis, tenant_a, session_a
):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock, redis=redis, session_idle_timeout_s=3600)
    assert await session_reaper.tick(ctx) == 0, "just started: not idle"

    clock.advance(3600 * 2)
    # a live Redis hash with a fresh updated_at keeps the session alive even if PG's updated_at is old
    await redis.hset(keys.sess(session_a.id), mapping={"updated_at": clock.now().isoformat(), "epoch": "1"})
    assert await session_reaper.tick(ctx) == 0
    assert await _state(app_engine, tenant_a.id, session_a.id) == "recording"

    await redis.hset(keys.sess(session_a.id), "updated_at", (clock.now() - timedelta(hours=2)).isoformat())
    assert await session_reaper.tick(ctx) == 1
    assert await _state(app_engine, tenant_a.id, session_a.id) == "ended"
    assert await redis.hget(keys.sess(session_a.id), "state") == "ended"
    entries = await redis.xrange(keys.sess_chunks(session_a.id))
    assert len(entries) == 1 and entries[0][1] == {"end": "1", "ep": str(session_a.epoch)}
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        actions = (await session.scalars(select(AuditEvent.action))).all()
    assert actions == ["session.ended"]
    assert await session_reaper.tick(ctx) == 0, "ended sessions are not scanned again"


def test_parse_updated_at_accepts_iso_and_epoch_forms():
    iso = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert session_reaper.parse_updated_at(iso.isoformat()) == iso
    assert session_reaper.parse_updated_at(str(iso.timestamp())) == iso
    assert session_reaper.parse_updated_at(str(int(iso.timestamp() * 1000))) == iso
    assert session_reaper.parse_updated_at("2026-09-01T09:00:00") == iso  # naive → UTC
    assert session_reaper.parse_updated_at(None) is None
    assert session_reaper.parse_updated_at("garbage") is None


# --------------------------------------------------------------------------- partition_ensure


async def test_partition_ensure_creates_months_ahead_and_reports_empty_default(app_engine, tenant_a):
    clock = await anchored_clock(app_engine)
    ctx = make_ctx(app_engine, clock)
    summary = await partition_ensure.tick(ctx)
    assert len(summary["partitions"]) == partition_ensure.MONTHS_AHEAD + 1
    assert all(name.startswith("transcript_segments_y") for name in summary["partitions"])
    assert summary["default_rows"] == 0
    assert METRICS.get_sample_value("segments_default_partition_rows") == 0
    async with AsyncSession(app_engine) as session:
        present = (await session.execute(select(func.to_regclass(summary["partitions"][-1])))).scalar_one()
    assert present == summary["partitions"][-1]
