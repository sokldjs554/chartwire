"""asyncio recorder / viewer clients for the load tests (spec §6 semantics, §11.2 definitions).

The console's JavaScript recorder and this module implement the same client rules:

* frames are ``codec.encode_frame`` bytes (12-byte header + seeded pseudo-random PCM payload);
* ``hello`` is the first text frame, ``resume=True`` + ``last_sent_seq`` after a reconnect;
* credit compliance — ``last_sent_seq - ack_seq <= credit`` at send time (§6.4 rule 3);
* a 150-chunk ring buffer feeds re-sends for ``welcome.missing`` and ``nack.missing`` (rule 4);
* server ``pause{retry_ms}`` holds the sender, ``ping`` is answered with ``pong``;
* ``end{final_seq}`` is sent after the last chunk and the client waits for ``bye{ended}`` (rule 6).

I/O is abstracted behind :class:`WsTransport` / :class:`Connector` / :class:`TicketSource` so the
unit tests drive a fake socket with a scripted server; :class:`WebsocketsTransport` and
:class:`HttpTicketSource` are the real implementations used by the scenario runner.

Measurements (§11.2): ``ack_rtt`` = last send of a frame → cumulative ack covering it (client
clock); ``final_e2e`` = first send of chunk ``seq`` → viewer ``transcript.final`` for that seq;
``alert_e2e`` = server ``committed_at`` → viewer ``risk.alert`` receipt (wall clock, same host).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import orjson

from chartwire.ws.codec import FLAG_LAST_CHUNK, FLAG_SIM, FrameHeader, encode_frame

Kind = Literal["ingest", "watch"]

RESUMABLE_CLOSE_CODES: frozenset[int | None] = frozenset({None, 1006, 1012, 4000, 4503})
"""Close codes after which the recorder reconnects with ``resume`` (drop, drain, heartbeat, dependency)."""

# --------------------------------------------------------------------------- transport protocols


class ConnectionClosed(Exception):
    """The socket is gone; ``code`` is the close code or ``None`` for a hard drop (no close frame)."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(f"closed code={code} reason={reason!r}")
        self.code = code
        self.reason = reason


class WsTransport(Protocol):
    async def send(self, data: bytes | str) -> None: ...

    async def recv(self) -> bytes | str: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...

    def abort(self) -> None:
        """Drop the TCP connection without a close frame (chaos hook)."""
        ...


Connector = Callable[[str], Awaitable[WsTransport]]
TicketSource = Callable[[str, Kind], Awaitable[str]]


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- stats


def percentile(samples: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (``p`` in 0..100); ``None`` when there are no samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), round(p / 100 * len(ordered))))
    return ordered[rank - 1]


@dataclass
class SessionStats:
    """Per-session bookkeeping shared by the recorder and the viewer of one session."""

    session_id: str
    # recorder
    sent: int = 0
    resent: int = 0
    ack_seq: int = 0
    final_seq: int = 0
    nacks: int = 0
    credit_min: int | None = None
    credit_zero_at_s: float | None = None
    """Seconds after the recorder started when credit first reached 0 (scenario B)."""
    credit_waits: int = 0
    pauses: int = 0
    reconnects: int = 0
    resumes_ok: int = 0
    superseded: int = 0
    epochs: list[int] = field(default_factory=list)
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    close_codes: Counter[int | None] = field(default_factory=Counter)
    outcome: str = "running"
    ack_rtt_ms: list[float] = field(default_factory=list)
    sent_at: dict[int, float] = field(default_factory=dict)
    # viewer
    partials: int = 0
    finals: int = 0
    final_dups: int = 0
    final_out_of_order: int = 0
    last_final_seq: int = -1
    """Highest final seq seen; segments are 0-based (stt-worker contract), so ``-1`` = none yet."""
    alerts: int = 0
    alerts_acked: int = 0
    escalations: int = 0
    lagged_dropped: int = 0
    degraded: int = 0
    viewer_reconnects: int = 0
    final_e2e_ms: list[float] = field(default_factory=list)
    alert_e2e_ms: list[float] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    note_status: str | None = None

    @property
    def loss(self) -> int:
        """Seqs sent that the server never acknowledged (must be 0 after ``bye{ended}``)."""
        return max(0, self.final_seq - self.ack_seq) if self.final_seq else max(0, self.sent - self.ack_seq)

    def observe_credit(self, credit: int, *, elapsed_s: float | None = None) -> None:
        self.credit_min = credit if self.credit_min is None else min(self.credit_min, credit)
        if credit <= 0 and self.credit_zero_at_s is None and elapsed_s is not None:
            self.credit_zero_at_s = elapsed_s

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "outcome": self.outcome,
            "sent": self.sent,
            "resent": self.resent,
            "ack_seq": self.ack_seq,
            "final_seq": self.final_seq,
            "loss": self.loss,
            "nacks": self.nacks,
            "credit_min": self.credit_min,
            "credit_zero_at_s": self.credit_zero_at_s,
            "credit_waits": self.credit_waits,
            "pauses": self.pauses,
            "reconnects": self.reconnects,
            "resumes_ok": self.resumes_ok,
            "superseded": self.superseded,
            "close_codes": {
                str(k): v for k, v in sorted(self.close_codes.items(), key=lambda kv: str(kv[0]))
            },
            "ack_p50_ms": percentile(self.ack_rtt_ms, 50),
            "ack_p95_ms": percentile(self.ack_rtt_ms, 95),
            "ack_p99_ms": percentile(self.ack_rtt_ms, 99),
            "finals": self.finals,
            "final_dups": self.final_dups,
            "final_out_of_order": self.final_out_of_order,
            "partials": self.partials,
            "alerts": self.alerts,
            "escalations": self.escalations,
            "lagged_dropped": self.lagged_dropped,
            "final_e2e_p95_ms": percentile(self.final_e2e_ms, 95),
            "alert_e2e_p95_ms": percentile(self.alert_e2e_ms, 95),
            "note_status": self.note_status,
        }


# --------------------------------------------------------------------------- framing helpers


def chunk_payload(seed: int, seq: int, size: int) -> bytes:
    """Seeded pseudo-random payload (sha256 differs per chunk, §6.2); O(size) and allocation-light."""
    block = hashlib.sha256(f"{seed}:{seq}".encode()).digest()
    reps, rem = divmod(size, len(block))
    return block * reps + block[:rem]


def build_frame(seq: int, offset_ms: int, payload: bytes, *, last: bool = False, sim: bool = True) -> bytes:
    flags = (FLAG_LAST_CHUNK if last else 0) | (FLAG_SIM if sim else 0)
    return encode_frame(FrameHeader(seq=seq, offset_ms=offset_ms, flags=flags), payload)


def dumps(msg: dict[str, Any]) -> str:
    return orjson.dumps(msg).decode()


def loads(data: bytes | str) -> dict[str, Any]:
    msg = orjson.loads(data)
    if not isinstance(msg, dict) or "t" not in msg:
        raise ValueError("not a protocol message")
    return msg


def expand_ranges(ranges: Iterable[Sequence[int]]) -> list[int]:
    out: list[int] = []
    for rng in ranges:
        lo, hi = int(rng[0]), int(rng[1])
        out.extend(range(lo, hi + 1))
    return out


class RingBuffer:
    """Last ``size`` frames by seq (150 chunks = 30 s at 200 ms, §6.4 rule 4)."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._frames: OrderedDict[int, bytes] = OrderedDict()

    def put(self, seq: int, frame: bytes) -> None:
        self._frames[seq] = frame
        while len(self._frames) > self.size:
            self._frames.popitem(last=False)

    def get(self, seq: int) -> bytes | None:
        return self._frames.get(seq)

    def __contains__(self, seq: int) -> bool:
        return seq in self._frames

    def __len__(self) -> int:
        return len(self._frames)


# --------------------------------------------------------------------------- recorder


@dataclass(frozen=True)
class RecorderConfig:
    session_id: str
    ingest_url: str
    total_chunks: int
    chunk_ms: int = 200
    chunk_bytes: int = 6_400
    speed: float = 1.0
    ring_size: int = 150
    sim: bool = True
    seed: int = 0
    hello_timeout_s: float = 5.0
    end_wait_s: float = 10.0
    reconnect_delay_s: float = 0.2
    max_reconnects: int = 50
    close_grace_s: float = 2.0
    """How long to wait for the server's close frame after a server-initiated stop (bye/error)."""


class RecorderClient:
    """One recorder session: paces chunks, obeys credit, resumes after drops, ends cleanly."""

    def __init__(
        self,
        cfg: RecorderConfig,
        *,
        connect: Connector,
        tickets: TicketSource,
        clock: Clock | None = None,
        stats: SessionStats | None = None,
    ) -> None:
        self.cfg = cfg
        self.stats = stats or SessionStats(cfg.session_id)
        self._connect = connect
        self._tickets = tickets
        self._clock = clock or SystemClock()
        self._ring = RingBuffer(cfg.ring_size)
        self._resend: deque[int] = deque()
        self._pending_ack: dict[int, float] = {}
        self._next_seq = 1
        self._last_sent_seq = 0
        self._credit = 0
        self._epoch = 0
        self._ever_connected = False
        self._pause_until = 0.0
        self._user_paused = False
        self._done = False
        self._stop_reason: str | None = None
        self._server_stopped = False
        self._ws: WsTransport | None = None
        self._wake = asyncio.Event()
        self._finished = asyncio.Event()
        self._extra_delay_s = 0.0
        self._background: set[asyncio.Future[None]] = set()
        self._started_at: float | None = None

    # ---- chaos / control hooks -------------------------------------------------------------
    def abort_connection(self) -> None:
        """Kill the socket without a close frame (scenario D, console '네트워크 끊기')."""
        if self._ws is not None:
            self._ws.abort()

    def delay(self, seconds: float) -> None:
        """Inject an extra pause before the next send."""
        self._extra_delay_s += seconds

    def pause(self) -> None:
        self._user_paused = True

    def resume(self) -> None:
        self._user_paused = False
        self._wake.set()

    def stop(self, reason: str = "stopped") -> None:
        self._finish(reason)

    @property
    def outstanding(self) -> int:
        return self._last_sent_seq - self.stats.ack_seq

    def _observe_credit(self, credit: int) -> None:
        self._credit = credit
        elapsed = None if self._started_at is None else self._clock.monotonic() - self._started_at
        self.stats.observe_credit(credit, elapsed_s=elapsed)

    # ---- main loop ---------------------------------------------------------------------------
    async def run(self) -> SessionStats:
        self._started_at = self._clock.monotonic()
        while not self._done:
            try:
                ws = await self._connect(self.cfg.ingest_url)
            except OSError as exc:
                self.stats.errors.append((0, f"connect: {type(exc).__name__}"))
                if not await self._backoff():
                    break
                continue
            self._ws = ws
            try:
                await self._session(ws)
            except ConnectionClosed as closed:
                self.stats.close_codes[closed.code] += 1
                if self._done:
                    break
                if closed.code not in RESUMABLE_CLOSE_CODES:
                    self._finish(f"closed:{closed.code}")
                    break
                if not await self._backoff():
                    break
            finally:
                self._ws = None
        if self.stats.outcome == "running":
            self.stats.outcome = self._stop_reason or "stopped"
        return self.stats

    async def _backoff(self) -> bool:
        self.stats.reconnects += 1
        if self.stats.reconnects > self.cfg.max_reconnects:
            self._finish("reconnect_limit")
            return False
        await self._clock.sleep(self.cfg.reconnect_delay_s)
        return True

    async def _session(self, ws: WsTransport) -> None:
        resume = self._ever_connected
        hello: dict[str, Any] = {
            "t": "hello",
            "ticket": await self._tickets(self.cfg.session_id, "ingest"),
            "proto": 1,
            "codec": "pcm16le",
            "sample_rate": 16_000,
            "chunk_ms": self.cfg.chunk_ms,
            "resume": resume,
        }
        if resume:
            hello["last_sent_seq"] = self._last_sent_seq
        await ws.send(dumps(hello))
        welcome = await asyncio.wait_for(self._recv_json(ws), self.cfg.hello_timeout_s)
        if welcome.get("t") != "welcome":
            self._on_message(ws, welcome)
            raise ConnectionClosed(None, "no welcome")
        self._on_welcome(welcome, resume)
        self._ever_connected = True
        if self._done:
            await ws.close(1000)
            return
        receiver = asyncio.ensure_future(self._receive_loop(ws))
        try:
            await self._send_loop(ws, receiver)
            if self._done and self._server_stopped:
                # bye/error came from the server: observe its close code (recorded by run()).
                await asyncio.wait({receiver}, timeout=self.cfg.close_grace_s)
                self._raise_if_closed(receiver)
            elif self._done:
                await ws.close(1000)
        finally:
            if not receiver.done():
                receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await receiver
            for task in list(self._background):
                task.cancel()

    async def _recv_json(self, ws: WsTransport) -> dict[str, Any]:
        data = await ws.recv()
        return loads(data)

    async def _receive_loop(self, ws: WsTransport) -> None:
        while True:
            msg = await self._recv_json(ws)
            self._on_message(ws, msg)

    async def _send_loop(self, ws: WsTransport, receiver: asyncio.Future[None]) -> None:
        chunk_period = self.cfg.chunk_ms / 1000.0 / self.cfg.speed
        while not self._done:
            self._raise_if_closed(receiver)
            if self._resend:
                seq = self._resend.popleft()
                frame = self._ring.get(seq)
                if frame is None:
                    self._finish("gap_unrecoverable")
                    break
                await self._wait_for_credit(seq, receiver)
                await ws.send(frame)
                self._pending_ack[seq] = self._clock.monotonic()
                self.stats.resent += 1
                continue
            if self._next_seq > self.cfg.total_chunks:
                if await self._end_phase(ws, receiver):
                    break
                continue
            await self._wait_sendable(receiver)
            if self._done:
                break
            seq = self._next_seq
            last = seq == self.cfg.total_chunks
            frame = build_frame(
                seq,
                (seq - 1) * self.cfg.chunk_ms,
                chunk_payload(self.cfg.seed, seq, self.cfg.chunk_bytes),
                last=last,
                sim=self.cfg.sim,
            )
            self._ring.put(seq, frame)
            await self._wait_for_credit(seq, receiver)
            now = self._clock.monotonic()
            await ws.send(frame)
            self._next_seq = seq + 1
            self._last_sent_seq = seq
            self.stats.sent += 1
            self.stats.sent_at[seq] = now
            self._pending_ack[seq] = now
            await self._clock.sleep(chunk_period)

    async def _wait_sendable(self, receiver: asyncio.Future[None]) -> None:
        """Honour user pause, server ``pause{retry_ms}`` and injected delays."""
        while not self._done:
            self._raise_if_closed(receiver)
            if self._extra_delay_s > 0:
                delay, self._extra_delay_s = self._extra_delay_s, 0.0
                await self._clock.sleep(delay)
                continue
            now = self._clock.monotonic()
            if self._pause_until > now:
                await self._clock.sleep(self._pause_until - now)
                continue
            if self._user_paused:
                await self._wait_wake(receiver)
                continue
            return

    async def _wait_for_credit(self, seq: int, receiver: asyncio.Future[None]) -> None:
        """Block until sending ``seq`` keeps ``last_sent_seq - ack_seq <= credit``."""
        waited = False
        while not self._done and seq - self.stats.ack_seq > self._credit:
            if not waited:
                self.stats.credit_waits += 1
                waited = True
            await self._wait_wake(receiver)

    async def _wait_wake(self, receiver: asyncio.Future[None]) -> None:
        self._raise_if_closed(receiver)
        self._wake.clear()
        waiter: asyncio.Future[Any] = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({waiter, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        self._raise_if_closed(receiver)

    async def _end_phase(self, ws: WsTransport, receiver: asyncio.Future[None]) -> bool:
        """Send ``end`` and wait for ``bye{ended}``; returns False when a nack asks for re-sends."""
        self.stats.final_seq = self.cfg.total_chunks
        await ws.send(dumps({"t": "end", "final_seq": self.cfg.total_chunks}))
        deadline = self._clock.monotonic() + self.cfg.end_wait_s
        while not self._done and not self._resend:
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                self._finish("end_timeout")
                return True
            self._raise_if_closed(receiver)
            self._wake.clear()
            waiter: asyncio.Future[Any] = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait({waiter, receiver}, return_when=asyncio.FIRST_COMPLETED, timeout=remaining)
            finally:
                if not waiter.done():
                    waiter.cancel()
        return self._done

    @staticmethod
    def _raise_if_closed(receiver: asyncio.Future[None]) -> None:
        if receiver.done() and not receiver.cancelled():
            exc = receiver.exception()
            if exc is not None:
                raise exc

    # ---- server messages ---------------------------------------------------------------------
    def _on_welcome(self, msg: dict[str, Any], resume: bool) -> None:
        self._epoch = int(msg.get("epoch", 0))
        self.stats.epochs.append(self._epoch)
        self._observe_credit(int(msg.get("credit", 0)))
        self._advance_ack(int(msg.get("ack_seq", 0)))
        missing = [(int(a), int(b)) for a, b in msg.get("missing", [])]
        if resume:
            self.stats.missing_ranges.extend(missing)
            self.stats.resumes_ok += 1
        self._queue_resends(expand_ranges(missing))
        self._wake.set()

    def _on_message(self, ws: WsTransport, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "ack":
            self._observe_credit(int(msg["credit"]))
            self._advance_ack(int(msg["ack_seq"]))
        elif kind == "nack":
            self.stats.nacks += 1
            self._queue_resends(expand_ranges(msg.get("missing", [])))
        elif kind == "credit":
            self._observe_credit(int(msg["credit"]))
        elif kind == "pause":
            self.stats.pauses += 1
            self._pause_until = self._clock.monotonic() + int(msg.get("retry_ms", 1000)) / 1000.0
        elif kind == "ping":
            task = asyncio.ensure_future(
                self._safe_send(ws, dumps({"t": "pong", "ts": int(msg.get("ts", 0))}))
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        elif kind == "error":
            code = int(msg.get("code", 0))
            self.stats.errors.append((code, str(msg.get("message", ""))))
            if code == 4008:
                self._server_stopped = True
                self._finish("gap_unrecoverable")
        elif kind == "bye":
            reason = str(msg.get("reason", ""))
            if msg.get("ack_seq") is not None:
                self._advance_ack(int(msg["ack_seq"]))
            self._server_stopped = reason in {"ended", "superseded", "consent_revoked"}
            if reason == "ended":
                self._finish("ended")
            elif reason == "superseded":
                self.stats.superseded += 1
                self._finish("superseded")
            elif reason == "consent_revoked":
                self._finish("consent_revoked")
            # drain: the server closes 1012 next; run() reconnects with resume
        elif kind == "welcome":
            self._on_welcome(msg, resume=True)
        self._wake.set()

    async def _safe_send(self, ws: WsTransport, data: str) -> None:
        with contextlib.suppress(ConnectionClosed):
            await ws.send(data)

    def _advance_ack(self, ack_seq: int) -> None:
        if ack_seq <= self.stats.ack_seq:
            return
        self.stats.ack_seq = ack_seq
        now = self._clock.monotonic()
        for seq in [s for s in self._pending_ack if s <= ack_seq]:
            self.stats.ack_rtt_ms.append((now - self._pending_ack.pop(seq)) * 1000.0)

    def _queue_resends(self, seqs: Iterable[int]) -> None:
        queued = set(self._resend)
        for seq in seqs:
            if seq <= self.stats.ack_seq or seq in queued:
                continue
            if seq not in self._ring:
                self._finish("gap_unrecoverable")
                return
            self._resend.append(seq)
            queued.add(seq)

    def _finish(self, reason: str) -> None:
        if self._done:
            return
        self._done = True
        self._stop_reason = reason
        self.stats.outcome = reason
        self._wake.set()
        self._finished.set()


# --------------------------------------------------------------------------- viewer


@dataclass(frozen=True)
class ViewerConfig:
    session_id: str
    watch_url: str
    from_seq: int | None = None
    chunk_ms: int = 200
    """Recorder chunk width — maps a final's ``t_end_ms`` back to the chunk seq that carried it."""
    per_message_delay_s: float = 0.0
    """Scenario C: a slow viewer sleeps this long after every message."""
    auto_ack_alerts: bool = False
    hello_timeout_s: float = 5.0
    reconnect_delay_s: float = 0.2
    max_reconnects: int = 50
    stop_on_bye_ended: bool = True


class ViewerClient:
    """One clinician viewer: replays from ``from_seq``, dedups finals, measures e2e latencies."""

    def __init__(
        self,
        cfg: ViewerConfig,
        *,
        connect: Connector,
        tickets: TicketSource,
        clock: Clock | None = None,
        stats: SessionStats | None = None,
    ) -> None:
        self.cfg = cfg
        self.stats = stats or SessionStats(cfg.session_id)
        self._connect = connect
        self._tickets = tickets
        self._clock = clock or SystemClock()
        self._ws: WsTransport | None = None
        self._done = False
        self._from_seq = cfg.from_seq

    def abort_connection(self) -> None:
        if self._ws is not None:
            self._ws.abort()

    def stop(self) -> None:
        self._done = True
        if self._ws is not None:
            self._ws.abort()

    async def run(self) -> SessionStats:
        reconnects = 0
        while not self._done:
            try:
                ws = await self._connect(self.cfg.watch_url)
            except OSError as exc:
                self.stats.errors.append((0, f"connect: {type(exc).__name__}"))
                reconnects += 1
                if reconnects > self.cfg.max_reconnects:
                    break
                await self._clock.sleep(self.cfg.reconnect_delay_s)
                continue
            self._ws = ws
            try:
                await self._session(ws)
            except ConnectionClosed as closed:
                self.stats.close_codes[closed.code] += 1
                if self._done or closed.code in {1000, 4001, 4003, 4004, 4011, 4012}:
                    break
                reconnects += 1
                self.stats.viewer_reconnects += 1
                if reconnects > self.cfg.max_reconnects:
                    break
                await self._clock.sleep(self.cfg.reconnect_delay_s)
            finally:
                self._ws = None
        return self.stats

    async def _session(self, ws: WsTransport) -> None:
        hello: dict[str, Any] = {"t": "hello", "ticket": await self._tickets(self.cfg.session_id, "watch")}
        if self._from_seq is not None:
            hello["from_seq"] = self._from_seq
        await ws.send(dumps(hello))
        welcome = await asyncio.wait_for(ws.recv(), self.cfg.hello_timeout_s)
        msg = loads(welcome)
        if msg.get("t") != "welcome":
            raise ConnectionClosed(None, "no welcome")
        self.stats.states.append(str(msg.get("state", "")))
        while not self._done:
            msg = loads(await ws.recv())
            await self._on_message(ws, msg)
            if self.cfg.per_message_delay_s > 0:
                await self._clock.sleep(self.cfg.per_message_delay_s)
        await ws.close(1000)

    async def _on_message(self, ws: WsTransport, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "transcript.partial":
            self.stats.partials += 1
        elif kind == "transcript.final":
            self._on_final(msg)
        elif kind == "risk.alert":
            self.stats.alerts += 1
            committed = parse_iso(msg.get("committed_at"))
            if committed is not None:
                self.stats.alert_e2e_ms.append((self._clock.wall() - committed) * 1000.0)
            if self.cfg.auto_ack_alerts:
                await ws.send(dumps({"t": "risk.ack", "risk_event_id": str(msg["risk_event_id"])}))
        elif kind == "risk.ack":
            self.stats.alerts_acked += 1
        elif kind == "risk.escalated":
            self.stats.escalations += 1
        elif kind == "session.state":
            self.stats.states.append(str(msg.get("state", "")))
        elif kind == "note.status":
            self.stats.note_status = str(msg.get("status", ""))
        elif kind == "viewer.lagged":
            self.stats.lagged_dropped += int(msg.get("dropped_partials", 0))
        elif kind == "viewer.degraded":
            self.stats.degraded += 1
        elif kind == "ping":
            await ws.send(dumps({"t": "pong", "ts": int(msg.get("ts", 0))}))
        elif kind == "bye":
            if msg.get("reason") == "ended" and self.cfg.stop_on_bye_ended:
                self._done = True
            # reconnect asks for the *next* final (same rule as the console: ``lastFinalSeq + 1``)
            self._from_seq = self.stats.last_final_seq + 1 if self.stats.last_final_seq >= 0 else None

    def _on_final(self, msg: dict[str, Any]) -> None:
        seq = int(msg["seq"])
        if seq <= self.stats.last_final_seq:
            if seq == self.stats.last_final_seq:
                self.stats.final_dups += 1
            else:
                self.stats.final_out_of_order += 1
            return
        self.stats.last_final_seq = seq
        self.stats.finals += 1
        # §11.2: chunk sent → viewer final for *that utterance's last chunk*. Segment seqs (0-based)
        # and chunk seqs (1-based) are different axes; the simulator releases a final on the chunk
        # whose window contains ``t_end_ms`` (same rule as the console: ``t_end_ms → seq``).
        chunk_seq = last_chunk_seq(msg.get("t_end_ms"), self.cfg.chunk_ms)
        sent = None if chunk_seq is None else self.stats.sent_at.get(chunk_seq)
        if sent is not None:
            self.stats.final_e2e_ms.append((self._clock.monotonic() - sent) * 1000.0)


def last_chunk_seq(t_end_ms: object, chunk_ms: int) -> int | None:
    """Chunk seq (1-based) whose ``[offset, offset + chunk_ms)`` window contains ``t_end_ms``."""
    if not isinstance(t_end_ms, int | float) or isinstance(t_end_ms, bool) or chunk_ms <= 0:
        return None
    return int(t_end_ms) // chunk_ms + 1


def parse_iso(value: object) -> float | None:
    """ISO-8601 timestamp (``Z`` or offset) → POSIX seconds; ``None`` when absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- real I/O


class WebsocketsTransport:
    """``websockets`` (≥ 13, asyncio client) adapter; import is deferred so unit tests stay pure."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    @classmethod
    async def connect(cls, url: str) -> WebsocketsTransport:
        from websockets.asyncio.client import connect as ws_connect

        return cls(await ws_connect(url, max_size=2**20, open_timeout=10, ping_interval=None))

    async def send(self, data: bytes | str) -> None:
        try:
            await self._ws.send(data)
        except Exception as exc:
            raise self._closed(exc) from exc

    async def recv(self) -> bytes | str:
        try:
            data = await self._ws.recv()
        except Exception as exc:
            raise self._closed(exc) from exc
        return data  # type: ignore[no-any-return]

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code, reason=reason)

    def abort(self) -> None:
        transport = getattr(self._ws, "transport", None)
        if transport is not None:
            transport.abort()

    @staticmethod
    def _closed(exc: Exception) -> ConnectionClosed:
        from websockets.exceptions import ConnectionClosed as WsClosed

        if isinstance(exc, WsClosed):
            rcvd = exc.rcvd
            return ConnectionClosed(
                None if rcvd is None or rcvd.code == 1006 else rcvd.code, exc.__class__.__name__
            )
        return ConnectionClosed(None, type(exc).__name__)


class HttpTicketSource:
    """``POST /v1/sessions/{id}/ws-ticket`` with a bearer JWT (spec §6.9)."""

    def __init__(self, base_url: str, token: str, *, client: Any | None = None) -> None:
        import httpx

        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=10.0)
        self._headers = {"Authorization": f"Bearer {token}"}

    async def __call__(self, session_id: str, kind: Kind) -> str:
        resp = await self._client.post(
            f"/v1/sessions/{session_id}/ws-ticket", json={"kind": kind}, headers=self._headers
        )
        resp.raise_for_status()
        ticket = resp.json()["ticket"]
        if not isinstance(ticket, str):
            raise ValueError("ticket must be a string")
        return ticket

    async def aclose(self) -> None:
        await self._client.aclose()
