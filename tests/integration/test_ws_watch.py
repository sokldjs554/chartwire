"""Viewer e2e against a real uvicorn (port 8101), PostgreSQL and Redis (spec §6.7, §15 gate).

The stt-worker is not running here; the tests play its part exactly as §7.4 prescribes — commit the
segment/alert row first, then PUBLISH the §6.3 message on ``sess:{sid}:events``. What is checked on
the viewer side: replay and live interleave into exactly one strictly increasing ``transcript.final``
sequence without gaps or duplicates, alerts are delivered once, ``risk.ack`` round-trips through the
database and the SLA set, presence counts follow joins/leaves, and a stalled viewer cannot slow a
healthy one down (it is closed ``4013`` instead)."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import websockets
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.crypto import Envelope
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops import metrics
from chartwire.redis import keys
from chartwire.ws import watch as watch_mod
from chartwire.ws.watch import segment_aad
from tests.ws._live import (
    Recorder,
    Seeded,
    dumps,
    ingest_ticket,
    live_server,
    open_viewer,
    seed_session,
    viewer_recv,
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


# --- the stt-worker's side of the contract (§7.4): commit, then publish -----------------------------------


class Publisher:
    """Writes finals/alerts the way the stt-worker does and publishes the §6.3 messages."""

    def __init__(self, engine: AsyncEngine, redis: Any, seeded: Seeded, started_at: datetime) -> None:
        self.engine, self.redis, self.seeded, self.started_at = engine, redis, seeded, started_at

    def _final_values(self, seq: int, text: str) -> dict[str, Any]:
        s = self.seeded
        return {
            "tenant_id": s.tenant_id,
            "session_id": s.session_id,
            "patient_id": s.patient_id,
            "seq": seq,
            "speaker": "patient" if seq % 2 else "clinician",
            "t_start_ms": seq * 1000,
            "t_end_ms": seq * 1000 + 900,
            "text_enc": Envelope.encrypt(s.dek, text.encode(), segment_aad(s.tenant_id, s.session_id, seq)),
            "text_len": len(text),
            "confidence": 0.9,
            "provider": "sim",
            "created_at": self.started_at + timedelta(milliseconds=seq * 1000),
        }

    async def insert_finals(self, seqs: list[int], text_of=lambda seq: f"합성 문장 {seq}") -> dict[int, int]:
        """Commit ``transcript_segments`` rows; returns ``seq → segment_id``."""
        ids: dict[int, int] = {}
        async with tenant_tx(self.engine, TenantCtx.service(self.seeded.tenant_id)) as s:
            for seq in seqs:
                row = await segments_repo.insert_final(s, **self._final_values(seq, text_of(seq)))
                ids[seq] = row.id
        return ids

    async def publish_final(self, seq: int, segment_id: int, text: str | None = None) -> None:
        msg = {
            "t": "transcript.final",
            "seq": seq,
            "speaker": "patient" if seq % 2 else "clinician",
            "t_start_ms": seq * 1000,
            "t_end_ms": seq * 1000 + 900,
            "text": text if text is not None else f"합성 문장 {seq}",
            "confidence": 0.9,
            "segment_id": segment_id,
            "committed_at": datetime.now(tz=UTC).isoformat(),
        }
        await self.redis.publish(keys.sess_events(self.seeded.session_id), dumps(msg))

    async def publish(self, msg: dict[str, Any]) -> None:
        await self.redis.publish(keys.sess_events(self.seeded.session_id), dumps(msg))

    async def insert_alert(self, seq: int, segment_id: int, *, severity: int = 3) -> Any:
        now = datetime.now(tz=UTC)
        async with tenant_tx(self.engine, TenantCtx.service(self.seeded.tenant_id)) as s:
            event = await risk_repo.insert_event(
                s,
                tenant_id=self.seeded.tenant_id,
                session_id=self.seeded.session_id,
                patient_id=self.seeded.patient_id,
                segment_id=segment_id,
                segment_created_at=self.started_at + timedelta(milliseconds=seq * 1000),
                segment_seq=seq,
                category="suicidal_ideation",
                severity=severity,
                phrase="합성",
                span_start=0,
                span_end=2,
                scope={},
                detector_version="lex-1",
                detected_at=now,
                sla_deadline_at=now + timedelta(seconds=60),
            )
        await self.redis.zadd(
            keys.ALERTS_SLA,
            {keys.alerts_sla_member(self.seeded.tenant_id, event.id): (now.timestamp() + 60) * 1000},
        )
        return event

    def alert_msg(self, event: Any) -> dict[str, Any]:
        return {
            "t": "risk.alert",
            "risk_event_id": event.id,
            "category": event.category,
            "severity": event.severity,
            "segment_seq": event.segment_seq,
            "span": [event.span_start, event.span_end],
            "sla_deadline_at": event.sla_deadline_at.isoformat(),
            "committed_at": datetime.now(tz=UTC).isoformat(),
        }


@pytest.fixture
async def publisher(app_engine, redis, seeded) -> Publisher:
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        row = await sessions_repo.get_session(s, seeded.session_id)
    assert row is not None and row.started_at is not None
    return Publisher(app_engine, redis, seeded, row.started_at)


async def collect(ws: Any, *, until: Any, wait_s: float = 15.0) -> list[dict[str, Any]]:
    """Receive until ``until(messages)`` holds or the socket closes (the close pseudo-message is kept)."""
    out: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + wait_s
    while asyncio.get_running_loop().time() < deadline:
        msg = await viewer_recv(ws, wait_s=max(0.1, deadline - asyncio.get_running_loop().time()))
        out.append(msg)
        if msg["t"] == "closed" or until(out):
            break
    return out


def final_seqs(msgs: list[dict[str, Any]]) -> list[int]:
    return [m["seq"] for m in msgs if m["t"] == "transcript.final"]


# --- replay + live -----------------------------------------------------------------------------------------


async def test_replay_and_live_interleave_into_one_gapless_sequence(server, redis, seeded, publisher):
    ids = await publisher.insert_finals(list(range(0, 200)))
    ws = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    welcome = await viewer_recv(ws)
    assert welcome == {
        "t": "welcome",
        "session_id": str(seeded.session_id),
        "state": "recording",
        "last_final_seq": 199,
    }

    async def live() -> None:
        """While the replay runs: re-publish already-replayed finals (dups) and commit+publish new ones,
        including a burst that reaches the viewer before the replay caught up with it (gap fill)."""
        for seq in range(150, 200):  # duplicates of replayed rows
            await publisher.publish_final(seq, ids[seq])
        new = list(range(200, 300))
        more = await publisher.insert_finals(new)
        for seq in new:
            await publisher.publish_final(seq, more[seq])
            if seq % 20 == 0:
                await asyncio.sleep(0.02)
        for seq in (205, 260):  # late duplicates after the fact
            await publisher.publish_final(seq, more[seq])

    live_task = asyncio.ensure_future(live())
    msgs = await collect(ws, until=lambda ms: final_seqs(ms) and final_seqs(ms)[-1] == 299, wait_s=30)
    await live_task
    seqs = final_seqs(msgs)
    assert seqs == list(range(0, 300)), "finals must be strictly increasing without gaps or duplicates"
    replayed = [m for m in msgs if m["t"] == "transcript.final" and m["seq"] < 150]
    assert all(m["text"] == f"합성 문장 {m['seq']}" for m in replayed), (
        "replayed text decrypts under the session DEK"
    )
    assert all(m["segment_id"] == ids[m["seq"]] for m in replayed)
    assert not any(m["t"] in ("viewer.lagged", "viewer.degraded", "error") for m in msgs)
    with contextlib.suppress(Exception):
        await ws.close()


async def test_from_seq_replays_only_from_there_and_state_events_pass_through(
    server, redis, seeded, publisher
):
    await publisher.insert_finals(list(range(0, 30)))
    ws = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"), from_seq=20)
    assert (await viewer_recv(ws))["t"] == "welcome"
    msgs = await collect(ws, until=lambda ms: final_seqs(ms) and final_seqs(ms)[-1] == 29)
    assert final_seqs(msgs) == list(range(20, 30))
    await publisher.publish({"t": "session.state", "state": "ended"})
    await publisher.publish({"t": "transcript.partial", "from_seq": 30, "text": "합성 부분"})
    msgs = await collect(ws, until=lambda ms: any(m["t"] == "transcript.partial" for m in ms))
    assert [m["t"] for m in msgs if m["t"] != "viewer.presence"] == ["session.state", "transcript.partial"]
    with contextlib.suppress(Exception):
        await ws.close()


# --- alerts, risk.ack, presence -------------------------------------------------------------------------------


async def test_alert_dedup_risk_ack_roundtrip_and_presence(server, redis, seeded, publisher, app_engine):
    ids = await publisher.insert_finals([0, 1])
    event = await publisher.insert_alert(1, ids[1])
    a = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    assert (await viewer_recv(a))["t"] == "welcome"
    first = await collect(a, until=lambda ms: {"risk.alert", "viewer.presence"} <= {m["t"] for m in ms})
    alert = next(m for m in first if m["t"] == "risk.alert")
    assert (alert["risk_event_id"], alert["severity"], alert["segment_seq"], alert["span"]) == (
        event.id,
        3,
        1,
        [0, 2],
    ), "open alerts are replayed from the database"
    assert any(m == {"t": "viewer.presence", "count": 1} for m in first)

    b = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    assert (await viewer_recv(b))["t"] == "welcome"
    both = await collect(a, until=lambda ms: any(m == {"t": "viewer.presence", "count": 2} for m in ms))
    assert both[-1] == {"t": "viewer.presence", "count": 2}
    await collect(b, until=lambda ms: any(m["t"] == "risk.alert" for m in ms))

    await publisher.publish(publisher.alert_msg(event))  # live re-publish of the same alert: deduplicated
    await a.send(dumps({"t": "risk.ack", "risk_event_id": event.id}))
    acks_a = await collect(a, until=lambda ms: any(m["t"] == "risk.ack" for m in ms))
    acks_b = await collect(b, until=lambda ms: any(m["t"] == "risk.ack" for m in ms))
    expected = {"t": "risk.ack", "risk_event_id": event.id, "by": str(seeded.clinician_id)}
    assert expected in acks_a and expected in acks_b
    assert not any(m["t"] == "risk.alert" for m in acks_a + acks_b), "the duplicate alert was dropped"
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant_id)) as s:
        row = await risk_repo.get_event(s, event.id)
    assert row is not None and row.acknowledged_at is not None and row.acknowledged_by == seeded.clinician_id
    assert await redis.zscore(keys.ALERTS_SLA, keys.alerts_sla_member(seeded.tenant_id, event.id)) is None
    assert await redis.scard(keys.sess_viewers(seeded.session_id)) == 2

    await b.close()
    left = await collect(a, until=lambda ms: any(m == {"t": "viewer.presence", "count": 1} for m in ms))
    assert left[-1] == {"t": "viewer.presence", "count": 1}
    await a.close()
    await asyncio.sleep(0.2)
    assert await redis.scard(keys.sess_viewers(seeded.session_id)) == 0


async def test_watch_ticket_and_session_checks(server, redis, seeded):
    ws = await websockets.connect(f"{server.url}/ws/v1/watch")
    await ws.send(dumps({"t": "hello", "ticket": await ingest_ticket(redis, seeded, kind="ingest")}))
    assert (await viewer_recv(ws))["code"] == 4001 and (await viewer_recv(ws))["code"] == 4001
    ws = await websockets.connect(f"{server.url}/ws/v1/watch")
    await ws.send(dumps({"t": "hello", "ticket": "x", "from_seq": -3}))
    assert (await viewer_recv(ws))["code"] == 4005


async def test_recorder_events_reach_viewers_live(server, redis, seeded, publisher):
    """The ingest shell publishes ``session.state`` on the events channel; a viewer sees the session end."""
    ws = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    assert (await viewer_recv(ws))["t"] == "welcome"
    rec = Recorder(server.url)
    await rec.connect()
    assert (await rec.hello(await ingest_ticket(redis, seeded)))["t"] == "welcome"
    assert (await rec.stream(upto=5))["reason"] == "ended"
    msgs = await collect(ws, until=lambda ms: any(m == {"t": "session.state", "state": "ended"} for m in ms))
    assert msgs[-1] == {"t": "session.state", "state": "ended"}
    await ws.close()


# --- slow-viewer isolation ------------------------------------------------------------------------------------


async def open_stalled_viewer(url: str, ticket: str) -> Any:
    """A viewer whose TCP receive window is tiny and that never reads after ``welcome``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(sock, ("127.0.0.1", PORT))
    ws = await websockets.connect(f"{url}/ws/v1/watch", sock=sock, max_size=2**20)
    await ws.send(dumps({"t": "hello", "ticket": ticket}))
    assert (await viewer_recv(ws))["t"] == "welcome"
    return ws


async def test_stalled_viewer_is_closed_4013_while_a_healthy_viewer_gets_everything(
    server, redis, seeded, publisher, monkeypatch
):
    monkeypatch.setattr(watch_mod, "PARTIAL_Q_MAX", 8)
    monkeypatch.setattr(watch_mod, "CRITICAL_Q_MAX", 64)
    dropped_before = metrics.WS_DROPPED_PARTIALS_TOTAL._value.get()
    fast = await open_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    assert (await viewer_recv(fast))["t"] == "welcome"
    slow = await open_stalled_viewer(server.url, await ingest_ticket(redis, seeded, kind="watch"))
    total = 300

    def text() -> str:  # ~64 KB and incompressible: permessage-deflate must not hide the stall
        return secrets.token_urlsafe(49_152)

    async def publish_all() -> None:
        for seq in range(total):
            await publisher.publish({"t": "transcript.partial", "from_seq": seq, "text": text()})
            await publisher.publish_final(seq, seq + 1, text())
            await asyncio.sleep(0.003)

    pub = asyncio.ensure_future(publish_all())
    msgs = await collect(fast, until=lambda ms: final_seqs(ms) and final_seqs(ms)[-1] == total - 1, wait_s=60)
    await pub
    assert final_seqs(msgs) == list(range(total)), "the healthy viewer is not affected by the stalled one"
    assert not any(m["t"] in ("viewer.lagged", "closed") for m in msgs)

    tail = await collect(slow, until=lambda ms: False, wait_s=30)  # now drain what the server queued for it
    assert tail[-1] == {"t": "closed", "code": 4013}, "critical queue overflow closes the stalled viewer"
    assert final_seqs(tail) == sorted(set(final_seqs(tail))), "what it did get is still in order"
    assert len(final_seqs(tail)) < total, "…and it is a strict prefix, never the whole stream"
    # the lag notice sits in the critical queue behind the blocked sender; a viewer that dies with 4013 never
    # sees it (test_watch_queues fixes the notice semantics) — the drops are visible in the process metric
    assert metrics.WS_DROPPED_PARTIALS_TOTAL._value.get() > dropped_before
    await fast.close()
