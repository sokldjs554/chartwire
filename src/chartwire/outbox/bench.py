"""Scenario H bench (§11.2): N ``noop`` outbox events across T tenants, K worker **processes**, one SIGKILL.

Measures the processing rate of the per-tenant claim loop (ADR-0001: no BYPASSRLS role, so every pass
costs one ``claim_batch`` per tenant). The workers are real processes running the real :class:`Poller`
loop (wake subscription, tick, stuck reclaim, drain on SIGTERM) — one asyncio loop per process is the
deployment shape, and an in-process "worker" would only measure one CPU. With ``kill_at`` the parent
SIGKILLs worker 0 once that fraction of events is done; its ``in_flight`` rows sit until the lease
expires and a survivor's ``reclaim_stuck`` returns them (``reclaimed`` in the report).

Timing: events are inserted first (``insert_s``, excluded), then the workers start; ``elapsed_s`` runs
from the first worker's ready mark to the last event leaving ``pending``/``in_flight``.

Writes ``docs/loadtest/H.json`` ``{events_per_s, dlq_count, ...}`` with the §11.1 report header.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from sqlalchemy import literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.db.engine import make_engine
from chartwire.db.models import OutboxEvent as OutboxRow
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.eval.report import build_report
from chartwire.ops.drain import Drainer
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.poller import Poller
from chartwire.outbox.registry import Registry
from chartwire.outbox.runtime import build_context, close_context

log = logging.getLogger(__name__)

BENCH_EVENT = "bench.tick"
BENCH_LEASE_S = 5
TENANT_SLUG = "bench-{i:03d}"
DEFAULT_OUT = Path("docs/loadtest/H.json")
INSERT_CHUNK = 1000
WORKER_POOL_SIZE = 10
WORKER_STOP_TIMEOUT_S = 20.0


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


async def insert_events(engine: Any, tenants: list[UUID], events: int, run_id: str) -> int:
    """Bulk ``outbox_events`` rows (``ON CONFLICT DO NOTHING`` on the idempotency key), per tenant."""
    inserted = 0
    per_tenant = [
        events // len(tenants) + (1 if i < events % len(tenants) else 0) for i in range(len(tenants))
    ]
    for tenant_id, n in zip(tenants, per_tenant, strict=True):
        rows = [
            {
                "tenant_id": tenant_id,
                "aggregate_type": "bench",
                "aggregate_id": (aggregate_id := uuid7()),
                "event_type": BENCH_EVENT,
                "payload": {"i": i},
                "idempotency_key": f"{BENCH_EVENT}:{aggregate_id}:{run_id}",
            }
            for i in range(n)
        ]
        async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
            for start in range(0, len(rows), INSERT_CHUNK):
                stmt = (
                    pg_insert(OutboxRow)
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(OutboxRow.id)
                )
                inserted += len((await session.execute(stmt, rows[start : start + INSERT_CHUNK])).all())
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


# --- worker process -------------------------------------------------------------------------------------


async def worker_main(settings: Settings, tenants: list[UUID], index: int, stats_path: Path) -> int:
    """One bench worker: the real poller loop until SIGTERM, then its totals as JSON."""
    ctx = build_context(settings, pool_size=WORKER_POOL_SIZE)
    drainer = Drainer(deadline_s=BENCH_LEASE_S * 2)
    drainer.install()
    poller = Poller(
        ctx,
        worker_id=f"bench-{index}",
        registry=bench_registry(),
        drainer=drainer,
        tenants=tenants,
        tick_s=0.05,
        reclaim_every_s=BENCH_LEASE_S,
        stats_every_s=1e9,
    )
    try:
        await asyncio.to_thread(stats_path.with_suffix(".ready").touch)
        await poller.run()
    finally:
        drainer.uninstall()
        await close_context(ctx)
    totals = poller.totals
    summary = {
        "worker": index,
        "passes": poller.passes,
        "processed": totals.processed + totals.skipped,
        "reclaimed": totals.reclaimed,
        "claim_ms": totals.claim_ms,
    }
    await asyncio.to_thread(stats_path.write_text, json.dumps(summary))
    return 0


def _worker_env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "CHARTWIRE_DATABASE_URL": settings.database_url,
        "CHARTWIRE_REDIS_URL": settings.redis_url,
        "CHARTWIRE_OBJECTSTORE": settings.objectstore,
        "CHARTWIRE_KEK_MASTER": settings.kek_master,
    }


def spawn_worker(
    settings: Settings, tenants: list[UUID], index: int, stats_path: Path
) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "chartwire.outbox.bench",
        "--worker",
        str(index),
        "--stats",
        str(stats_path),
        "--tenants",
        ",".join(str(t) for t in tenants),
    ]
    return subprocess.Popen(
        cmd, env=_worker_env(settings), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )


def _existing(paths: set[Path]) -> set[Path]:
    return {p for p in paths if p.exists()}


def _read_stats(paths: list[Path]) -> list[dict[str, Any]]:
    """Per-worker totals; a SIGKILLed worker never wrote its file."""
    return [json.loads(p.read_text()) for p in paths if p.exists()]


async def _wait_ready(paths: list[Path], timeout_s: float = 60.0) -> float:
    """Returns the monotonic time the first worker became ready; raises if any never does."""
    deadline = time.monotonic() + timeout_s
    first: float | None = None
    remaining = {p.with_suffix(".ready") for p in paths}
    while remaining:
        if time.monotonic() > deadline:
            raise TimeoutError("bench workers did not start")
        ready = await asyncio.to_thread(_existing, remaining)
        if ready:
            first = first or time.monotonic()
            remaining -= ready
        await asyncio.sleep(0.02)
    assert first is not None
    return first


def _stop(proc: subprocess.Popen[bytes]) -> int | None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=WORKER_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.returncode


# --- parent ----------------------------------------------------------------------------------------------


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
    engine = make_engine(settings.database_url, pool_size=4)
    procs: list[subprocess.Popen[bytes]] = []
    try:
        run_id = uuid7().hex
        insert_started = time.monotonic()
        inserted = await insert_events(engine, tenant_ids, events, run_id)
        insert_s = time.monotonic() - insert_started
        with TemporaryDirectory(prefix="chartwire-bench-") as tmp:
            stats_paths = [Path(tmp) / f"worker-{i}.json" for i in range(workers)]
            procs = [spawn_worker(settings, tenant_ids, i, p) for i, p in enumerate(stats_paths)]
            started = await _wait_ready(stats_paths)
            killed = False
            while True:
                open_rows, _ = await pending_total(engine, tenant_ids)
                if open_rows == 0:
                    break
                progress = 1.0 - open_rows / max(inserted, 1)
                if kill_at is not None and not killed and workers > 1 and progress >= kill_at:
                    os.kill(procs[0].pid, signal.SIGKILL)  # a real crash mid-pass; survivors must reclaim
                    procs[0].wait(timeout=10)
                    killed = True
                    log.info("bench: worker 0 killed", extra={"progress": round(progress, 3)})
                if any(p.poll() is not None for p in procs[1 if killed else 0 :]):
                    raise RuntimeError("a bench worker exited early")
                await asyncio.sleep(0.2)
            elapsed = time.monotonic() - started
            exit_codes = [_stop(p) for p in procs]
            per_worker = await asyncio.to_thread(_read_stats, stats_paths)
        _, dlq_count = await pending_total(engine, tenant_ids)
        claim_ms = sorted(ms for w in per_worker for ms in w["claim_ms"])
        body: dict[str, Any] = {
            "scenario": "H",
            "events": inserted,
            "workers": workers,
            "tenants": tenants,
            "insert_s": round(insert_s, 3),
            "elapsed_s": round(elapsed, 3),
            "events_per_s": round(inserted / elapsed, 1) if elapsed > 0 else None,
            "dlq_count": dlq_count,
            "worker_killed": killed,
            "reclaimed": sum(w["reclaimed"] for w in per_worker),
            "passes": sum(w["passes"] for w in per_worker),
            "claim_ms_p50": round(statistics.median(claim_ms), 3) if claim_ms else None,
            "claim_ms_p95": round(claim_ms[int(0.95 * (len(claim_ms) - 1))], 3) if claim_ms else None,
            "lease_s": BENCH_LEASE_S,
            "worker_exit_codes": exit_codes,
            "per_worker": [
                {k: w[k] for k in ("worker", "passes", "processed", "reclaimed")} for w in per_worker
            ],
        }
        body["cleaned_rows"] = await cleanup(engine, tenant_ids)
        pg_version = await _pg_version(engine)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        await engine.dispose()
    report = build_report(seed, body, pg_version=pg_version)
    if out is not None:
        await asyncio.to_thread(write_report, out, report)
    return report


def write_report(out: Path, report: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _pg_version(engine: Any) -> str | None:
    with contextlib.suppress(Exception):
        async with AsyncSession(engine) as session:
            return str((await session.execute(text("SHOW server_version"))).scalar_one())
    return None


def main(argv: list[str] | None = None) -> int:
    """``python -m chartwire.outbox.bench --worker N --stats <path> --tenants a,b,…`` (spawned by the parent)."""
    parser = argparse.ArgumentParser(description="scenario H bench worker process")
    parser.add_argument("--worker", type=int, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--tenants", required=True, help="comma-separated tenant ids")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    tenants = [UUID(t) for t in args.tenants.split(",") if t]
    return asyncio.run(worker_main(Settings(), tenants, args.worker, args.stats))


if __name__ == "__main__":
    sys.exit(main())
