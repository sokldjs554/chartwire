"""Recorder e2e against a real uvicorn (port 8101), PostgreSQL and Redis (spec §6.1, §6.4, §15 gate).

Invariants checked on the ledger after every scenario: the set of ``audio_chunks.seq`` equals the set
the recorder sent (no loss), the primary key makes duplicates impossible, every ``ack`` is ≤ the rows
committed at that moment (never ahead of the ledger), and the object store holds a ciphertext per row
that decrypts under the session DEK to the exact payload."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.crypto import Envelope, aad
from chartwire.db.models import AudioChunk, AuditEvent
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore import LocalFs, chunk_key
from chartwire.redis import keys
from chartwire.ws.codec import FLAG_LAST_CHUNK, FrameHeader, encode_frame
from tests.ws._live import (
    Recorder,
    Seeded,
    dumps,
    frame,
    ingest_ticket,
    live_server,
    payload_for,
    seed_session,
    sha_of,
)

pytestmark = pytest.mark.integration
PORT = 8101


@pytest.fixture(scope="module")
def objects_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("objects")


@pytest.fixture(scope="module")
def server(migrated_db, objects_dir: Path) -> Iterator:
    with live_server(migrated_db.app, migrated_db.redis, objects_dir, port=PORT) as s:
        yield s


@pytest.fixture
async def seeded(factories, app_engine, settings) -> Seeded:
    return await seed_session(factories, app_engine, settings)


async def ledger_rows(engine: AsyncEngine, seeded: Seeded) -> list[AudioChunk]:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        stmt = select(AudioChunk).where(AudioChunk.session_id == seeded.session_id).order_by(AudioChunk.seq)
        return list((await s.scalars(stmt)).all())


async def session_row(engine: AsyncEngine, seeded: Seeded):
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        return await sessions_repo.get_session(s, seeded.session_id)


async def assert_ledger_complete(
    engine: AsyncEngine, seeded: Seeded, objects_dir: Path, final: int, seed: int = 0
) -> None:
    rows = await ledger_rows(engine, seeded)
    assert [r.seq for r in rows] == list(range(1, final + 1)), "loss or gap in the ledger"
    store = LocalFs(objects_dir)
    for row in rows[
        :: max(1, final // 10)
    ]:  # sample: ciphertext decrypts to the exact bytes the recorder sent
        assert row.sha256 == sha_of(row.seq, seed) and row.byte_len == len(payload_for(row.seq, seed))
        blob = await store.get(chunk_key(seeded.tenant_id, seeded.session_id, row.seq))
        plain = Envelope.decrypt(
            seeded.dek, blob, aad(seeded.tenant_id, "session", seeded.session_id, f"chunk:{row.seq}")
        )
        assert plain == payload_for(row.seq, seed)
        assert row.storage_key == chunk_key(seeded.tenant_id, seeded.session_id, row.seq)


# --- happy path ------------------------------------------------------------------------------------------


async def test_happy_path_300_chunks_ack_equals_final(server, redis, seeded, app_engine, objects_dir):
    rec = Recorder(server.url)
    await rec.connect()
    welcome = await rec.hello(await ingest_ticket(redis, seeded))
    assert (
        welcome["t"] == "welcome"
        and welcome["epoch"] == 1
        and welcome["ack_seq"] == 0
        and welcome["missing"] == []
    )
    assert welcome["credit"] == 50 and welcome["heartbeat_ms"] == 15000
    bye = await rec.stream(upto=300)
    assert bye == {"t": "bye", "reason": "ended", "ack_seq": 300}
    closed = await rec.recv()
    assert closed == {"t": "closed", "code": 1000}
    assert rec.acks == sorted(rec.acks) and rec.acks[-1] == 300
    # a recorder that fills its credit (50) gets one ack per ledger batch (50 ms window), so acks are
    # about one per credit window here; the 8-chunk / 100 ms rule is a lower bound on ack *eagerness*
    assert len(rec.acks) >= 300 // (welcome["credit"] + 20)
    assert not any(m["t"] == "nack" for m in rec.received), "a lossless stream never triggers a nack"

    await assert_ledger_complete(app_engine, seeded, objects_dir, 300)
    row = await session_row(app_engine, seeded)
    assert (row.state, row.ack_seq, row.final_seq, row.epoch) == (
        "ended",
        300,
        300,
        1,
    ) and row.ended_at is not None
    entries = await redis.xrange(keys.sess_chunks(seeded.session_id))
    assert len(entries) == 301 and [e[1]["seq"] for e in entries[:-1]] == [str(s) for s in range(1, 301)]
    assert entries[-1][1] == {"end": "1", "ep": "1"} and entries[0][1]["ep"] == "1"
    assert set(entries[0][1]) == set(keys.CHUNK_FIELDS)
    hot = await redis.hgetall(keys.sess(seeded.session_id))
    assert (
        hot["state"] == "ended"
        and hot["ack_seq"] == "300"
        and await redis.ttl(keys.sess(seeded.session_id)) > 0
    )
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        actions = (await s.scalars(select(AuditEvent.action).order_by(AuditEvent.id))).all()
    assert actions == ["ws.hello", "session.ended"]


async def test_acks_never_run_ahead_of_the_ledger(server, redis, seeded, app_engine):
    """Every ack observed by the recorder is ≤ max ledgered seq at that instant."""
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    violations = 0
    checked = 0
    for seq in range(1, 41):
        await rec.send_frame(seq)
        msg = await rec.recv(wait_s=0.5) if seq % 8 == 0 else None
        if msg and msg["t"] == "ack":
            rows = await ledger_rows(app_engine, seeded)
            checked += 1
            violations += msg["ack_seq"] > (rows[-1].seq if rows else 0)
    assert checked >= 3 and violations == 0
    await rec.close()


# --- resume after a hard drop ------------------------------------------------------------------------------


async def test_forced_kill_then_resume_three_sessions_loss_and_dup_zero(
    server, redis, factories, app_engine, settings, objects_dir
):
    seeds = [await seed_session(factories, app_engine, settings, slug=f"clinic-{i}") for i in range(3)]

    async def one(i: int, seeded: Seeded) -> None:
        rec = Recorder(server.url, seed=i)
        await rec.connect()
        assert (await rec.hello(await ingest_ticket(redis, seeded)))["t"] == "welcome"
        killed = await rec.stream(upto=200, kill_at=80 + i)
        assert killed["t"] == "killed"
        acked_before = rec.ack_seq
        await asyncio.sleep(0.3)  # the ledger keeps committing rows that were in flight when the socket died
        rec2 = Recorder(server.url, seed=i)
        await rec2.connect()
        welcome = await rec2.hello(
            await ingest_ticket(redis, seeded), resume=True, last_sent_seq=rec.last_sent
        )
        assert welcome["t"] == "welcome" and welcome["epoch"] == 2
        assert welcome["ack_seq"] >= acked_before, (
            "welcome.ack_seq covers rows that became durable after the drop"
        )
        rec2.last_sent = rec.last_sent
        bye = await rec2.stream(upto=200)
        assert bye == {"t": "bye", "reason": "ended", "ack_seq": 200}
        assert await rec2.recv() == {"t": "closed", "code": 1000}
        assert rec2.acks == sorted(rec2.acks)
        await assert_ledger_complete(app_engine, seeded, objects_dir, 200, seed=i)
        row = await session_row(app_engine, seeded)
        assert (row.state, row.ack_seq, row.final_seq, row.epoch) == ("ended", 200, 200, 2)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(seeds)))


async def test_resume_gap_beyond_ring_buffer_closes_4008_and_ends_session(server, redis, seeded, app_engine):
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.hello(await ingest_ticket(redis, seeded)))["t"] == "welcome"
    await rec.close()
    rec2 = Recorder(server.url)
    await rec2.connect()
    err = await rec2.hello(await ingest_ticket(redis, seeded), resume=True, last_sent_seq=400)
    assert err["t"] == "error" and err["code"] == 4008
    assert await rec2.recv() == {"t": "closed", "code": 4008}
    assert (await session_row(app_engine, seeded)).state == "ended"


# --- epoch fencing ------------------------------------------------------------------------------------------


async def test_second_hello_supersedes_first_with_4409(server, redis, seeded, app_engine, objects_dir):
    first = Recorder(server.url)
    await first.connect()
    assert (await first.hello(await ingest_ticket(redis, seeded)))["epoch"] == 1
    for seq in range(1, 6):
        await first.send_frame(seq)
    while first.ack_seq < 5:
        assert (await first.recv())["t"] != "closed"
    second = Recorder(server.url)
    await second.connect()
    welcome = await second.hello(await ingest_ticket(redis, seeded), resume=True, last_sent_seq=5)
    assert welcome["epoch"] == 2 and welcome["ack_seq"] == 5 and welcome["missing"] == []
    msgs = [await first.recv() for _ in range(3)]
    assert {"t": "bye", "reason": "superseded", "ack_seq": 5} in msgs
    assert {"t": "closed", "code": 4409} in msgs
    second.last_sent = 5
    assert (await second.stream(upto=10)) == {"t": "bye", "reason": "ended", "ack_seq": 10}
    await assert_ledger_complete(app_engine, seeded, objects_dir, 10)
    assert (await redis.hgetall(keys.sess(seeded.session_id)))["epoch"] == "2"


async def test_stale_epoch_frames_never_reach_the_stream(server, redis, seeded, app_engine):
    """The window between a hello on another node (epoch bumped in Redis) and the delivery of its
    ``superseded`` notice: frames the old connection still sends are fenced by ``xadd_chunk.lua`` and
    never acked. The epoch is bumped by hand so the notice is withheld deterministically."""
    first = Recorder(server.url)
    await first.connect()
    assert (await first.hello(await ingest_ticket(redis, seeded)))["epoch"] == 1
    for seq in range(1, 4):
        await first.send_frame(seq)
    while first.ack_seq < 3:
        assert (await first.recv())["t"] != "closed"
    await redis.hset(keys.sess(seeded.session_id), "epoch", "2")  # another node's hello.lua ran
    for seq in range(4, 7):  # the old connection does not know yet
        await first.send_frame(seq)
    with contextlib.suppress(TimeoutError):
        while (msg := await first.recv(wait_s=1.0))["t"] != "closed":
            assert not (msg["t"] == "ack" and msg["ack_seq"] > 3), "a fenced chunk is never acked"
    entries = await redis.xrange(keys.sess_chunks(seeded.session_id))
    assert [e[1]["seq"] for e in entries] == ["1", "2", "3"], "stale-epoch frames never reach the stream"
    assert [r.seq for r in await ledger_rows(app_engine, seeded)] == [1, 2, 3], "…nor the ledger"
    await redis.publish(keys.ctl(seeded.session_id), dumps({"t": "superseded", "epoch": 2}))
    while (await first.recv())["t"] != "closed":
        pass
    assert first.close_code == 4409


# --- protocol violations and gates ---------------------------------------------------------------------------


async def test_credit_violation_closes_4009(server, redis, seeded):
    rec = Recorder(server.url)
    await rec.connect()
    welcome = await rec.hello(await ingest_ticket(redis, seeded))
    for seq in range(1, welcome["credit"] + 22):  # tolerance is +20 beyond the advertised credit
        await rec.send_frame(seq)
    msgs = []
    while (msg := await rec.recv())["t"] != "closed":
        msgs.append(msg)
    assert any(m["t"] == "error" and m["code"] == 4009 for m in msgs)
    assert rec.close_code == 4009


async def test_consent_missing_closes_4011(server, redis, factories, app_engine, settings):
    seeded = await seed_session(factories, app_engine, settings, consent=False)
    rec = Recorder(server.url)
    await rec.connect()
    err = await rec.hello(await ingest_ticket(redis, seeded))
    assert err["t"] == "error" and err["code"] == 4011
    assert await rec.recv() == {"t": "closed", "code": 4011}


@pytest.mark.parametrize(
    ("kind", "code"),
    [("bogus", 4001), ("watch", 4001), ("missing-session", 4004)],
)
async def test_ticket_and_session_checks(server, redis, seeded, kind, code):
    from uuid import uuid4

    if kind == "bogus":
        ticket = "not-a-ticket"
    elif kind == "watch":
        ticket = await ingest_ticket(redis, seeded, kind="watch")
    else:
        ticket = await ingest_ticket(redis, seeded, session_id=uuid4())
    rec = Recorder(server.url)
    await rec.connect()
    err = await rec.hello(ticket)
    assert err["t"] == "error" and err["code"] == code
    assert await rec.recv() == {"t": "closed", "code": code}


async def test_ticket_is_single_use(server, redis, seeded):
    ticket = await ingest_ticket(redis, seeded)
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.hello(ticket))["t"] == "welcome"
    rec2 = Recorder(server.url)
    await rec2.connect()
    assert (await rec2.hello(ticket))["code"] == 4001
    await rec.close()


async def test_bad_hello_and_bad_frame(server, redis, seeded):
    rec = Recorder(server.url)
    await rec.connect()
    await rec.ws.send(dumps({"t": "hello", "ticket": "x", "unknown": 1}))
    assert (await rec.recv())["code"] == 4005 and (await rec.recv())["code"] == 4005
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    await rec.ws.send(b"\x00" * 5)
    assert (await rec.recv())["code"] == 4010 and (await rec.recv())["code"] == 4010


async def test_chunk_after_final_seq_closes_4012_and_end_zero_ends_immediately(
    server, redis, seeded, app_engine
):
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    await rec.ws.send(dumps({"t": "end", "final_seq": 0}))
    assert (await rec.recv()) == {"t": "bye", "reason": "ended", "ack_seq": 0}
    assert (await rec.recv()) == {"t": "closed", "code": 1000}
    rec2 = Recorder(server.url)
    await rec2.connect()
    err = await rec2.hello(await ingest_ticket(redis, seeded))
    assert err["code"] == 4012  # the session is ended: no further ingest


async def test_hello_timeout_closes_4001(server):
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.recv(wait_s=8))["code"] == 4001


# --- redis loss --------------------------------------------------------------------------------------------------


async def test_redis_flush_mid_session_fails_closed_then_rehydrates_on_reconnect(
    server, redis, seeded, app_engine, objects_dir
):
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.hello(await ingest_ticket(redis, seeded)))["epoch"] == 1
    for seq in range(1, 41):
        await rec.send_frame(seq)
    while rec.ack_seq < 40:
        assert (await rec.recv())["t"] != "closed"
    await redis.flushdb()  # this index only: the hot state, the stream and the consumer group are gone
    for seq in range(41, 46):
        await rec.send_frame(seq)
    msgs = []
    while (msg := await rec.recv())["t"] != "closed":
        msgs.append(msg)
    assert any(m["t"] == "error" and m["code"] == 4503 and m["retryable"] for m in msgs), msgs
    assert rec.close_code == 4503
    assert not any(m["t"] == "ack" and m["ack_seq"] > 40 for m in msgs), (
        "fail-closed: nothing acked after the flush"
    )

    rec2 = Recorder(server.url)
    await rec2.connect()
    welcome = await rec2.hello(await ingest_ticket(redis, seeded), resume=True, last_sent_seq=45)
    assert welcome["t"] == "welcome" and welcome["epoch"] == 2, (
        "epoch continues from sessions.epoch after rehydration"
    )
    assert welcome["ack_seq"] == 40 and welcome["missing"] == [[41, 45]]
    rec2.last_sent = 45
    assert (await rec2.stream(upto=60)) == {"t": "bye", "reason": "ended", "ack_seq": 60}
    await assert_ledger_complete(app_engine, seeded, objects_dir, 60)
    hot = await redis.hgetall(keys.sess(seeded.session_id))
    assert hot["epoch"] == "2" and hot["ack_seq"] == "60"
    groups = await redis.xinfo_groups(keys.sess_chunks(seeded.session_id))
    assert [g["name"] for g in groups] == [keys.STT_CONSUMER_GROUP]


# --- pause / resume_rec / last-chunk flag --------------------------------------------------------------------------


async def test_pause_and_resume_rec_change_session_state_only(server, redis, seeded, app_engine):
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    await rec.ws.send(dumps({"t": "pause"}))
    await asyncio.sleep(0.3)
    assert (await session_row(app_engine, seeded)).state == "paused"
    await rec.ws.send(dumps({"t": "resume_rec"}))
    await asyncio.sleep(0.3)
    assert (await session_row(app_engine, seeded)).state == "recording"
    await rec.ws.send(encode_frame(FrameHeader(seq=1, offset_ms=0, flags=FLAG_LAST_CHUNK), payload_for(1)))
    await rec.ws.send(dumps({"t": "end", "final_seq": 1}))
    while (msg := await rec.recv())["t"] not in ("bye", "closed"):
        pass
    assert msg == {"t": "bye", "reason": "ended", "ack_seq": 1}
    rows = await ledger_rows(app_engine, seeded)
    assert [(r.seq, r.flags) for r in rows] == [(1, FLAG_LAST_CHUNK)]
    _ = frame  # re-exported helper used by other modules


# --- control channel: consent revoked / purge mid-session ----------------------------------------------------


async def test_consent_revoked_mid_session_acks_then_closes_4011(server, redis, seeded, app_engine):
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    for seq in range(1, 9):
        await rec.send_frame(seq)
    while rec.ack_seq < 8:
        assert (await rec.recv())["t"] != "closed"
    await redis.publish(keys.ctl(seeded.session_id), dumps({"t": "consent_revoked"}))
    msgs = []
    while (msg := await rec.recv())["t"] != "closed":
        msgs.append(msg)
    assert {"t": "bye", "reason": "consent_revoked", "ack_seq": 8} in msgs and rec.close_code == 4011
    assert len(await ledger_rows(app_engine, seeded)) == 8


async def test_purge_control_message_closes_4012(server, redis, seeded):
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    await redis.publish(keys.ctl(seeded.session_id), dumps({"t": "purge"}))
    msgs = []
    while (msg := await rec.recv())["t"] != "closed":
        msgs.append(msg)
    assert any(m["t"] == "error" and m["code"] == 4012 for m in msgs) and rec.close_code == 4012


async def test_duplicate_of_a_buffered_chunk_keeps_its_payload(
    server, redis, seeded, app_engine, objects_dir
):
    """A recorder re-sending its unacked window (docs/protocol.md §4.1 rule 3) must not cost the chunks
    that are still parked in the reorder buffer.

    The shell caches every frame's bytes until the core emits ``Store`` for that seq. It used to drop
    the cache whenever the core counted a *duplicate* — but the core counts a duplicate for a seq it is
    still holding in ``_reorder``, so once the gap filled it emitted ``Store`` for a chunk whose bytes
    were gone: the chunk was silently never written, ``contig_seq`` had already moved past it so
    ``missing`` could never nack it again, ``ack_seq`` froze and the session never reached ``ended``.
    """
    rec = Recorder(server.url)
    await rec.connect()
    await rec.hello(await ingest_ticket(redis, seeded))
    for seq in (1, 2, 4, 5):  # 3 is "lost in the Wi-Fi"
        await rec.send_frame(seq)
    while not any(m["t"] == "nack" for m in rec.received):
        assert (await rec.recv())["t"] != "closed"
    for seq in (4, 5):  # the ring buffer re-sends the whole unacked window
        await rec.ws.send(frame(seq, rec.seed))
    await asyncio.sleep(0.2)
    await rec.ws.send(frame(3, rec.seed))
    bye = await rec.stream(upto=5)
    assert bye == {"t": "bye", "reason": "ended", "ack_seq": 5}
    await assert_ledger_complete(app_engine, seeded, objects_dir, 5)
    row = await session_row(app_engine, seeded)
    assert (row.state, row.ack_seq, row.final_seq) == ("ended", 5, 5)
