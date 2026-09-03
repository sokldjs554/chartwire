"""§7.4 / §11.2-D chaos: a real stt-worker process is SIGSTOPped mid-session; its owner lease expires; a
second worker process acquires the session, ``XAUTOCLAIM``s the entries the frozen worker left pending
and finishes the session. The frozen worker is then SIGCONTed: it must notice the lost lease and stop
without duplicating segments or regressing ``stt_offsets``. Both drain cleanly on SIGTERM (exit 0).

Runs ``python -m chartwire.stt.worker`` with ``CHARTWIRE_*`` pointed at this work package's database /
Redis index (``chartwire_test_c`` / 3), the ``slow`` provider (150 ms per chunk) so a delivered batch is
still in progress when the process is frozen, a 2 s lease and a 500 ms autoclaim threshold.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from chartwire.db.models import OutboxEvent, TranscriptSegment
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.redis import keys
from tests.integration.test_stt_worker import (
    SCRIPTS_DIR,
    T01_CHUNKS,
    T01_FINALS,
    Producer,
    make_deps,
    offsets_of,
    seed,
    segments_of,
    state_of,
    wait_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.chaos]

REPO = Path(__file__).resolve().parents[2]
LEASE_MS = 2000
AUTOCLAIM_IDLE_MS = 500
SLOW_DELAY_MS = 150


def spawn(env: dict[str, str], node_id: str, log: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "chartwire.stt.worker"],
        env={**env, "CHARTWIRE_NODE_ID": node_id},
        cwd=REPO,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


async def test_sigstop_worker_is_taken_over_via_xautoclaim(
    migrated_db, factories, app_engine, redis, settings, tmp_path
):
    env = {
        **os.environ,
        "CHARTWIRE_DATABASE_URL": migrated_db.app,
        "CHARTWIRE_DATABASE_OWNER_URL": migrated_db.owner,
        "CHARTWIRE_REDIS_URL": migrated_db.redis,
        "CHARTWIRE_OBJECTSTORE": f"localfs:{tmp_path / 'objects'}",
        "CHARTWIRE_STT_PROVIDER": "slow",
        "CHARTWIRE_STT_SLOW_DELAY_MS": str(SLOW_DELAY_MS),
        "CHARTWIRE_STT_SIM_LATENCY": "0",
        "CHARTWIRE_STT_SCRIPTS_DIR": str(SCRIPTS_DIR),
        "CHARTWIRE_STT_LEASE_MS": str(LEASE_MS),
        "CHARTWIRE_STT_AUTOCLAIM_IDLE_MS": str(AUTOCLAIM_IDLE_MS),
        "CHARTWIRE_STT_WORKER_PORT": "0",
    }
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    await producer.produce(range(1, T01_CHUNKS + 1))
    await producer.end()
    stream = keys.sess_chunks(seeded.session_id)

    async def some_progress() -> bool:
        return (await offsets_of(app_engine, seeded))[0] >= 3

    async def transcribed() -> bool:
        return await state_of(app_engine, seeded) == "transcribed"

    victim = spawn(env, "w1", tmp_path / "victim.log")
    survivor = None
    try:
        await wait_for(some_progress, timeout_s=30, what="first worker consuming")
        os.kill(victim.pid, signal.SIGSTOP)
        frozen_at = await offsets_of(app_engine, seeded)
        assert frozen_at[0] < T01_CHUNKS, "frozen mid-session"
        pending_before = (await redis.xpending(stream, keys.STT_CONSUMER_GROUP))["pending"]
        assert pending_before > 0, "the frozen worker holds delivered-but-unacked entries"

        survivor = spawn(env, "w2", tmp_path / "survivor.log")
        await wait_for(transcribed, timeout_s=LEASE_MS / 1000 + 40, what="takeover + completion")

        # invariants while the first worker is still frozen
        rows = await segments_of(app_engine, seeded)
        assert [r.seq for r in rows] == list(range(T01_FINALS))
        assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, T01_FINALS - 1)
        assert (await redis.xpending(stream, keys.STT_CONSUMER_GROUP))["pending"] == 0
        consumers = {c["name"]: c for c in await redis.xinfo_consumers(stream, keys.STT_CONSUMER_GROUP)}
        assert any(name.startswith("stt:w2:") for name in consumers), "the survivor joined the group"
        assert all(c["pending"] == 0 for c in consumers.values())
        assert not await redis.sismember(keys.STT_ACTIVE, str(seeded.session_id))

        os.kill(victim.pid, signal.SIGCONT)
        victim.send_signal(signal.SIGTERM)
        assert victim.wait(timeout=20) == 0, "the thawed worker drains cleanly"
        survivor.send_signal(signal.SIGTERM)
        assert survivor.wait(timeout=20) == 0

        # nothing the thawed worker did after SIGCONT duplicated or regressed anything
        async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
            seqs = (await s.scalars(select(TranscriptSegment.seq).order_by(TranscriptSegment.seq))).all()
            events = (await s.scalars(select(OutboxEvent.event_type))).all()
        assert list(seqs) == list(range(T01_FINALS)) and events == ["session.transcribed"]
        assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, T01_FINALS - 1)
        assert await state_of(app_engine, seeded) == "transcribed"
    finally:
        for proc in (victim, survivor):
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(OSError):
                    os.kill(proc.pid, signal.SIGCONT)
                proc.kill()
                proc.wait(timeout=5)
    victim_log = (tmp_path / "victim.log").read_text()
    survivor_log = (tmp_path / "survivor.log").read_text()
    assert "session acquired" in victim_log and "session acquired" in survivor_log
    assert "entries autoclaimed" in survivor_log and "session transcribed" in survivor_log
    assert "stt-worker stopped" in victim_log and "stt-worker stopped" in survivor_log
    assert "session transcribed" not in victim_log
