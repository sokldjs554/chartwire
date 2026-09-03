"""Pure-Python tests for the load-test protocol clients (fake websocket, scripted server)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from chartwire.loadtest import client as lt
from chartwire.ws.codec import FLAG_LAST_CHUNK, FLAG_SIM, HEADER_LEN, decode_frame

TIMEOUT = 2.0
SETTLE_TURNS = 16
"""Event-loop turns a virtual sleep yields before advancing time (queue → wait_for → task chains)."""

# --------------------------------------------------------------------------- fakes


class VirtualClock:
    """Sleeps advance virtual time instantly; the event loop still gets a turn."""

    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def wall(self) -> float:
        return 1_700_000_000.0 + self.t

    async def sleep(self, seconds: float) -> None:
        """Let every runnable task (scripted server, receiver loop) settle, then jump the clock."""
        self.sleeps.append(seconds)
        for _ in range(SETTLE_TURNS):
            await asyncio.sleep(0)
        self.t += seconds


class FakeSocket:
    """Client-side transport; the test plays the server through ``push``/``server_close``/``frames``."""

    def __init__(self) -> None:
        self.to_server: asyncio.Queue[bytes | str] = asyncio.Queue()
        self.to_client: asyncio.Queue[object] = asyncio.Queue()
        self.aborted = False
        self.closed: int | None = None

    async def send(self, data: bytes | str) -> None:
        if self.aborted or self.closed is not None:
            raise lt.ConnectionClosed(None, "send on dead socket")
        await self.to_server.put(data)

    async def recv(self) -> bytes | str:
        item = await self.to_client.get()
        if isinstance(item, lt.ConnectionClosed):
            raise item
        assert isinstance(item, bytes | str)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = code

    def abort(self) -> None:
        self.aborted = True
        self.to_client.put_nowait(lt.ConnectionClosed(None, "aborted"))

    # -- server side helpers
    def push(self, msg: dict[str, Any]) -> None:
        self.to_client.put_nowait(lt.dumps(msg))

    def server_close(self, code: int) -> None:
        self.to_client.put_nowait(lt.ConnectionClosed(code, "server"))

    async def next_message(self) -> bytes | dict[str, Any]:
        data = await asyncio.wait_for(self.to_server.get(), TIMEOUT)
        return data if isinstance(data, bytes) else lt.loads(data)

    async def next_json(self) -> dict[str, Any]:
        msg = await self.next_message()
        assert isinstance(msg, dict), msg
        return msg

    async def next_frame(self) -> tuple[int, int, int]:
        msg = await self.next_message()
        assert isinstance(msg, bytes), msg
        h = decode_frame(msg)
        return h.seq, h.offset_ms, h.flags


class FakeNetwork:
    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.tickets: list[tuple[str, str]] = []
        self._new = asyncio.Event()

    async def connect(self, url: str) -> lt.WsTransport:
        sock = FakeSocket()
        self.sockets.append(sock)
        self._new.set()
        return sock

    async def ticket(self, session_id: str, kind: lt.Kind) -> str:
        self.tickets.append((session_id, kind))
        return f"tk-{len(self.tickets)}"

    async def wait_socket(self, index: int) -> FakeSocket:
        while len(self.sockets) <= index:
            self._new.clear()
            await asyncio.wait_for(self._new.wait(), TIMEOUT)
        return self.sockets[index]


def make_recorder(
    net: FakeNetwork, clock: VirtualClock, **overrides: Any
) -> tuple[lt.RecorderClient, lt.RecorderConfig]:
    cfg = lt.RecorderConfig(
        session_id="s-1",
        ingest_url="ws://test/ws/v1/ingest",
        total_chunks=overrides.pop("total_chunks", 5),
        **overrides,
    )
    return lt.RecorderClient(cfg, connect=net.connect, tickets=net.ticket, clock=clock), cfg


async def welcome(
    sock: FakeSocket, *, epoch: int = 1, ack_seq: int = 0, credit: int = 50, missing: list | None = None
) -> dict[str, Any]:
    hello = await sock.next_json()
    assert hello["t"] == "hello"
    sock.push(
        {
            "t": "welcome",
            "session_id": "s-1",
            "epoch": epoch,
            "ack_seq": ack_seq,
            "credit": credit,
            "heartbeat_ms": 15000,
            "missing": missing or [],
        }
    )
    return hello


async def ack_all(sock: FakeSocket, n: int, *, credit: int = 50, clock: VirtualClock | None = None) -> None:
    for expected in range(1, n + 1):
        seq, _offset, _flags = await sock.next_frame()
        assert seq == expected
        if clock is not None:
            clock.t += 0.05
        sock.push({"t": "ack", "ack_seq": seq, "credit": credit})


async def finish(sock: FakeSocket, final_seq: int) -> None:
    end = await sock.next_json()
    assert end == {"t": "end", "final_seq": final_seq}
    sock.push({"t": "bye", "reason": "ended", "ack_seq": final_seq})
    sock.server_close(1000)


async def run_with(
    client_run: Awaitable[lt.SessionStats], server: Callable[[], Awaitable[None]]
) -> lt.SessionStats:
    task = asyncio.ensure_future(client_run)
    await asyncio.wait_for(server(), TIMEOUT * 4)
    return await asyncio.wait_for(task, TIMEOUT)


# --------------------------------------------------------------------------- helpers


def test_chunk_payload_is_seeded_and_distinct_per_seq() -> None:
    a = lt.chunk_payload(7, 1, 6400)
    assert len(a) == 6400
    assert a == lt.chunk_payload(7, 1, 6400)
    assert a != lt.chunk_payload(7, 2, 6400)
    assert a != lt.chunk_payload(8, 1, 6400)
    assert len(lt.chunk_payload(1, 1, 100)) == 100


def test_build_frame_round_trips_through_codec() -> None:
    payload = lt.chunk_payload(0, 3, 64)
    frame = lt.build_frame(3, 400, payload, last=True, sim=True)
    h = decode_frame(frame)
    assert (h.seq, h.offset_ms, h.flags) == (3, 400, FLAG_LAST_CHUNK | FLAG_SIM)
    assert frame[HEADER_LEN:] == payload
    assert decode_frame(lt.build_frame(1, 0, b"x", sim=False)).flags == 0


def test_ring_buffer_keeps_last_n() -> None:
    ring = lt.RingBuffer(3)
    for seq in range(1, 6):
        ring.put(seq, bytes([seq]))
    assert len(ring) == 3
    assert 2 not in ring and 3 in ring and ring.get(5) == b"\x05"
    assert ring.get(1) is None


def test_percentile_nearest_rank() -> None:
    assert lt.percentile([], 95) is None
    assert lt.percentile([5.0], 99) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert lt.percentile(values, 50) == 50.0
    assert lt.percentile(values, 95) == 95.0
    assert lt.percentile(values, 100) == 100.0


def test_expand_ranges_and_parse_iso() -> None:
    assert lt.expand_ranges([[2, 4], (7, 7)]) == [2, 3, 4, 7]
    assert lt.parse_iso("2026-09-03T00:00:00Z") == 1_788_393_600.0
    assert lt.parse_iso("2026-09-03T09:00:00+09:00") == 1_788_393_600.0
    assert lt.parse_iso(None) is None and lt.parse_iso("nope") is None


# --------------------------------------------------------------------------- recorder


async def test_recorder_happy_path_hello_frames_end_bye() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _cfg = make_recorder(net, clock, total_chunks=5, chunk_bytes=64)

    async def server() -> None:
        sock = await net.wait_socket(0)
        hello = await welcome(sock)
        assert hello == {
            "t": "hello",
            "ticket": "tk-1",
            "proto": 1,
            "codec": "pcm16le",
            "sample_rate": 16000,
            "chunk_ms": 200,
            "resume": False,
        }
        for expected in range(1, 6):
            seq, offset, flags = await sock.next_frame()
            assert (seq, offset) == (expected, (expected - 1) * 200)
            assert bool(flags & FLAG_LAST_CHUNK) == (expected == 5)
            assert flags & FLAG_SIM
            clock.t += 0.05
            sock.push({"t": "ack", "ack_seq": seq, "credit": 50})
        await finish(sock, 5)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended"
    assert (stats.sent, stats.resent, stats.ack_seq, stats.final_seq, stats.loss) == (5, 0, 5, 5, 0)
    assert len(stats.ack_rtt_ms) == 5 and all(abs(v - 50.0) < 1e-6 for v in stats.ack_rtt_ms)
    assert sorted(stats.sent_at) == [1, 2, 3, 4, 5]
    assert stats.epochs == [1] and stats.credit_min == 50 and stats.reconnects == 0
    assert stats.summary()["ack_p95_ms"] == pytest.approx(50.0)
    assert net.tickets == [("s-1", "ingest")]


async def test_recorder_never_exceeds_credit() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=6, chunk_bytes=16)
    observed_outstanding: list[int] = []

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock, credit=2)
        acked = 0
        for expected in range(1, 7):
            seq, _o, _f = await sock.next_frame()
            assert seq == expected
            observed_outstanding.append(seq - acked)
            if seq % 2 == 0:
                await asyncio.sleep(0.02)  # nothing else must arrive while 2 are outstanding
                assert seq == 6 or sock.to_server.empty()  # (after the last chunk only ``end`` may follow)
                acked = seq
                sock.push({"t": "ack", "ack_seq": seq, "credit": 2})
        await finish(sock, 6)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended"
    assert max(observed_outstanding) <= 2
    assert stats.credit_waits >= 2
    assert stats.credit_min == 2


async def test_recorder_resumes_after_abort_and_resends_missing() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=8, chunk_bytes=16, reconnect_delay_s=0.0)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock, credit=50)
        await ack_all(sock, 3)
        # frames 4 and 5 are "in flight" when the network dies
        for expected in (4, 5):
            seq, _o, _f = await sock.next_frame()
            assert seq == expected
        rec.abort_connection()
        sock2 = await net.wait_socket(1)
        hello = await welcome(sock2, epoch=2, ack_seq=3, credit=50, missing=[[4, 5]])
        assert hello["resume"] is True and hello["last_sent_seq"] == 5
        for expected in (4, 5):  # re-sent from the ring buffer, in order
            seq, _o, _f = await sock2.next_frame()
            assert seq == expected
            sock2.push({"t": "ack", "ack_seq": seq, "credit": 50})
        await ack_all(sock2, 8)  # continues 6..8
        await finish(sock2, 8)

    async def ack_all(sock: FakeSocket, upto: int) -> None:
        while True:
            seq, _o, _f = await sock.next_frame()
            sock.push({"t": "ack", "ack_seq": seq, "credit": 50})
            if seq == upto:
                return

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended"
    assert (stats.sent, stats.resent) == (8, 2)
    assert stats.reconnects == 1 and stats.resumes_ok == 1 and stats.close_codes[None] == 1
    assert stats.missing_ranges == [(4, 5)] and stats.epochs == [1, 2]
    assert stats.loss == 0 and stats.ack_seq == 8
    assert net.tickets == [("s-1", "ingest"), ("s-1", "ingest")]


async def test_recorder_gap_beyond_ring_buffer_is_unrecoverable() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=10, chunk_bytes=16, ring_size=3, reconnect_delay_s=0.0)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock, credit=50)
        for _ in range(6):
            await sock.next_frame()
        rec.abort_connection()
        sock2 = await net.wait_socket(1)
        await welcome(sock2, epoch=2, ack_seq=0, credit=50, missing=[[1, 6]])

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "gap_unrecoverable"
    assert stats.resent == 0 and stats.reconnects == 1


async def test_recorder_honours_server_pause_notice() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=3, chunk_bytes=16)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        seq, _o, _f = await sock.next_frame()
        sock.push({"t": "pause", "reason": "stt_lag", "retry_ms": 3000})
        sock.push({"t": "ack", "ack_seq": seq, "credit": 0})
        await asyncio.sleep(0.02)
        sock.push({"t": "credit", "credit": 50})
        for expected in (2, 3):
            seq, _o, _f = await sock.next_frame()
            assert seq == expected
            sock.push({"t": "ack", "ack_seq": seq, "credit": 50})
        await finish(sock, 3)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended" and stats.pauses == 1
    assert stats.sent_at[2] - stats.sent_at[1] >= 3.0
    assert stats.credit_min == 0


async def test_recorder_answers_ping_and_resends_on_nack() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=3, chunk_bytes=16)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        seq, _o, _f = await sock.next_frame()
        assert seq == 1
        sock.push({"t": "ping", "ts": 42})
        sock.push({"t": "ack", "ack_seq": 1, "credit": 50})
        seen: list[Any] = []
        while (
            len([m for m in seen if isinstance(m, dict)]) < 1
            or len([m for m in seen if isinstance(m, bytes)]) < 2
        ):
            seen.append(await sock.next_message())
        assert {"t": "pong", "ts": 42} in seen
        sock.push({"t": "nack", "missing": [[2, 2]]})
        seq, _o, _f = await sock.next_frame()  # re-send of 2 (3 was already sent)
        assert seq == 2
        sock.push({"t": "ack", "ack_seq": 3, "credit": 50})
        await finish(sock, 3)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended"
    assert stats.nacks == 1 and stats.resent == 1 and stats.sent == 3


async def test_recorder_stops_on_superseded_and_consent_revoked() -> None:
    for reason in ("superseded", "consent_revoked"):
        net, clock = FakeNetwork(), VirtualClock()
        rec, _ = make_recorder(net, clock, total_chunks=50, chunk_bytes=16)

        async def server(net: FakeNetwork = net, reason: str = reason) -> None:
            sock = await net.wait_socket(0)
            await welcome(sock)
            await sock.next_frame()
            sock.push({"t": "bye", "reason": reason, "ack_seq": 1})
            sock.server_close(4409 if reason == "superseded" else 4011)

        stats = await run_with(rec.run(), server)
        assert stats.outcome == reason
        assert stats.reconnects == 0 and len(net.sockets) == 1
        assert stats.superseded == (1 if reason == "superseded" else 0)


async def test_recorder_error_4008_and_non_resumable_close() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=50, chunk_bytes=16)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        await sock.next_frame()
        sock.push({"t": "error", "code": 4008, "message": "seq_gap_unrecoverable", "retryable": False})
        sock.server_close(4008)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "gap_unrecoverable"
    assert stats.errors == [(4008, "seq_gap_unrecoverable")]
    assert stats.close_codes[4008] == 1 and stats.reconnects == 0

    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=50, chunk_bytes=16)

    async def server_credit_violation() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        await sock.next_frame()
        sock.server_close(4009)

    stats = await run_with(rec.run(), server_credit_violation)
    assert stats.outcome == "closed:4009" and stats.reconnects == 0


async def test_recorder_drain_bye_then_1012_reconnects_with_resume() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=4, chunk_bytes=16, reconnect_delay_s=0.0)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        await ack_all(sock, 2)
        sock.push({"t": "bye", "reason": "drain", "ack_seq": 2})
        sock.server_close(1012)
        sock2 = await net.wait_socket(1)
        hello = await welcome(sock2, epoch=2, ack_seq=2)
        assert hello["resume"] is True and hello["last_sent_seq"] >= 2
        while True:
            seq, _o, _f = await sock2.next_frame()
            sock2.push({"t": "ack", "ack_seq": seq, "credit": 50})
            if seq == 4:
                break
        await finish(sock2, 4)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended" and stats.close_codes[1012] == 1
    assert stats.reconnects == 1 and stats.loss == 0


async def test_recorder_user_pause_resume_and_delay_hook() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(net, clock, total_chunks=3, chunk_bytes=16)
    rec.pause()

    async def server() -> None:
        sock = await net.wait_socket(0)
        await welcome(sock)
        await asyncio.sleep(0.02)
        assert sock.to_server.empty()  # paused before the first chunk
        rec.delay(7.5)
        rec.resume()
        await ack_all(sock, 3)
        await finish(sock, 3)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "ended"
    assert 7.5 in clock.sleeps


async def test_recorder_stop_and_reconnect_limit() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    rec, _ = make_recorder(
        net, clock, total_chunks=100, chunk_bytes=16, reconnect_delay_s=0.0, max_reconnects=2
    )

    async def server() -> None:
        for i in range(3):
            sock = await net.wait_socket(i)
            await welcome(sock, epoch=i + 1)
            await sock.next_frame()
            sock.server_close(4503)

    stats = await run_with(rec.run(), server)
    assert stats.outcome == "reconnect_limit"
    assert stats.reconnects == 3 and stats.close_codes[4503] == 3


# --------------------------------------------------------------------------- viewer


def make_viewer(
    net: FakeNetwork, clock: VirtualClock, stats: lt.SessionStats, **overrides: Any
) -> lt.ViewerClient:
    cfg = lt.ViewerConfig(session_id="s-1", watch_url="ws://test/ws/v1/watch", **overrides)
    return lt.ViewerClient(cfg, connect=net.connect, tickets=net.ticket, clock=clock, stats=stats)


async def test_viewer_dedups_finals_and_measures_latencies() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    stats = lt.SessionStats("s-1")
    stats.sent_at = {3: 99.0, 6: 99.5}
    viewer = make_viewer(net, clock, stats, auto_ack_alerts=True)

    def final(seq: int) -> dict[str, Any]:
        return {
            "t": "transcript.final",
            "seq": seq,
            "speaker": "patient",
            "t_start_ms": 0,
            "t_end_ms": 200,
            "text": "x",
            "confidence": 0.9,
            "segment_id": seq,
        }

    async def server() -> None:
        sock = await net.wait_socket(0)
        hello = await sock.next_json()
        assert hello == {"t": "hello", "ticket": "tk-1"}
        sock.push({"t": "welcome", "session_id": "s-1", "state": "recording", "last_final_seq": 0})
        sock.push({"t": "transcript.partial", "from_seq": 1, "text": "부분"})
        sock.push(final(3))
        sock.push(final(3))
        sock.push(final(2))
        sock.push(final(6))
        committed = 1_700_000_000.0 + clock.t - 0.25
        sock.push(
            {
                "t": "risk.alert",
                "risk_event_id": "0190a3b2-0000-7000-8000-000000000001",
                "category": "suicidal_ideation",
                "severity": 3,
                "segment_seq": 6,
                "span": [0, 4],
                "sla_deadline_at": None,
                "committed_at": datetime.fromtimestamp(committed, tz=UTC).isoformat(),
            }
        )
        ack = await sock.next_json()
        assert ack == {"t": "risk.ack", "risk_event_id": "0190a3b2-0000-7000-8000-000000000001"}
        sock.push({"t": "risk.ack", "risk_event_id": ack["risk_event_id"], "by": "u"})
        sock.push({"t": "risk.escalated", "risk_event_id": ack["risk_event_id"]})
        sock.push({"t": "viewer.lagged", "dropped_partials": 4})
        sock.push({"t": "ping", "ts": 1})
        assert await sock.next_json() == {"t": "pong", "ts": 1}
        sock.push({"t": "session.state", "state": "ended"})
        sock.push(
            {
                "t": "note.status",
                "note_id": "n",
                "status": "needs_review",
                "coverage": 0.9,
                "unsupported_count": 1,
            }
        )
        sock.push({"t": "bye", "reason": "ended"})

    out = await run_with(viewer.run(), server)
    assert (out.partials, out.finals, out.final_dups, out.final_out_of_order) == (1, 2, 1, 1)
    assert out.last_final_seq == 6
    assert [round(v, 3) for v in out.final_e2e_ms] == [1000.0, 500.0]
    assert out.alerts == 1 and out.alerts_acked == 1 and out.escalations == 1
    assert out.alert_e2e_ms == [pytest.approx(250.0)]
    assert (
        out.lagged_dropped == 4 and out.states == ["recording", "ended"] and out.note_status == "needs_review"
    )
    assert out.summary()["final_e2e_p95_ms"] == 1000.0


async def test_viewer_reconnects_with_from_seq_after_drop() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    stats = lt.SessionStats("s-1")
    viewer = make_viewer(net, clock, stats, reconnect_delay_s=0.0)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await sock.next_json()
        sock.push({"t": "welcome", "session_id": "s-1", "state": "recording", "last_final_seq": 0})
        sock.push(
            {
                "t": "transcript.final",
                "seq": 5,
                "speaker": "clinician",
                "t_start_ms": 0,
                "t_end_ms": 1,
                "text": "",
                "confidence": 1.0,
                "segment_id": 1,
            }
        )
        await asyncio.sleep(0.02)
        sock.push({"t": "bye", "reason": "drain"})
        sock.server_close(1012)
        sock2 = await net.wait_socket(1)
        hello = await sock2.next_json()
        assert hello == {"t": "hello", "ticket": "tk-2", "from_seq": 5}
        sock2.push({"t": "welcome", "session_id": "s-1", "state": "recording", "last_final_seq": 5})
        sock2.push(
            {
                "t": "transcript.final",
                "seq": 5,
                "speaker": "clinician",
                "t_start_ms": 0,
                "t_end_ms": 1,
                "text": "",
                "confidence": 1.0,
                "segment_id": 1,
            }
        )
        sock2.push({"t": "bye", "reason": "ended"})

    out = await run_with(viewer.run(), server)
    assert out.viewer_reconnects == 1 and out.close_codes[1012] == 1
    assert out.finals == 1 and out.final_dups == 1


async def test_slow_viewer_sleeps_per_message_and_stop_aborts() -> None:
    net, clock = FakeNetwork(), VirtualClock()
    stats = lt.SessionStats("s-1")
    viewer = make_viewer(net, clock, stats, per_message_delay_s=0.2)

    async def server() -> None:
        sock = await net.wait_socket(0)
        await sock.next_json()
        sock.push({"t": "welcome", "session_id": "s-1", "state": "recording", "last_final_seq": 0})
        for i in range(3):
            sock.push({"t": "transcript.partial", "from_seq": i, "text": ""})
        await asyncio.sleep(0.02)
        viewer.stop()

    out = await run_with(viewer.run(), server)
    assert out.partials == 3
    assert clock.sleeps.count(0.2) == 3
    assert out.close_codes[None] == 1
