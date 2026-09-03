"""Redis hot state, tickets and the ledger batcher against the real Redis/PostgreSQL (spec §5, §6.6)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import orjson
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.db.models import AudioChunk
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.redis import keys, tickets
from chartwire.redis.session_state import SessionState, StateLost
from chartwire.ws.ledger import ChunkRow, LedgerBatcher, LedgerError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


# --- tickets ---------------------------------------------------------------------------------------


async def test_ticket_is_consumed_exactly_once(redis):
    tid, uid, sid = uuid4(), uuid4(), uuid4()
    token = await tickets.issue(
        redis, tenant_id=tid, user_id=uid, role="clinician", session_id=sid, kind="ingest"
    )
    assert 0 < await redis.ttl(keys.ticket(token)) <= keys.TTL_TICKET
    payload = await tickets.consume(redis, token)
    assert payload is not None and (payload.tenant_id, payload.user_id, payload.session_id) == (tid, uid, sid)
    assert (payload.role, payload.kind, payload.sub) == ("clinician", "ingest", None)
    assert await tickets.consume(redis, token) is None  # GETDEL: second use fails
    assert await tickets.consume(redis, "never-issued") is None


async def test_ticket_keeps_non_uuid_dev_subject(redis):
    token = await tickets.issue(
        redis, tenant_id=uuid4(), user_id="dev:recorder", role="recorder", session_id=uuid4(), kind="ingest"
    )
    payload = await tickets.consume(redis, token)
    assert payload is not None and payload.user_id is None and payload.sub == "dev:recorder"


# --- session state / Lua ----------------------------------------------------------------------------


async def test_hello_increments_epoch_publishes_superseded_and_creates_group(redis):
    state = SessionState(redis, stream_maxlen=100)
    sid = uuid4()
    pubsub = redis.pubsub()
    await pubsub.subscribe(keys.ctl(sid))
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    first = await state.hello(sid, "node-a", "c1", now=NOW)
    second = await state.hello(sid, "node-b", "c2", now=NOW)
    assert (first["epoch"], second["epoch"]) == (1, 2)
    assert second["node"] == "node-b" and second["conn"] == "c2"
    msgs = []
    for _ in range(2):
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        msgs.append(orjson.loads(m["data"]))
    assert msgs == [{"t": "superseded", "epoch": 1}, {"t": "superseded", "epoch": 2}]
    groups = await redis.xinfo_groups(keys.sess_chunks(sid))
    assert [g["name"] for g in groups] == [keys.STT_CONSUMER_GROUP]
    assert await redis.sismember(keys.STT_ACTIVE, str(sid))
    await pubsub.aclose()


async def test_xadd_chunk_is_epoch_fenced_and_fails_closed_without_state(redis):
    state = SessionState(redis, stream_maxlen=100)
    sid = uuid4()
    fields = {"seq": 1, "key": "t/s/00000001.bin", "len": 6400, "off": 0, "fl": 0, "ep": 1, "ts": 1}
    with pytest.raises(StateLost):
        await state.xadd_chunk(sid, 1, fields, now=NOW)
    await state.hello(sid, "n", "c", now=NOW)
    assert await state.xadd_chunk(sid, 1, fields, now=NOW) is True
    assert await state.xadd_chunk(sid, 0, {**fields, "seq": 2}, now=NOW) is False  # stale epoch
    entries = await redis.xrange(keys.sess_chunks(sid))
    assert len(entries) == 1 and tuple(entries[0][1]) == keys.CHUNK_FIELDS
    await state.xadd_end(sid, 1)
    entries = await redis.xrange(keys.sess_chunks(sid))
    assert entries[-1][1] == {"end": "1", "ep": "1"}
    await state.expire_after_end(sid)
    assert 0 < await redis.ttl(keys.sess(sid)) <= keys.TTL_SESSION_AFTER_END


async def test_rehydrate_seeds_epoch_only_when_missing(redis):
    state = SessionState(redis)
    sid = uuid4()
    assert await state.rehydrate(sid, 40, "recording", NOW, epoch=7, now=NOW) is True
    data = await state.get(sid)
    assert data is not None and (data["epoch"], data["ack_seq"], data["ledger_seq"]) == ("7", "40", "40")
    assert await state.rehydrate(sid, 0, "recording", NOW, epoch=1, now=NOW) is False
    assert (await state.hello(sid, "n", "c", now=NOW))["epoch"] == 8
    assert await state.stt_lag(sid) == 0
    await redis.hset(keys.STT_LAG, str(sid), "12")
    assert await state.stt_lag(sid) == 12
    assert await state.viewer_join(sid, "v1") == 1
    assert await state.viewer_join(sid, "v2") == 2
    assert await state.viewer_leave(sid, "v1") == 1


# --- ledger batcher ----------------------------------------------------------------------------------


def _row(tenant_id, session_id, seq: int) -> ChunkRow:
    return ChunkRow(
        tenant_id=tenant_id,
        session_id=session_id,
        seq=seq,
        byte_len=6400,
        sha256=hashlib.sha256(seq.to_bytes(4, "little")).digest(),
        storage_key=f"{tenant_id}/{session_id}/{seq:08d}.bin",
        offset_ms=(seq - 1) * 200,
        flags=0,
        received_at=NOW,
    )


async def _chunk_seqs(engine: AsyncEngine, tenant_id, session_id) -> list[int]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        return await sessions_repo.ledgered_seqs(s, session_id, 0, 10_000)


async def test_batcher_groups_by_tenant_and_resolves_futures_after_commit(app_engine, session_a, session_b):
    batcher = LedgerBatcher(app_engine, flush_ms=20, flush_rows=500)
    batcher.start()
    futures = [batcher.submit(_row(session_a.tenant_id, session_a.id, s), ack_hint=s) for s in range(1, 11)]
    futures += [batcher.submit(_row(session_b.tenant_id, session_b.id, s), ack_hint=s) for s in range(1, 4)]
    assert batcher.pending_rows() == 13
    await asyncio.gather(*futures)
    assert batcher.pending_rows() == 0 and batcher.flushes >= 1 and batcher.rows_committed == 13
    assert await _chunk_seqs(app_engine, session_a.tenant_id, session_a.id) == list(range(1, 11))
    assert await _chunk_seqs(app_engine, session_b.tenant_id, session_b.id) == [1, 2, 3]
    async with tenant_tx(app_engine, TenantCtx.service(session_a.tenant_id)) as s:
        assert (await sessions_repo.get_session(s, session_a.id)).ack_seq == 10
    async with tenant_tx(app_engine, TenantCtx.service(session_b.tenant_id)) as s:
        assert (await sessions_repo.get_session(s, session_b.id)).ack_seq == 3
    # duplicates (resume re-sends) are ignored, ack never goes backwards
    await batcher.submit(_row(session_a.tenant_id, session_a.id, 5), ack_hint=5)
    async with tenant_tx(app_engine, TenantCtx.service(session_a.tenant_id)) as s:
        assert (await sessions_repo.get_session(s, session_a.id)).ack_seq == 10
        rows = (
            (await s.execute(select(AudioChunk.seq).where(AudioChunk.session_id == session_a.id)))
            .scalars()
            .all()
        )
        assert sorted(rows) == list(range(1, 11))
    await batcher.stop()
    with pytest.raises(LedgerError):
        await batcher.submit(_row(session_a.tenant_id, session_a.id, 11), ack_hint=11)


async def test_batcher_flushes_at_row_cap_before_the_window(app_engine, session_a):
    batcher = LedgerBatcher(app_engine, flush_ms=5_000, flush_rows=4)
    batcher.start()
    futures = [batcher.submit(_row(session_a.tenant_id, session_a.id, s), ack_hint=s) for s in range(1, 5)]
    await asyncio.wait_for(asyncio.gather(*futures), timeout=2)
    await batcher.stop()


async def test_failed_flush_raises_and_ack_only_moves_over_a_complete_prefix(app_engine, session_a):
    """A failed batch raises on every future; the persisted ack never skips a hole, whatever the hint says."""
    batcher = LedgerBatcher(app_engine, flush_ms=10, flush_rows=500)
    batcher.start()
    bad = _row(
        session_a.tenant_id, uuid4(), 1
    )  # FK violation: unknown session → the whole tenant batch fails
    with pytest.raises(LedgerError):
        await batcher.submit(bad, ack_hint=1)
    tid, sid = session_a.tenant_id, session_a.id
    await asyncio.gather(*(batcher.submit(_row(tid, sid, s), ack_hint=s) for s in (1, 2)))
    await batcher.submit(_row(tid, sid, 5), ack_hint=5)  # a stale hint from a lost connection: 3, 4 missing
    async with tenant_tx(app_engine, TenantCtx.service(tid)) as s:
        assert (await sessions_repo.get_session(s, sid)).ack_seq == 2, "ack does not jump over a hole"
    await asyncio.gather(*(batcher.submit(_row(tid, sid, s), ack_hint=5) for s in (3, 4)))
    async with tenant_tx(app_engine, TenantCtx.service(tid)) as s:
        assert (await sessions_repo.get_session(s, sid)).ack_seq == 5, "…and moves once the hole is filled"
    await batcher.stop()


async def test_sessions_ack_seq_is_visible_to_app_role(app_engine, session_a):
    async with tenant_tx(app_engine, TenantCtx.service(session_a.tenant_id)) as s:
        row = (
            await s.execute(select(SessionModel.ack_seq).where(SessionModel.id == session_a.id))
        ).scalar_one()
        assert row == 0
