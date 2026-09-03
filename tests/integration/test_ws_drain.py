"""Drain choreography against a real uvicorn (spec §6.4 rule 7): ``bye{drain}`` → flush → ``1012``.

Own module because a drained node refuses new connections for the rest of its life — the server here is
started fresh and thrown away. The drain is started from the test thread through the server loop, exactly
what WP-G's ``Drainer`` does on SIGTERM (``registry.begin`` is registered as its ``on_begin`` hook)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from chartwire.db.models import AudioChunk
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from tests.integration.test_ws_watch import collect
from tests.ws._live import (
    Recorder,
    Seeded,
    ingest_ticket,
    live_server,
    open_viewer,
    seed_session,
    viewer_recv,
)

pytestmark = pytest.mark.integration
PORT = 8101


@pytest.fixture(scope="module")
def server(migrated_db, tmp_path_factory) -> Iterator:
    objects: Path = tmp_path_factory.mktemp("objects")
    with live_server(migrated_db.app, migrated_db.redis, objects, port=PORT, node_id="node-drain") as s:
        yield s


@pytest.fixture
async def seeded(factories, app_engine, settings) -> Seeded:
    return await seed_session(factories, app_engine, settings)


async def test_drain_sends_bye_flushes_the_ledger_and_closes_1012(server, redis, seeded, app_engine):
    viewer = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    assert (await viewer_recv(viewer))["t"] == "welcome"
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.hello(await ingest_ticket(redis, seeded)))["t"] == "welcome"
    for seq in range(1, 41):
        await rec.send_frame(seq)
    while rec.ack_seq < 32:  # some rows durable, the tail still in flight when the drain starts
        assert (await rec.recv())["t"] != "closed"

    rt = server.app.state.ws_runtime
    server.app.state.loop.call_soon_threadsafe(rt.registry.begin)

    msgs = []
    while (msg := await rec.recv())["t"] != "closed":
        msgs.append(msg)
    byes = [m for m in msgs if m["t"] == "bye"]
    assert len(byes) == 1 and byes[0]["reason"] == "drain" and byes[0]["ack_seq"] >= 32
    assert rec.close_code == 1012
    assert rec.ack_seq == 40, "every chunk stored on this connection was acked before the close"
    assert rec.acks == sorted(rec.acks)
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        count = (
            await s.execute(
                select(func.count()).select_from(AudioChunk).where(AudioChunk.session_id == seeded.session_id)
            )
        ).scalar_one()
        row = await sessions_repo.get_session(s, seeded.session_id)
    assert count == 40 and row is not None and row.state == "recording", "resumable on another node"

    seen = await collect(viewer, until=lambda ms: False, wait_s=5)
    assert {"t": "bye", "reason": "drain"} in seen and seen[-1] == {"t": "closed", "code": 1012}

    late = Recorder(server.url)
    await late.connect()
    assert (await late.recv()) == {"t": "closed", "code": 1012}, "a draining node refuses new connections"
    assert rt.registry.draining and len(rt.registry) == 0
    await asyncio.sleep(0)
