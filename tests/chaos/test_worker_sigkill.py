"""§7.1 chaos: SIGKILL a real worker process mid-handler → the row stays ``in_flight`` under its lease →
a second worker reclaims it after expiry and completes the job exactly once (effects counted), with
``attempts`` unchanged. The survivor is then SIGTERMed and must drain cleanly (exit 0).

Runs ``tests/chaos/run_worker.py`` as a subprocess with ``CHARTWIRE_*`` pointed at this work package's
test database / Redis index (``chartwire_test_g`` / 7).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from chartwire.db.models import AuditEvent, OutboxEvent, ProcessedEvent
from chartwire.db.tenant import TenantCtx, tenant_tx
from tests.chaos import chaos_handler
from tests.integration.test_outbox_poller import emit

pytestmark = [pytest.mark.integration, pytest.mark.chaos]

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "tests" / "chaos" / "run_worker.py"


def spawn(env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(RUNNER)],
        env=env,
        cwd=REPO,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


async def wait_for(predicate, *, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def test_sigkill_mid_handler_then_second_worker_completes_once(
    migrated_db, app_engine, tenant_a, tmp_path
):
    started = tmp_path / "started"
    flag = tmp_path / "flag"
    env = {
        **os.environ,
        "CHARTWIRE_DATABASE_URL": migrated_db.app,
        "CHARTWIRE_DATABASE_OWNER_URL": migrated_db.owner,
        "CHARTWIRE_REDIS_URL": migrated_db.redis,
        "CHARTWIRE_OBJECTSTORE": f"localfs:{tmp_path / 'objects'}",
        "CHARTWIRE_NODE_ID": "chaos",
        "PYTHONPATH": str(REPO),
        chaos_handler.STARTED_ENV: str(started),
        chaos_handler.FLAG_ENV: str(flag),
    }
    event_id = await emit(app_engine, tenant_a.id, chaos_handler.EVENT_TYPE)

    async def fetch() -> OutboxEvent:
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
            return (await session.scalars(select(OutboxEvent).where(OutboxEvent.id == event_id))).one()

    async def effects() -> tuple[int, int]:
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
            audits = (await session.execute(select(func.count()).select_from(AuditEvent))).scalar_one()
            ledger = (await session.execute(select(func.count()).select_from(ProcessedEvent))).scalar_one()
        return int(audits), int(ledger)

    victim = spawn(env, tmp_path / "victim.log")
    survivor = None
    try:
        await wait_for(lambda: asyncio.sleep(0, started.exists()), timeout_s=30, what="handler start")
        row = await fetch()
        assert row.status == "in_flight" and row.locked_by is not None and row.locked_by.startswith("chaos:")
        os.kill(victim.pid, signal.SIGKILL)
        victim.wait(timeout=10)
        assert victim.returncode == -signal.SIGKILL
        assert await effects() == (0, 0), "the handler transaction died with the process"
        row = await fetch()
        assert row.status == "in_flight", "the lease still holds the row for the dead worker"

        started.unlink()
        flag.write_text("go")  # the survivor's handler may proceed immediately
        survivor = spawn(env, tmp_path / "survivor.log")

        async def done() -> bool:
            return (await fetch()).status == "done"

        await wait_for(done, timeout_s=chaos_handler.LEASE_S + 30, what="reclaim + completion")
        row = await fetch()
        assert row.attempts == 0, "reclaim is not a failed attempt"
        assert row.locked_by is None and row.lease_until is None
        assert await effects() == (1, 1), "exactly one effect, one ledger row"
        assert started.exists() and started.read_text() == str(survivor.pid)

        survivor.send_signal(signal.SIGTERM)
        assert survivor.wait(timeout=15) == 0, "graceful drain exits 0"
        assert await effects() == (1, 1)
    finally:
        for proc in (victim, survivor):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    victim_log = (tmp_path / "victim.log").read_text()
    survivor_log = (tmp_path / "survivor.log").read_text()
    assert "outbox claimed" in victim_log and "outbox reclaimed stuck rows" in survivor_log
    assert "worker stopped" in survivor_log
