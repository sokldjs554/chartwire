"""stt-worker integration (§7.4) against real PostgreSQL + Redis (``chartwire_test_c`` / index 3).

Stream entries are produced directly with the contract fields (``seq,key,len,off,fl,ep,ts``) through
WP-B's ``SessionState`` — the same path the ingest shell uses — so these tests need no WebSocket server.
The ledger (``audio_chunks``) and the object store hold the encrypted chunks exactly as the ingest
would have written them; the ``t01`` fixture script (5 utterances, 8.3 s → 42 chunks of 200 ms) drives
the ``ScriptedSimulator`` without provider latency.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core import pii
from chartwire.core.clock import SystemClock
from chartwire.core.config import Settings
from chartwire.crypto import Envelope, KeyCache, LocalKek, dek_fingerprint
from chartwire.db.models import (
    AuditEvent,
    OutboxEvent,
    RiskEvent,
    SegmentSearch,
    TranscriptSegment,
)
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.base import chunk_key
from chartwire.objectstore.localfs import LocalFs
from chartwire.ops.drain import Drainer
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys
from chartwire.redis.session_state import SessionState
from chartwire.stt.consumer import chunk_aad, segment_aad
from chartwire.stt.scripts_io import load_script
from chartwire.stt.worker import SttWorker, SttWorkerConfig, build_adapter

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO / "tests" / "fixtures" / "scripts"
CHUNK_MS = 200
CHUNK_BYTES = 6_400
T01_CHUNKS = 42  # total_ms 8300 → offsets 0 … 8200
T01_FINALS = 5
T01_ALERT_SEQ = 3  # "요즘은 그냥 사라지고 싶어요" → suicidal_ideation, severity 2
ALL_SCOPES = ["recording", "transcription", "ai_drafting", "search_index"]


# --------------------------------------------------------------------------- seeding


@dataclass(frozen=True)
class Seeded:
    tenant_id: UUID
    clinician_id: UUID
    patient_id: UUID
    session_id: UUID
    kek_ref: str
    dek: bytes
    started_at: datetime


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


async def seed(
    factories: Any,
    app_engine: AsyncEngine,
    settings: Settings,
    *,
    scopes: list[str] | None = None,
    script_ref: str = "t01",
) -> Seeded:
    """Tenant + clinician + patient (+ consent) + recording session with a real wrapped DEK."""
    scopes = ALL_SCOPES if scopes is None else scopes
    tenant = await factories.tenant("clinic-c")
    clinician = await factories.user(tenant.id, "clinician")
    patient = await factories.patient(tenant.id)
    started_at = utcnow()
    session = await factories.session(tenant.id, patient.id, clinician.id, started_at=started_at)
    dek = Envelope.new_dek()
    wrapped = LocalKek(settings.kek_master_bytes).wrap(dek, tenant.kek_ref)
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        await sessions_repo.update_session(
            s,
            session.id,
            dek_wrapped=wrapped,
            dek_fingerprint=dek_fingerprint(wrapped),
            script_ref=script_ref,
            chunk_ms=CHUNK_MS,
        )
        if scopes:
            await patients_repo.grant_consent(
                s, tenant_id=tenant.id, patient_id=patient.id, scopes=scopes, granted_by=clinician.id
            )
    return Seeded(tenant.id, clinician.id, patient.id, session.id, tenant.kek_ref, dek, started_at)


def make_deps(app_engine: AsyncEngine, redis: Any, settings: Settings, objects_dir: Path) -> HandlerContext:
    kek = LocalKek(settings.kek_master_bytes)
    return HandlerContext(
        engine=app_engine,
        redis=redis,
        objectstore=LocalFs(objects_dir),
        clock=SystemClock(),
        settings=settings,
        kek=kek,
        keycache=KeyCache(kek),
    )


def payload_for(seq: int) -> bytes:
    return random.Random(seq).randbytes(CHUNK_BYTES)


class Producer:
    """Writes what the ingest shell writes: encrypted object, stream entry, ledger row."""

    def __init__(self, deps: HandlerContext, seeded: Seeded) -> None:
        self.deps, self.seeded = deps, seeded
        self.state = SessionState(deps.redis, stream_maxlen=2000)
        self.epoch = 0

    async def hello(self) -> int:
        data = await self.state.hello(self.seeded.session_id, "node-test", "conn-1", now=utcnow())
        self.epoch = int(data["epoch"])
        return self.epoch

    def fields(self, seq: int) -> dict[str, Any]:
        s = self.seeded
        return {
            "seq": seq,
            "key": chunk_key(s.tenant_id, s.session_id, seq),
            "len": CHUNK_BYTES,
            "off": (seq - 1) * CHUNK_MS,
            "fl": 0,
            "ep": self.epoch,
            "ts": int(utcnow().timestamp() * 1000),
        }

    async def store(self, seq: int) -> None:
        s = self.seeded
        blob = Envelope.encrypt(s.dek, payload_for(seq), chunk_aad(s.tenant_id, s.session_id, seq))
        await self.deps.objectstore.put(chunk_key(s.tenant_id, s.session_id, seq), blob)

    async def xadd(self, seq: int) -> None:
        assert await self.state.xadd_chunk(self.seeded.session_id, self.epoch, self.fields(seq), now=utcnow())

    async def ledger(self, seqs: Iterable[int]) -> None:
        s = self.seeded
        rows = [
            {
                "session_id": s.session_id,
                "seq": seq,
                "tenant_id": s.tenant_id,
                "byte_len": CHUNK_BYTES,
                "sha256": hashlib.sha256(payload_for(seq)).digest(),
                "storage_key": chunk_key(s.tenant_id, s.session_id, seq),
                "offset_ms": (seq - 1) * CHUNK_MS,
                "flags": 0,
                "received_at": utcnow(),
            }
            for seq in seqs
        ]
        async with tenant_tx(self.deps.engine, TenantCtx.service(s.tenant_id)) as db:
            await sessions_repo.insert_chunks(db, rows)

    async def produce(self, seqs: Iterable[int], *, stream: bool = True, ledger: bool = True) -> None:
        seqs = list(seqs)
        for seq in seqs:
            await self.store(seq)
            if stream:
                await self.xadd(seq)
        if ledger:
            await self.ledger(seqs)

    async def end(self, final_seq: int = T01_CHUNKS, *, marker: bool = True) -> None:
        """What the recorder shell does on ``end``: marker first, then ``state='ended'`` (§6.4 rule 6).

        ``marker=False`` is the state a Redis flush leaves behind when it lands between the two: the
        session is ``ended`` in PostgreSQL and the marker no longer exists anywhere."""
        s = self.seeded
        if marker:
            await self.state.xadd_end(s.session_id, self.epoch)
        async with tenant_tx(self.deps.engine, TenantCtx.service(s.tenant_id)) as db:
            await sessions_repo.set_state(db, s.session_id, "ended", now=utcnow())
            await sessions_repo.update_session(db, s.session_id, final_seq=final_seq)


# --------------------------------------------------------------------------- worker harness


async def wait_for(predicate: Callable[[], Awaitable[bool]], *, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def worker_config(**overrides: Any) -> SttWorkerConfig:
    base: dict[str, Any] = {
        "scripts_dir": SCRIPTS_DIR,
        "sim_latency": False,
        "discovery_s": 0.1,
        "block_ms": 200,
        "lease_ms": 3000,
        "http_port": 0,
    }
    base.update(overrides)
    return SttWorkerConfig(**base)


class RunningWorker:
    def __init__(self, deps: HandlerContext, cfg: SttWorkerConfig, worker_id: str) -> None:
        self.drainer = Drainer(deadline_s=10)
        self.worker = SttWorker(deps, build_adapter(cfg), cfg, worker_id=worker_id, drainer=self.drainer)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> SttWorker:
        self.task = asyncio.create_task(self.worker.run())
        return self.worker

    async def __aexit__(self, *exc: object) -> None:
        self.drainer.begin("test")
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=15)


async def run_until(
    deps: HandlerContext,
    predicate: Callable[[], Awaitable[bool]],
    *,
    what: str,
    timeout_s: float = 30.0,
    worker_id: str = "w1",
    **cfg_overrides: Any,
) -> SttWorker:
    async with RunningWorker(deps, worker_config(**cfg_overrides), worker_id) as worker:
        await wait_for(predicate, timeout_s=timeout_s, what=what)
    return worker


# --------------------------------------------------------------------------- queries


async def state_of(engine: AsyncEngine, seeded: Seeded) -> str:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        return (await s.scalars(select(SessionModel.state).where(SessionModel.id == seeded.session_id))).one()


def transcribed(engine: AsyncEngine, seeded: Seeded) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await state_of(engine, seeded) == "transcribed"

    return check


async def segments_of(engine: AsyncEngine, seeded: Seeded) -> list[segments_repo.SegmentRow]:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        return await segments_repo.replay(s, seeded.session_id, -1, 1000, started_at=seeded.started_at)


async def offsets_of(engine: AsyncEngine, seeded: Seeded) -> tuple[int, int]:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        row = await sessions_repo.get_stt_offset(s, seeded.session_id)
    return (int(row.last_chunk_seq), int(row.last_segment_seq)) if row is not None else (0, -1)


async def count(engine: AsyncEngine, seeded: Seeded, model: Any) -> int:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
        stmt = select(func.count()).select_from(model).where(model.session_id == seeded.session_id)
        return int((await s.execute(stmt)).scalar_one())


def decrypt(seeded: Seeded, row: segments_repo.SegmentRow) -> str:
    return Envelope.decrypt(
        seeded.dek, row.text_enc, segment_aad(seeded.tenant_id, seeded.session_id, row.seq)
    ).decode()


async def subscribe(redis: Any, *channels: str) -> Any:
    ps = redis.pubsub()
    await ps.subscribe(*channels)
    for _ in channels:  # consume the subscribe confirmations so nothing published afterwards is missed
        assert (await ps.get_message(timeout=2.0)) is not None
    return ps


async def messages(ps: Any, settle_s: float = 0.3) -> list[dict[str, Any]]:
    """Everything published so far: keep reading until ``settle_s`` passes without a message."""
    out: list[dict[str, Any]] = []
    last = time.monotonic()
    while time.monotonic() - last < settle_s:
        raw = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if raw is not None:
            out.append(orjson.loads(raw["data"]))
            last = time.monotonic()
    return out


# --------------------------------------------------------------------------- tests


async def test_search_index_is_redacted_while_the_ciphertext_keeps_the_original(
    factories, app_engine, redis, settings, tmp_path
):
    """spec §10.2 — `segment_search.text` 는 평문 색인이라 식별자를 가리고, `text_enc` 는 원문을 지킨다.

    t02 의 첫 발화에는 전화번호가 들어 있다. 색인에 `[전화]` 가 들어가고 암호문을 풀면 원문이
    그대로 나와야 한다: 임상의는 전사에서 환자가 말한 것을 보고, 테넌트 전체를 훑는 색인만 가려진다.
    """
    seeded = await seed(factories, app_engine, settings, script_ref="t02")
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    assert await producer.hello() == 1
    script = load_script(SCRIPTS_DIR / "t02.json")
    chunks = script.total_ms // CHUNK_MS
    await producer.produce(range(1, chunks + 1))
    await producer.end()
    await run_until(deps, transcribed(app_engine, seeded), what="session transcribed")

    rows = await segments_of(app_engine, seeded)
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        search_rows = (await s.scalars(select(SegmentSearch).order_by(SegmentSearch.segment_id))).all()

    raw = script.utterances[0].text
    assert "010-2345-6789" in raw, "픽스처가 전화번호를 잃었다면 이 시험은 아무것도 확인하지 않는다"
    assert decrypt(seeded, rows[0]) == raw, "암호문은 원문 그대로"
    assert search_rows[0].text == pii.redact(raw) and search_rows[0].text != raw
    assert "010-2345-6789" not in search_rows[0].text and "[전화]" in search_rows[0].text
    for row, utt in zip(search_rows, script.utterances, strict=True):
        assert row.text == pii.redact(utt.text)


async def test_finals_persisted_with_seq_created_at_alert_and_end_marker(
    factories, app_engine, redis, settings, tmp_path
):
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    assert await producer.hello() == 1
    ps = await subscribe(redis, keys.sess_events(seeded.session_id))
    await producer.produce(range(1, T01_CHUNKS + 1))
    await producer.end()

    worker = await run_until(deps, transcribed(app_engine, seeded), what="session transcribed")

    script = load_script(SCRIPTS_DIR / "t01.json")
    rows = await segments_of(app_engine, seeded)
    assert [r.seq for r in rows] == list(range(T01_FINALS))
    for row, utt in zip(rows, script.utterances, strict=True):
        assert row.created_at == seeded.started_at + timedelta(milliseconds=utt.t_start_ms)
        assert (row.speaker, row.t_start_ms, row.t_end_ms) == (utt.speaker, utt.t_start_ms, utt.t_end_ms)
        assert decrypt(seeded, row) == utt.text
        assert row.provider == "simulator" and row.text_len == len(utt.text)
    assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, T01_FINALS - 1)

    # inline risk transaction: row + SLA member + audit, all keyed to the segment
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        events = (await s.scalars(select(RiskEvent))).all()
        search_rows = (await s.scalars(select(SegmentSearch).order_by(SegmentSearch.segment_id))).all()
        outbox = (await s.scalars(select(OutboxEvent))).all()
        actions = set((await s.scalars(select(AuditEvent.action))).all())
        session_row = await sessions_repo.get_session(s, seeded.session_id)
    assert len(events) == 1
    event = events[0]
    assert (event.category, event.severity, event.segment_seq) == ("suicidal_ideation", 2, T01_ALERT_SEQ)
    assert event.segment_id == rows[T01_ALERT_SEQ].id and event.detector_version == "lex-1"
    assert event.sla_deadline_at == event.detected_at + timedelta(seconds=300)
    assert event.escalation_level == 0 and event.acknowledged_at is None
    score = await redis.zscore(keys.ALERTS_SLA, keys.alerts_sla_member(seeded.tenant_id, event.id))
    assert score is not None and abs(score - event.sla_deadline_at.timestamp() * 1000) < 1.0

    assert [r.segment_id for r in search_rows] == [r.id for r in rows]
    assert "불면" in search_rows[1].terms and "자살사고" in search_rows[T01_ALERT_SEQ].terms
    assert search_rows[1].text == script.utterances[1].text

    assert session_row is not None and session_row.state == "transcribed"
    assert session_row.transcribed_at is not None
    assert len(outbox) == 1 and outbox[0].event_type == "session.transcribed"
    assert outbox[0].payload == {"session_id": str(seeded.session_id), "patient_id": str(seeded.patient_id)}
    assert outbox[0].idempotency_key == f"session.transcribed:{seeded.session_id}:{session_row.epoch}"
    assert {"alert.created", "session.transcribed"} <= actions

    msgs = await messages(ps)
    finals = [m for m in msgs if m["t"] == "transcript.final"]
    assert [m["seq"] for m in finals] == list(range(T01_FINALS))
    assert all("committed_at" in m and m["segment_id"] for m in finals)
    assert finals[T01_ALERT_SEQ]["text"] == script.utterances[T01_ALERT_SEQ].text
    alerts = [m for m in msgs if m["t"] == "risk.alert"]
    assert len(alerts) == 1
    assert alerts[0]["risk_event_id"] == event.id and alerts[0]["segment_seq"] == T01_ALERT_SEQ
    assert alerts[0]["sla_deadline_at"] == event.sla_deadline_at.isoformat() and "committed_at" in alerts[0]
    assert alerts[0]["span"] == [event.span_start, event.span_end]
    assert any(m["t"] == "transcript.partial" for m in msgs)
    assert msgs[-1] == {"t": "session.state", "state": "transcribed"}

    assert not await redis.sismember(keys.STT_ACTIVE, str(seeded.session_id))
    assert await redis.get(keys.stt_owner(seeded.session_id)) is None
    assert await redis.hget(keys.STT_LAG, str(seeded.session_id)) is None
    assert worker.outcomes[seeded.session_id] == "transcribed"
    stats = worker.finished[seeded.session_id]
    assert (stats.finals, stats.alerts, stats.duplicates, stats.rebuilds) == (T01_FINALS, 1, 0, 0)


async def test_redelivered_entries_are_deduplicated_by_offsets(
    factories, app_engine, redis, settings, tmp_path
):
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    await producer.produce(range(1, 21))

    async def caught_up() -> bool:
        return (await offsets_of(app_engine, seeded))[0] >= 20

    first = await run_until(deps, caught_up, what="first worker at seq 20")
    assert first.outcomes[seeded.session_id] == "released"
    assert await redis.get(keys.stt_owner(seeded.session_id)) is None, "drain releases the lease"

    # at-least-once: the same 20 entries again (resume re-sends after a crash), then the rest
    for seq in range(1, 21):
        await producer.xadd(seq)
    await producer.produce(range(21, T01_CHUNKS + 1))
    await producer.end()
    second = await run_until(deps, transcribed(app_engine, seeded), what="transcribed", worker_id="w2")

    rows = await segments_of(app_engine, seeded)
    assert [r.seq for r in rows] == list(range(T01_FINALS))
    assert await count(app_engine, seeded, TranscriptSegment) == T01_FINALS
    assert await count(app_engine, seeded, RiskEvent) == 1
    assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, T01_FINALS - 1)
    stats = second.finished[seeded.session_id]
    assert stats.duplicates == 20 and stats.rebuilds == 0
    assert first.finished[seeded.session_id].finals + stats.finals == T01_FINALS


async def test_gap_is_rebuilt_from_ledger_and_waits_for_late_rows(
    factories, app_engine, redis, settings, tmp_path
):
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    gap = range(11, 31)
    late = range(21, 31)
    for seq in range(1, T01_CHUNKS + 1):
        await producer.store(seq)
        if seq not in gap:
            await producer.xadd(seq)  # stream lost 11..30 (trimmed / flushed)
    await producer.ledger(seq for seq in range(1, T01_CHUNKS + 1) if seq not in late)
    await producer.end()

    async def ledger_late_rows() -> None:
        await asyncio.sleep(0.8)  # longer than the 200 ms rebuild retry
        await producer.ledger(late)

    late_task = asyncio.create_task(ledger_late_rows())
    worker = await run_until(deps, transcribed(app_engine, seeded), what="transcribed after rebuild")
    await late_task

    rows = await segments_of(app_engine, seeded)
    assert [r.seq for r in rows] == list(range(T01_FINALS))
    assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, T01_FINALS - 1)
    stats = worker.finished[seeded.session_id]
    assert stats.rebuilds == 1 and stats.rebuilt_chunks == len(gap) and stats.integrity_failures == 0
    assert stats.entries == T01_CHUNKS - len(gap) + 1  # + end marker


async def test_flushdb_recreates_group_and_rebuilds_from_ledger(
    factories, app_engine, redis, settings, tmp_path
):
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    await producer.produce(range(1, 16))
    await producer.produce(range(16, T01_CHUNKS + 1), stream=False)  # ledgered, not yet streamed

    async def at_15() -> bool:
        return (await offsets_of(app_engine, seeded))[0] >= 15

    async def all_finals() -> bool:
        return (await offsets_of(app_engine, seeded)) == (T01_CHUNKS, T01_FINALS - 1)

    async with RunningWorker(deps, worker_config(), "w1") as worker:
        await wait_for(at_15, timeout_s=20, what="seq 15")
        await redis.flushdb()  # this index only: hash, stream, consumer group, lease, stt:active all gone
        await wait_for(all_finals, timeout_s=20, what="rebuild from the ledger after NOGROUP")
        groups = await redis.xinfo_groups(keys.sess_chunks(seeded.session_id))
        assert [g["name"] for g in groups] == [keys.STT_CONSUMER_GROUP]
        assert await redis.sismember(keys.STT_ACTIVE, str(seeded.session_id)), "re-added: the session is live"
        assert await redis.get(keys.stt_owner(seeded.session_id)) == "w1", "lease re-taken after the flush"
        await producer.end()  # the recorder finishes; the marker lands in the recreated stream
        await wait_for(transcribed(app_engine, seeded), timeout_s=20, what="transcribed")

    rows = await segments_of(app_engine, seeded)
    assert [r.seq for r in rows] == list(range(T01_FINALS))
    assert await count(app_engine, seeded, TranscriptSegment) == T01_FINALS
    stats = worker.finished[seeded.session_id]
    assert stats.group_recreated == 1 and stats.rebuilds == 1 and stats.rebuilt_chunks == T01_CHUNKS - 15
    assert worker.outcomes[seeded.session_id] == "transcribed"


@pytest.mark.parametrize(
    ("scopes", "expect_segments", "expect_search", "expect_alerts"),
    [
        (["recording"], 0, 0, 0),
        (["recording", "transcription"], T01_FINALS, 0, 1),
        (["recording", "transcription", "search_index"], T01_FINALS, T01_FINALS, 1),
    ],
    ids=["no-transcription", "no-search-index", "all"],
)
async def test_consent_gates(
    factories, app_engine, redis, settings, tmp_path, scopes, expect_segments, expect_search, expect_alerts
):
    seeded = await seed(factories, app_engine, settings, scopes=scopes)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    ps = await subscribe(redis, keys.sess_events(seeded.session_id))
    await producer.produce(range(1, T01_CHUNKS + 1))
    await producer.end()

    worker = await run_until(deps, transcribed(app_engine, seeded), what="transcribed")

    assert await count(app_engine, seeded, TranscriptSegment) == expect_segments
    assert await count(app_engine, seeded, SegmentSearch) == expect_search
    assert await count(app_engine, seeded, RiskEvent) == expect_alerts
    assert await offsets_of(app_engine, seeded) == (T01_CHUNKS, expect_segments - 1), "offsets always advance"
    stats = worker.finished[seeded.session_id]
    assert stats.skipped_no_consent == (T01_CHUNKS if expect_segments == 0 else 0)
    msgs = await messages(ps)
    assert sum(m["t"] == "transcript.final" for m in msgs) == expect_segments
    assert sum(m["t"] == "risk.alert" for m in msgs) == expect_alerts
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        outbox = (await s.scalars(select(OutboxEvent.event_type))).all()
    assert outbox == ["session.transcribed"], "the note handler applies the ai_drafting gate itself"


async def test_ownership_lease_is_exclusive_and_released(factories, app_engine, redis, settings, tmp_path):
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    cfg = worker_config(lease_ms=500)
    w1 = SttWorker(deps, build_adapter(cfg), cfg, worker_id="w1")
    w2 = SttWorker(deps, build_adapter(cfg), cfg, worker_id="w2")
    sid = seeded.session_id
    assert await w1.acquire(sid) and not await w2.acquire(sid)
    assert await w1.owns(sid) and not await w2.owns(sid)
    assert await w1.renew(sid) and not await w2.renew(sid)
    assert not await w2.release(sid) and await redis.get(keys.stt_owner(sid)) == "w1"
    await asyncio.sleep(0.6)  # w1 stalled: the lease expires
    assert await w2.acquire(sid) and not await w1.renew(sid), (
        "an expired lease is not renewed by its ex-owner"
    )
    assert await w2.release(sid) and await redis.get(keys.stt_owner(sid)) is None
    assert not await w1.owns(sid) and not await w1.renew(sid), (
        "released lease of an inactive session stays gone"
    )
    await redis.sadd(keys.STT_ACTIVE, str(sid))
    assert await w1.owns(sid), "a missing lease of an active session is re-taken on the spot (Redis flushed)"
    await redis.delete(keys.stt_owner(sid))
    assert await w1.renew(sid) and await redis.get(keys.stt_owner(sid)) == "w1", "renew re-takes it too"

    # discovery drops an active member that exists in no tenant, and skips sessions someone owns
    ghost = UUID(int=7)
    await redis.sadd(keys.STT_ACTIVE, str(ghost))
    started = await w2.run_once()
    assert started == [] and not await redis.sismember(keys.STT_ACTIVE, str(ghost))
    assert await redis.sismember(keys.STT_ACTIVE, str(sid)) and await w1.owns(sid)
    await w1.release(sid)


@pytest.fixture(autouse=True)
def _objects_dir(tmp_path: Path) -> Path:
    (tmp_path / "objects").mkdir(exist_ok=True)
    return tmp_path / "objects"


__all__ = [
    "ALL_SCOPES",
    "SCRIPTS_DIR",
    "T01_CHUNKS",
    "T01_FINALS",
    "Producer",
    "Seeded",
    "count",
    "make_deps",
    "offsets_of",
    "seed",
    "segments_of",
    "state_of",
    "wait_for",
]


async def test_discovery_survives_a_failing_pass(factories, app_engine, redis, settings, tmp_path):
    """One transient Redis/DB error in ``run_once`` must not kill discovery.

    It used to propagate out of the ``while drainer.accepting`` loop straight into ``shutdown()``,
    which released every lease — while the process stayed up and ``/readyz`` went back to 200 as soon
    as Redis recovered. No session was ever discovered again and nothing looked unhealthy. Same guard
    ``outbox.poller.Poller.run`` has ("a failed pass (DB blip) must not kill the worker").
    """
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    cfg = worker_config()
    async with RunningWorker(deps, cfg, "w1") as worker:
        real_run_once = worker.run_once
        calls = {"n": 0}

        async def flaky() -> list[UUID]:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("redis failover")
            return await real_run_once()

        worker.run_once = flaky  # type: ignore[method-assign]
        await redis.sadd(keys.STT_ACTIVE, str(seeded.session_id))
        await wait_for(
            lambda: _acquired(worker, seeded.session_id),
            timeout_s=15,
            what="discovery to recover after two failing passes",
        )
        assert calls["n"] > 2


async def _acquired(worker: SttWorker, sid: UUID) -> bool:
    return sid in worker.tasks


async def test_flush_after_the_end_marker_still_transcribes(factories, app_engine, redis, settings, tmp_path):
    """A Redis flush between the recorder's end marker and its delivery must not strand the session.

    The marker is XADDed *before* ``state='ended'`` commits, so a flush right after can take the marker
    with it. ``_recover_group`` recreates the group at ``$`` — the marker is gone for good, and the
    ledger rebuild does not replace it. Re-arming the stream would then pin the session forever:
    ``ended`` is not in ``TERMINAL_STATES``, ``_idle_should_exit`` only exits when the session is absent
    from ``stt:active`` (which the recovery itself re-populated), and ``session_reaper`` only scans
    ``recording``/``paused``. So the state stayed ``ended``, no ``session.transcribed`` was ever
    emitted, no SOAP draft was ever produced, and the lease plus a ``max_sessions`` slot leaked.
    """
    seeded = await seed(factories, app_engine, settings)
    deps = make_deps(app_engine, redis, settings, tmp_path / "objects")
    producer = Producer(deps, seeded)
    await producer.hello()
    await producer.produce(range(1, 16))

    async def at_15() -> bool:
        return (await offsets_of(app_engine, seeded))[0] >= 15

    async with RunningWorker(deps, worker_config(), "w1") as worker:
        await wait_for(at_15, timeout_s=20, what="seq 15")
        await producer.produce(range(16, T01_CHUNKS + 1), stream=False)  # ledgered, never streamed
        # the recorder finished, and the flush landed between the marker and its delivery: `ended`
        # is committed in PostgreSQL, and nothing in Redis remembers the marker any more
        await producer.end(marker=False)
        await redis.flushdb()
        await wait_for(transcribed(app_engine, seeded), timeout_s=25, what="transcribed without a marker")

    rows = await segments_of(app_engine, seeded)
    assert [r.seq for r in rows] == list(range(T01_FINALS)), "the ledger rebuild still produced every final"
    assert not await redis.sismember(keys.STT_ACTIVE, str(seeded.session_id)), "the slot was released"
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        assert list((await s.scalars(select(OutboxEvent.event_type))).all()) == ["session.transcribed"]
    assert worker.outcomes[seeded.session_id] == "transcribed"
