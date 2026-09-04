"""Recorder shell for ``GET /ws/v1/ingest`` (spec §6.1, §6.4): every I/O around :class:`IngestCore`.

Design: one connection = one *main task* that consumes an inbound event queue and is the only
caller of the core (the core is not concurrency-safe by design). Producer tasks feed the queue:
the socket receiver (frames/texts), a 200 ms ticker, the shared pub/sub reader (``ctl:{sid}``),
the ledger futures (``ledgered``) and the store worker (``store_failed``). Actions returned by the
core are executed in order by :meth:`IngestConnection._run`.

Store path (§6.4 rule 2), in the store worker, strictly in ``Store`` order so the ledger receives
rows in seq order: sha256 → AES-GCM under the session DEK → ``objectstore.put`` → ``xadd_chunk.lua``
(epoch-fenced) → ``LedgerBatcher.submit`` → future → ``on_ledgered`` → ``ack``. Redis is retried
three times; a chunk whose store path failed can never be acknowledged on this connection, so the
shell then sends ``error 4503 retryable`` and closes — the recorder resumes and ``welcome.missing``
is recomputed from the ledger (fail-closed, no loss).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import orjson
from fastapi import WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from chartwire.audit import service as audit
from chartwire.consent import gates
from chartwire.consent.service import active_scopes_for_patient
from chartwire.crypto import CryptoError, DekDestroyedError, Envelope, aad
from chartwire.db.models import Tenant
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore import chunk_key
from chartwire.redis import tickets
from chartwire.redis.session_state import StateLost
from chartwire.ws import actions as act
from chartwire.ws import messages as m
from chartwire.ws.actions import CloseCode
from chartwire.ws.codec import HEADER_LEN, FrameError, decode_frame
from chartwire.ws.core import IngestCore
from chartwire.ws.ledger import ChunkRow
from chartwire.ws.runtime import WsRuntime, metrics, now_ms

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
TICK_MS = 200
LAG_EVERY_TICKS = 5
"""``stt:lag`` is read once per second; the ledger backlog is local and sampled every tick."""
STORE_RETRIES = 3
STORE_RETRY_BACKOFF_S = 0.1
LEDGER_LOOKAHEAD = 1_000
"""Ledger rows read after ``sessions.ack_seq`` at hello (resume ``missing``); ≫ ring buffer + reorder buffer."""
TERMINAL_STATES = frozenset({"ended", "transcribed", "drafted", "signed", "purging", "purged"})


class DependencyFailure(RuntimeError):
    """Redis / ledger / object store failed after retries: close ``4503 retryable``."""


@dataclass(slots=True)
class _StoreJob:
    seq: int
    offset_ms: int
    flags: int
    payload: bytes


@dataclass(slots=True)
class _Session:
    """What the hello transaction learned; immutable for the life of the connection."""

    tenant_id: UUID
    session_id: UUID
    patient_id: UUID
    state: str
    ack_seq: int
    ledgered_after_ack: list[int]
    epoch: int
    started_at: Any
    kek_ref: str
    dek_wrapped: bytes | None
    scopes: set[str] = field(default_factory=set)


class IngestConnection:
    def __init__(self, ws: WebSocket, rt: WsRuntime, *, conn_id: str | None = None) -> None:
        self.ws, self.rt = ws, rt
        self.conn_id = conn_id or uuid4().hex[:12]
        self.events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.store_q: asyncio.Queue[_StoreJob] = asyncio.Queue()
        self.core: IngestCore | None = None
        self.sess: _Session | None = None
        self.ticket: tickets.TicketPayload | None = None
        self.dek = b""
        self.epoch = 0
        self.closed = False
        self._payloads: dict[int, bytes] = {}
        self._receipts: deque[tuple[int, float]] = deque()
        self._stt_lag = 0
        self._tick_no = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._subscribed = False

    # --- lifecycle ---------------------------------------------------------------------------------

    async def run(self) -> None:
        await self.ws.accept()
        metrics.WS_CONNECTIONS.labels("ingest").inc()
        self.rt.registry.add(self)
        try:
            if await self._handshake():
                await self._loop()
        except WebSocketDisconnect:
            self.closed = True
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        self.closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._subscribed and self.sess is not None:
            await self.rt.subscribers.unsubscribe(self.sess.session_id, self._on_pubsub)
        self.rt.registry.remove(self)
        self._payloads.clear()
        metrics.WS_CONNECTIONS.labels("ingest").dec()

    def drain(self) -> None:
        """Called by the drainer: the core sends ``bye{drain}`` and closes 1012 once flushed (§6.4 rule 7)."""
        self.events.put_nowait(("drain", None))

    # --- handshake ---------------------------------------------------------------------------------

    async def _handshake(self) -> bool:
        try:
            first = await asyncio.wait_for(self.ws.receive(), timeout=HELLO_TIMEOUT_S)
        except TimeoutError:
            return await self._fail(CloseCode.UNAUTHORIZED, "hello timeout")
        if first.get("type") == "websocket.disconnect":
            return False
        if "text" not in first:
            return await self._fail(CloseCode.BAD_HELLO, "first frame must be hello")
        try:
            hello = m.parse_client_message("ingest", first["text"])
        except m.MessageError as exc:
            return await self._fail(exc.close_code, str(exc))
        if not isinstance(hello, m.Hello):
            return await self._fail(CloseCode.BAD_HELLO, "first message must be hello")
        try:
            ticket = await tickets.consume(self.rt.redis, hello.ticket)
        except (RedisError, OSError):
            return await self._fail(
                CloseCode.DEPENDENCY_UNAVAILABLE, "ticket store unavailable", retryable=True
            )
        if ticket is None or ticket.kind != "ingest":
            return await self._fail(CloseCode.UNAUTHORIZED, "ticket invalid")
        self.ticket = ticket
        try:
            sess = await self._load_session(ticket)
        except SQLAlchemyError:
            return await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "database unavailable", retryable=True)
        if sess is None:
            return await self._fail(CloseCode.SESSION_NOT_FOUND, "session not found")
        if sess.state in TERMINAL_STATES:
            return await self._fail(CloseCode.SESSION_ENDED, f"session is {sess.state}")
        try:
            gates.require_scope(sess.scopes, "recording")
        except gates.ConsentScopeMissing as exc:
            return await self._fail(CloseCode(exc.ws_code), "consent scope 'recording' missing")
        try:
            self.dek = self.rt.keycache.get(sess.session_id, sess.kek_ref, sess.dek_wrapped)
        except DekDestroyedError:
            return await self._fail(CloseCode.SESSION_ENDED, "session key destroyed")
        except CryptoError:
            return await self._fail(
                CloseCode.DEPENDENCY_UNAVAILABLE, "key provider unavailable", retryable=True
            )
        self.sess = sess
        try:
            hot_state_present = await self._redis_hello(sess)
        except (RedisError, OSError):
            return await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "redis unavailable", retryable=True)
        try:
            await self._persist_hello(sess, hello)
        except SQLAlchemyError:
            return await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "database unavailable", retryable=True)
        self.core = IngestCore(
            now_ms=now_ms(), credit_base=self.rt.settings.credit_base, session_id=str(sess.session_id)
        )
        actions = self.core.on_hello(
            ack_seq_from_store=sess.ack_seq,
            resume=hello.resume,
            last_sent_seq=hello.last_sent_seq,
            epoch=self.epoch,
            now_ms=now_ms(),
            ledgered_after_ack=sess.ledgered_after_ack,
            hot_state_present=hot_state_present,
        )
        if hello.resume:
            metrics.WS_RESUME_TOTAL.labels("gap_unrecoverable" if self.core.closed else "ok").inc()
        await self._run(actions)
        return not self.closed

    async def _load_session(self, ticket: tickets.TicketPayload) -> _Session | None:
        async with tenant_tx(self.rt.engine, TenantCtx.service(ticket.tenant_id)) as s:
            row = await sessions_repo.get_session(s, ticket.session_id)
            if row is None or row.tenant_id != ticket.tenant_id:
                return None
            tenant = await s.get(Tenant, ticket.tenant_id)
            if tenant is None:
                return None
            ledgered = await sessions_repo.ledgered_seqs(
                s, row.id, row.ack_seq + 1, row.ack_seq + LEDGER_LOOKAHEAD
            )
            scopes = await active_scopes_for_patient(s, row.patient_id)
            return _Session(
                tenant_id=row.tenant_id,
                session_id=row.id,
                patient_id=row.patient_id,
                state=row.state,
                ack_seq=int(row.ack_seq),
                ledgered_after_ack=ledgered,
                epoch=int(row.epoch),
                started_at=row.started_at,
                kek_ref=tenant.kek_ref,
                dek_wrapped=row.dek_wrapped,
                scopes=scopes,
            )

    async def _redis_hello(self, sess: _Session) -> bool:
        """Subscribe ``ctl`` first (no fencing gap), rehydrate when the hash is gone, then ``hello.lua``."""
        await self.rt.subscribers.subscribe(sess.session_id, self._on_pubsub)
        self._subscribed = True
        hot = await self.rt.state.get(sess.session_id)
        if hot is None:
            await self.rt.state.rehydrate(
                sess.session_id,
                sess.ack_seq,
                sess.state,
                sess.started_at,
                epoch=sess.epoch,
                now=self.rt.clock.now(),
            )
            log.info("session state rehydrated", extra={"session_id": str(sess.session_id)})
        data = await self.rt.state.hello(
            sess.session_id, self.rt.node_id, self.conn_id, now=self.rt.clock.now()
        )
        self.epoch = int(data["epoch"])
        # the stt-worker resolves the tenant from the hash (no tenant walk) — WP-C request 1
        await self.rt.state.set_fields(sess.session_id, tenant=str(sess.tenant_id))
        return hot is not None

    async def _persist_hello(self, sess: _Session, hello: m.Hello) -> None:
        async with tenant_tx(self.rt.engine, TenantCtx.service(sess.tenant_id)) as s:
            await sessions_repo.update_session(s, sess.session_id, epoch=self.epoch)
            await audit.record(
                s,
                tenant_id=sess.tenant_id,
                actor_id=self.ticket.user_id if self.ticket else None,
                actor_role=self.ticket.role if self.ticket else None,
                action="ws.hello",
                resource_type="session",
                resource_id=sess.session_id,
                detail={
                    "kind": "ingest",
                    "epoch": self.epoch,
                    "resume": hello.resume,
                    "node": self.rt.node_id,
                },
            )

    # --- main loop ---------------------------------------------------------------------------------

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._receiver(), name="ingest-recv"),
            loop.create_task(self._ticker(), name="ingest-tick"),
            loop.create_task(self._store_worker(), name="ingest-store"),
        ]
        while not self.closed:
            kind, payload = await self.events.get()
            await self._handle(kind, payload)

    async def _handle(self, kind: str, payload: Any) -> None:
        assert self.core is not None and self.sess is not None
        if kind == "frame":
            await self._on_frame(payload)
        elif kind == "text":
            await self._on_text(payload)
        elif kind == "tick":
            await self._on_tick()
        elif kind == "ledgered":
            seqs = list(payload)
            seqs += self._drain_ledgered()  # coalesce: one on_ledgered call per burst of commits
            await self._run(self.core.on_ledgered(seqs))
        elif kind == "ctl":
            await self._on_ctl(payload)
        elif kind == "store_failed":
            await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "store path failed", retryable=True)
        elif kind == "drain":
            await self._run(self.core.on_drain(now_ms()))
        elif kind == "disconnect":
            self.closed = True

    def _drain_ledgered(self) -> list[int]:
        seqs: list[int] = []
        queue: deque[tuple[str, Any]] = self.events._queue  # type: ignore[attr-defined]
        while queue and queue[0][0] == "ledgered":
            seqs += self.events.get_nowait()[1]
        return seqs

    async def _on_frame(self, data: bytes) -> None:
        assert self.core is not None
        try:
            header = decode_frame(data, allow_sim=self.rt.settings.allow_sim_frames)
        except FrameError as exc:
            metrics.WS_CHUNKS_TOTAL.labels("rejected").inc()
            await self._fail(exc.close_code, exc.reason)
            return
        stats = self.core.stats
        before = (stats.duplicates, stats.dropped, stats.reordered)
        self._payloads[header.seq] = bytes(data[HEADER_LEN:])
        actions = self.core.on_chunk(header.seq, header.offset_ms, header.flags, now_ms())
        if stats.duplicates > before[0]:
            metrics.WS_CHUNKS_TOTAL.labels("duplicate").inc()
        elif stats.dropped > before[1] or stats.reordered > before[2]:
            metrics.WS_CHUNKS_TOTAL.labels("reordered").inc()
        if stats.duplicates > before[0] or stats.dropped > before[1]:
            self._payloads.pop(header.seq, None)  # the core did not keep it either
        await self._run(actions)

    async def _on_text(self, text: str) -> None:
        assert self.core is not None
        try:
            msg = m.parse_client_message("ingest", text)
        except m.MessageError as exc:
            await self._fail(exc.close_code, str(exc))
            return
        if isinstance(msg, m.End):
            await self._run(self.core.on_end(msg.final_seq, now_ms()))
        elif isinstance(msg, m.Pong):
            await self._run(self.core.on_pong(now_ms()))
        elif isinstance(msg, m.Pause):
            await self._set_state("paused")
        elif isinstance(msg, m.ResumeRec):
            await self._set_state("recording")
        else:
            await self._fail(CloseCode.BAD_HELLO, "hello already received")

    async def _on_tick(self) -> None:
        assert self.core is not None and self.sess is not None
        self._tick_no += 1
        if self._tick_no % LAG_EVERY_TICKS == 1:
            with contextlib.suppress(RedisError, OSError, ValueError):  # keep the last value on failure
                self._stt_lag = await self.rt.state.stt_lag(self.sess.session_id)
        await self._run(self.core.on_tick(now_ms(), self._stt_lag, self.rt.ledger.pending_rows()))

    async def _on_ctl(self, msg: Mapping[str, Any]) -> None:
        assert self.core is not None
        kind = msg.get("t")
        if kind == "superseded":
            await self._run(self.core.on_superseded(int(msg.get("epoch", 0))))
        elif kind == "consent_revoked":
            await self._run(self.core.on_consent_revoked())
        elif kind == "purge":
            await self._fail(CloseCode.SESSION_ENDED, "session purged")

    # --- producers ---------------------------------------------------------------------------------

    async def _receiver(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.events.put_nowait(("disconnect", None))
                return
            if msg.get("bytes") is not None:
                self.events.put_nowait(("frame", msg["bytes"]))
            elif msg.get("text") is not None:
                self.events.put_nowait(("text", msg["text"]))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_MS / 1000)
            self.events.put_nowait(("tick", None))

    def _on_pubsub(self, channel: str, msg: Mapping[str, Any]) -> None:
        if channel == "ctl":
            self.events.put_nowait(("ctl", msg))

    async def _store_worker(self) -> None:
        while True:
            job = await self.store_q.get()
            try:
                await self._store(job)
            except DependencyFailure as exc:
                log.warning("store path failed", extra={"seq": job.seq, "error": str(exc)})
                self.events.put_nowait(("store_failed", exc))
                return

    async def _store(self, job: _StoreJob) -> None:
        assert self.sess is not None
        sess, sid = self.sess, self.sess.session_id
        digest = hashlib.sha256(job.payload).digest()
        key = chunk_key(sess.tenant_id, sid, job.seq)
        blob = Envelope.encrypt(
            self.dek, job.payload, aad(sess.tenant_id, "session", sid, f"chunk:{job.seq}")
        )
        received_at = self.rt.clock.now()
        try:
            await self.rt.objectstore.put(key, blob)
        except OSError as exc:
            raise DependencyFailure(f"objectstore: {type(exc).__name__}") from exc
        fields = {
            "seq": job.seq,
            "key": key,
            "len": len(job.payload),
            "off": job.offset_ms,
            "fl": job.flags,
            "ep": self.epoch,
            "ts": int(received_at.timestamp() * 1000),
        }
        appended = await self._xadd_with_retry(fields)
        if not appended:
            metrics.WS_CHUNKS_TOTAL.labels("stale").inc()
            return  # superseded: the new connection's welcome.missing will ask for it again
        row = ChunkRow(
            sess.tenant_id, sid, job.seq, len(job.payload), digest, key, job.offset_ms, job.flags, received_at
        )
        future = self.rt.ledger.submit(row, ack_hint=job.seq)
        future.add_done_callback(lambda f, seq=job.seq: self._on_ledger_done(seq, f))
        metrics.WS_CHUNKS_TOTAL.labels("stored").inc()

    async def _xadd_with_retry(self, fields: Mapping[str, Any]) -> bool:
        assert self.sess is not None
        last: Exception | None = None
        for attempt in range(STORE_RETRIES):
            try:
                return await self.rt.state.xadd_chunk(
                    self.sess.session_id, self.epoch, fields, now=self.rt.clock.now()
                )
            except (RedisError, OSError, StateLost) as exc:
                last = exc
                await asyncio.sleep(STORE_RETRY_BACKOFF_S * (attempt + 1))
        raise DependencyFailure(f"redis: {type(last).__name__}")

    def _on_ledger_done(self, seq: int, future: asyncio.Future[None]) -> None:
        if future.cancelled() or self.closed:
            return
        if future.exception() is not None:
            self.events.put_nowait(("store_failed", future.exception()))
        else:
            self.events.put_nowait(("ledgered", [seq]))

    # --- actions -----------------------------------------------------------------------------------

    async def _run(self, actions: Iterable[act.Action]) -> None:
        assert self.core is not None
        for a in actions:
            if self.closed:
                return
            if isinstance(a, act.Store):
                payload = self._payloads.pop(a.seq, None)
                if (
                    payload is None
                ):  # cannot happen for a well-behaved core; treat as lost and let nack recover
                    log.error("store without payload", extra={"seq": a.seq})
                    continue
                self._receipts.append((a.seq, self.rt.clock.monotonic()))
                self.store_q.put_nowait(_StoreJob(a.seq, a.offset_ms, a.flags, payload))
            elif isinstance(a, act.Ack):
                await self._send(m.render(a))
                self._observe_ack(a)
            elif isinstance(a, act.Nack | act.SendCredit | act.Send):
                await self._send(m.render(a))
            elif isinstance(a, act.Transition):
                await self._transition(a.state)
            elif isinstance(a, act.Close):
                await self._close(a.code, a.reason)
            # Rehydrate: already done before hello.lua (the epoch must build on the persisted one)

    def _observe_ack(self, a: act.Ack) -> None:
        metrics.WS_CREDIT.observe(a.credit)
        now = self.rt.clock.monotonic()
        while self._receipts and self._receipts[0][0] <= a.ack_seq:
            metrics.WS_ACK_LATENCY_SECONDS.observe(now - self._receipts.popleft()[1])
        if self.sess is not None and self.core is not None:
            asyncio.get_running_loop().create_task(self._mirror_ack(a))

    async def _mirror_ack(self, a: act.Ack) -> None:
        assert self.sess is not None and self.core is not None
        with contextlib.suppress(RedisError, OSError):
            await self.rt.state.set_fields(
                self.sess.session_id,
                ack_seq=a.ack_seq,
                ledger_seq=self.core.ledger_seq,
                credit=a.credit,
                updated_at=self.rt.clock.now().isoformat(),
            )

    async def _transition(self, state: str) -> None:
        assert self.sess is not None and self.core is not None
        sess = self.sess
        try:
            async with tenant_tx(self.rt.engine, TenantCtx.service(sess.tenant_id)) as s:
                if state == "recording" and sess.started_at is None:
                    await sessions_repo.set_state(s, sess.session_id, state, now=self.rt.clock.now())
                    sess.started_at = self.rt.clock.now()
                elif state == "ended":
                    await sessions_repo.set_state(s, sess.session_id, state, now=self.rt.clock.now())
                    if self.core.final_seq is not None:
                        await sessions_repo.update_session(s, sess.session_id, final_seq=self.core.final_seq)
                    await audit.record(
                        s,
                        tenant_id=sess.tenant_id,
                        actor_id=self.ticket.user_id if self.ticket else None,
                        actor_role=self.ticket.role if self.ticket else None,
                        action="session.ended",
                        resource_type="session",
                        resource_id=sess.session_id,
                        detail={
                            "final_seq": self.core.final_seq,
                            "ack_seq": self.core.ack_seq,
                            "epoch": self.epoch,
                        },
                    )
                else:
                    await sessions_repo.update_session(s, sess.session_id, state=state)
        except SQLAlchemyError:
            await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "database unavailable", retryable=True)
            return
        with contextlib.suppress(RedisError, OSError):
            if state == "ended":
                # ``ended`` is committed *before* the end marker so the stt-worker's ``transcribed``
                # can never be overwritten by a late ``ended`` commit (WP-C request 2)
                await self.rt.state.xadd_end(sess.session_id, self.epoch)
            await self.rt.state.set_fields(
                sess.session_id, state=state, updated_at=self.rt.clock.now().isoformat()
            )
            await self.rt.state.publish_event(sess.session_id, {"t": "session.state", "state": state})
            if state == "ended":
                await self.rt.state.expire_after_end(sess.session_id)

    async def _set_state(self, state: str) -> None:
        """``pause{}`` / ``resume_rec{}``: session-state bookkeeping only (protocol doc §3.1)."""
        await self._transition(state)

    # --- socket ------------------------------------------------------------------------------------

    async def _send(self, msg: Mapping[str, Any]) -> None:
        try:
            await self.ws.send_text(orjson.dumps(msg).decode())
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.closed = True

    async def _fail(self, code: CloseCode, message: str, *, retryable: bool = False) -> bool:
        await self._send(act.error_msg(code, message, retryable=retryable))
        await self._close(int(code), act.CLOSE_REASONS.get(code, "error"))
        return False

    async def _close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.ws.close(code=code, reason=reason)
