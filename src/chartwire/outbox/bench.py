"""Scenario H micro-bench (§11.2): N ``noop`` outbox events across T tenants, K pollers, one "crash".

Measures the *processing* rate of the per-tenant claim loop (ADR-0001: no BYPASSRLS role, so every
pass costs one ``claim_batch`` per active tenant) — insert time is excluded. ``kill_at`` cancels one
poller mid-pass once that fraction of events is done; its ``in_flight`` rows sit until the lease
expires and the survivors' ``reclaim_stuck`` step returns them (``reclaimed`` in the report). The real
SIGKILL path is ``tests/chaos/test_worker_sigkill.py``; here the crash is an in-process cancel.

Writes ``docs/loadtest/H.json`` ``{events_per_s, dlq_count, ...}`` with the §11.1 report header.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.db.engine import make_engine
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.eval.report import build_report
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.poller import Poller
from chartwire.outbox.registry import Registry
from chartwire.outbox.runtime import build_context, close_context

log = logging.getLogger(__name__)

BENCH_EVENT = "bench.tick"
BENCH_LEASE_S = 5
TENANT_SLUG = "bench-{i:03d}"
DEFAULT_OUT = Path("docs/loadtest/H.json")


def bench_registry() -> Registry:
    """A private registry: the bench never touches the process-wide ``REGISTRY``."""
    registry = Registry()

    @registry.handler(BENCH_EVENT, lease_s=BENCH_LEASE_S, name="bench_noop")
    async def noop(ctx: HandlerContext, event: OutboxEvent) -> None:
        async with ctx.tenant_tx(event.tenant_id) as session:  # one round trip, like a real handler
            await session.execute(select(literal(1)))

    return registry


async def ensure_tenants(owner_url: str, count: int) -> list[UUID]:
    """``bench-000`` … as owner (``tenants`` is owner-write); idempotent across runs."""
    engine = make_engine(owner_url, pool_size=2)
    ids: list[UUID] = []
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            for i in range(count):
                slug = TENANT_SLUG.format(i=i)
                tenant = await tenancy_repo.get_tenant_by_slug(session, slug)
                if tenant is None:
                    tenant = await tenancy_repo.create_tenant(
                        session,
                        slug=slug,
                        name=f"가상의원-{slug}",
                        kek_ref=f"local:{slug}",
                        record_key_wrapped=bytes(48),
                    )
                ids.append(tenant.id)
    finally:
        await engine.dispose()
    return ids


async def insert_events(ctx: HandlerContext, tenants: list[UUID], events: int, run_id: str) -> int:
    inserted = 0
    per_tenant = [
        events // len(tenants) + (1 if i < events % len(tenants) else 0) for i in range(len(tenants))
    ]
    for tenant_id, n in zip(tenants, per_tenant, strict=True):
        async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as session:
            for i in range(n):
                aggregate_id = uuid7()
                if (
                    await outbox_repo.insert_event(
                        session,
                        tenant_id=tenant_id,
                        aggregate_type="bench",
                        aggregate_id=aggregate_id,
                        event_type=BENCH_EVENT,
                        payload={"i": i},
                        idempotency_key=f"{BENCH_EVENT}:{aggregate_id}:{run_id}",
                    )
                    is not None
                ):
                    inserted += 1
    return inserted


async def pending_total(engine: Any, tenants: list[UUID]) -> tuple[int, int]:
    """``(pending + in_flight, dead)`` over the bench tenants."""
    open_rows = dead = 0
    for tenant_id in tenants:
        async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
            row = await outbox_repo.stats(session)
        open_rows += int(row["pending"]) + int(row["in_flight"])
        dead += int(row["dead"])
    return open_rows, dead


async def cleanup(engine: Any, tenants: list[UUID]) -> int:
    """Delete the bench's ``done`` rows now (``prune_done`` with a far-future clock)."""
    from datetime import UTC, datetime, timedelta

    future = datetime.now(tz=UTC) + timedelta(days=2)
    deleted = 0
    for tenant_id in tenants:
        while True:
            async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
                n = await outbox_repo.prune_done(session, batch=5000, now=future)
            deleted += n
            if n < 5000:
                break
    return deleted


class _Worker:
    def __init__(self, poller: Poller) -> None:
        self.poller = poller
        self.claim_ms: list[float] = []
        self.processed = 0
        self.reclaimed = 0
        self.passes = 0

    async def run(self, done: asyncio.Event) -> None:
        idle = 0
        while not done.is_set():
            stats = await self.poller.run_once(reclaim=None, sample_stats=False)
            self.passes += 1
            self.claim_ms.extend(stats.claim_ms)
            self.processed += stats.processed + stats.skipped
            self.reclaimed += stats.reclaimed
            idle = idle + 1 if stats.claimed == 0 else 0
            if idle:
                await asyncio.sleep(min(0.05 * idle, 0.5))


async def run_bench(
    settings: Settings,
    *,
    events: int,
    workers: int = 2,
    tenants: int = 30,
    seed: int = 42,
    kill_at: float | None = 0.25,
    out: Path | None = DEFAULT_OUT,
) -> dict[str, Any]:
    if events < 1 or workers < 1 or tenants < 1:
        raise ValueError("events, workers and tenants must be >= 1")
    tenant_ids = await ensure_tenants(settings.database_owner_url, tenants)
    ctx = build_context(settings, pool_size=max(workers * 8, 8))
    registry = bench_registry()
    try:
        run_id = uuid7().hex
        inserted = await insert_events(ctx, tenant_ids, events, run_id)
        pollers = [
            Poller(
                ctx,
                worker_id=f"bench-{i}",
                registry=registry,
                tick_s=0.05,
                reclaim_every_s=BENCH_LEASE_S,
                stats_every_s=1e9,
                tenant_cache_s=1e9,
            )
            for i in range(workers)
        ]
        runners = [_Worker(p) for p in pollers]
        done = asyncio.Event()
        started = time.monotonic()
        tasks = [asyncio.create_task(r.run(done)) for r in runners]
        killed = False
        while True:
            open_rows, _ = await pending_total(ctx.engine, tenant_ids)
            if open_rows == 0:
                break
            progress = 1.0 - open_rows / max(inserted, 1)
            if kill_at is not None and not killed and workers > 1 and progress >= kill_at:
                tasks[0].cancel()  # "crash" one worker mid-pass; its in_flight rows must be reclaimed
                killed = True
                log.info("bench: worker 0 cancelled", extra={"progress": round(progress, 3)})
            await asyncio.sleep(0.2)
        elapsed = time.monotonic() - started
        done.set()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _, dlq_count = await pending_total(ctx.engine, tenant_ids)
        claim_ms = sorted(ms for r in runners for ms in r.claim_ms)
        body: dict[str, Any] = {
            "scenario": "H",
            "events": inserted,
            "workers": workers,
            "tenants": tenants,
            "elapsed_s": round(elapsed, 3),
            "events_per_s": round(inserted / elapsed, 1) if elapsed > 0 else None,
            "dlq_count": dlq_count,
            "worker_killed": killed,
            "reclaimed": sum(r.reclaimed for r in runners),
            "passes": sum(r.passes for r in runners),
            "claim_ms_p50": round(statistics.median(claim_ms), 3) if claim_ms else None,
            "claim_ms_p95": round(claim_ms[int(0.95 * (len(claim_ms) - 1))], 3) if claim_ms else None,
            "lease_s": BENCH_LEASE_S,
        }
        body["cleaned_rows"] = await cleanup(ctx.engine, tenant_ids)
    finally:
        await close_context(ctx)
    report = build_report(seed, body, pg_version=await _pg_version(settings))
    if out is not None:
        await asyncio.to_thread(write_report, out, report)
    return report


def write_report(out: Path, report: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _pg_version(settings: Settings) -> str | None:
    engine = make_engine(settings.database_url, pool_size=1)
    try:
        async with AsyncSession(engine) as session:
            from sqlalchemy import text

            return str((await session.execute(text("SHOW server_version"))).scalar_one())
    except Exception:
        return None
    finally:
        await engine.dispose()
