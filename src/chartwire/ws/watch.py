"""Viewer shell for ``GET /ws/v1/watch`` (spec §6.7): every I/O around :class:`WatchCore`.

Same shape as the ingest shell: one main task consumes an inbound queue (socket texts, live
pub/sub events, replay batches, ticks) and is the only caller of the core. Outbound isolation per
viewer: ``partial_q`` (256; full → drop the oldest, count, ``viewer.lagged`` at most every 5 s) and
``critical_q`` (1024; finals/alerts/state/note; full → close ``4013``). One sender task drains
both, critical first. Hello order is fixed: SUBSCRIBE → ``welcome`` → DB replay → live.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import orjson
from fastapi import WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from chartwire.audit import service as audit
from chartwire.crypto import CryptoError, DecryptError, Envelope, aad
from chartwire.db.models import Tenant, TranscriptSegment
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.redis import keys, tickets
from chartwire.ws import actions as act
from chartwire.ws import messages as m
from chartwire.ws.actions import CloseCode
from chartwire.ws.core import WatchCore
from chartwire.ws.runtime import WsRuntime, metrics, now_ms

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
TICK_MS = 200
REPLAY_BATCH = 100
GAP_FILL_RETRY_S = 0.2
"""A gap-fill replay that found nothing waits this long before reporting back, so a publisher that
announced a final before its row was visible cannot spin the viewer through replay requests."""
PARTIAL_Q_MAX = 256
CRITICAL_Q_MAX = 1_024
LAGGED_NOTICE_EVERY_S = 5.0
CLOSE_TIMEOUT_S = 5.0
"""A close frame to a viewer whose socket buffer is full must not hold the connection task hostage."""
NOBODY = UUID(int=0)
"""``risk.ack.by`` for principals without a user id (dev tokens)."""


def segment_aad(tenant_id: UUID, session_id: UUID, seq: int) -> str:
    """AAD of ``transcript_segments.text_enc`` — shared contract with the stt-worker (writer) and
    every reader (viewer replay, notes, REST segments)."""
    return aad(tenant_id, "session", session_id, f"segment:{seq}")


@dataclass(slots=True)
class _Session:
    tenant_id: UUID
    session_id: UUID
    state: str
    started_at: Any
    last_final_seq: int
    kek_ref: str
    dek_wrapped: bytes | None


class OutboundQueues:
    """Per-viewer isolation (§6.7). Pure asyncio, unit-tested without sockets."""

    def __init__(self, *, partial_max: int | None = None, critical_max: int | None = None) -> None:
        partial_max = PARTIAL_Q_MAX if partial_max is None else partial_max
        critical_max = CRITICAL_Q_MAX if critical_max is None else critical_max
        self.partial: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(partial_max)
        self.critical: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(critical_max)
        self.dropped_partials = 0
        self._unreported = 0
        self._last_notice: float | None = None
        self.wake = asyncio.Event()

    def put(self, msg: Mapping[str, Any], *, monotonic: float) -> bool:
        """Enqueue; ``False`` means the critical queue overflowed (caller closes ``4013``)."""
        if msg.get("t") == "transcript.partial":
            if self.partial.full():
                self.partial.get_nowait()
                self.dropped_partials += 1
                self._unreported += 1
                metrics.WS_DROPPED_PARTIALS_TOTAL.inc()
                if self._last_notice is None or monotonic - self._last_notice >= LAGGED_NOTICE_EVERY_S:
                    self._last_notice = monotonic
                    self._enqueue_critical({"t": "viewer.lagged", "dropped_partials": self._unreported})
                    self._unreported = 0
            self.partial.put_nowait(msg)
        elif not self._enqueue_critical(msg):
            return False
        self.wake.set()
        return True

    def _enqueue_critical(self, msg: Mapping[str, Any]) -> bool:
        if self.critical.full():
            return False
        self.critical.put_nowait(msg)
        self.wake.set()
        return True

    async def next(self) -> Mapping[str, Any]:
        """Critical messages first; waits when both queues are empty."""
        while True:
            if not self.critical.empty():
                return self.critical.get_nowait()
            if not self.partial.empty():
                return self.partial.get_nowait()
            self.wake.clear()
            await self.wake.wait()


class WatchConnection:
    def __init__(self, ws: WebSocket, rt: WsRuntime, *, conn_id: str | None = None) -> None:
        self.ws, self.rt = ws, rt
        self.conn_id = conn_id or uuid4().hex[:12]
        self.events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.out = OutboundQueues()
        self.core: WatchCore | None = None
        self.sess: _Session | None = None
        self.ticket: tickets.TicketPayload | None = None
        self.dek: bytes | None = None
        self.closed = False
        self._tasks: list[asyncio.Task[None]] = []
        self._replay: asyncio.Task[None] | None = None
        self._subscribed = False
        self._joined = False

    # --- lifecycle ---------------------------------------------------------------------------------

    async def run(self) -> None:
        await self.ws.accept()
        metrics.WS_CONNECTIONS.labels("watch").inc()
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
        for task in [*self._tasks, *([self._replay] if self._replay else [])]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.sess is not None:
            if self._subscribed:
                await self.rt.subscribers.unsubscribe(self.sess.session_id, self._on_pubsub)
            if self._joined:
                with contextlib.suppress(RedisError, OSError):
                    count = await self.rt.state.viewer_leave(self.sess.session_id, self.conn_id)
                    await self.rt.state.publish_event(
                        self.sess.session_id, {"t": "viewer.presence", "count": count}
                    )
        self.rt.registry.remove(self)
        metrics.WS_CONNECTIONS.labels("watch").dec()

    def drain(self) -> None:
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
            hello = m.parse_client_message("watch", first["text"])
        except m.MessageError as exc:
            return await self._fail(exc.close_code, str(exc))
        if not isinstance(hello, m.WatchHello):
            return await self._fail(CloseCode.BAD_HELLO, "first message must be hello")
        try:
            ticket = await tickets.consume(self.rt.redis, hello.ticket)
        except (RedisError, OSError):
            return await self._fail(
                CloseCode.DEPENDENCY_UNAVAILABLE, "ticket store unavailable", retryable=True
            )
        if ticket is None or ticket.kind != "watch":
            return await self._fail(CloseCode.UNAUTHORIZED, "ticket invalid")
        self.ticket = ticket
        try:
            sess = await self._load_session(ticket)
        except SQLAlchemyError:
            return await self._fail(CloseCode.DEPENDENCY_UNAVAILABLE, "database unavailable", retryable=True)
        if sess is None:
            return await self._fail(CloseCode.SESSION_NOT_FOUND, "session not found")
        self.sess = sess
        with contextlib.suppress(
            CryptoError
        ):  # purged session: replay carries no text, live events still flow
            self.dek = self.rt.keycache.get(sess.session_id, sess.kek_ref, sess.dek_wrapped)
        self.core = WatchCore(now_ms=now_ms(), session_id=str(sess.session_id))
        state: Any = sess.state if sess.state in m.SessionState.__args__ else "recording"
        await self._run(self.core.on_hello(hello.from_seq, state=state, last_final_seq=sess.last_final_seq))
        return not self.closed

    async def _load_session(self, ticket: tickets.TicketPayload) -> _Session | None:
        async with tenant_tx(self.rt.engine, TenantCtx.service(ticket.tenant_id)) as s:
            row = await sessions_repo.get_session(s, ticket.session_id)
            tenant = await s.get(Tenant, ticket.tenant_id)
            if row is None or tenant is None or row.tenant_id != ticket.tenant_id:
                return None
            stmt = select(func.coalesce(func.max(TranscriptSegment.seq), -1)).where(
                TranscriptSegment.session_id == row.id
            )
            if row.started_at is not None:
                stmt = stmt.where(TranscriptSegment.created_at >= row.started_at)
            last = int((await s.execute(stmt)).scalar_one())
            return _Session(
                row.tenant_id, row.id, row.state, row.started_at, last, tenant.kek_ref, row.dek_wrapped
            )

    # --- main loop ---------------------------------------------------------------------------------

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._receiver(), name="watch-recv"),
            loop.create_task(self._ticker(), name="watch-tick"),
            loop.create_task(self._sender(), name="watch-send"),
        ]
        while not self.closed:
            kind, payload = await self.events.get()
            await self._handle(kind, payload)

    async def _handle(self, kind: str, payload: Any) -> None:
        assert self.core is not None
        if kind == "text":
            await self._on_text(payload)
        elif kind == "event":
            await self._run(self.core.on_live_event(payload))
        elif kind == "replay_batch":
            await self._run(self.core.on_replay_batch(payload))
        elif kind == "replay_done":
            await self._run(self.core.on_replay_done())
        elif kind == "tick":
            await self._run(self.core.on_tick(now_ms()))
        elif kind == "ctl":
            await self._on_ctl(payload)
        elif kind == "drain":
            await self._bye("drain", CloseCode.SERVICE_RESTART)
        elif kind == "disconnect":
            self.closed = True

    async def _on_text(self, text: str) -> None:
        assert self.core is not None
        try:
            msg = m.parse_client_message("watch", text)
        except m.MessageError as exc:
            await self._fail(exc.close_code, str(exc))
            return
        if isinstance(msg, m.Pong):
            await self._run(self.core.on_pong(now_ms()))
        elif isinstance(msg, m.RiskAckRequest):
            await self._ack_alert(msg.risk_event_id)
        else:
            await self._fail(CloseCode.BAD_HELLO, "hello already received")

    async def _on_ctl(self, msg: Mapping[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "consent_revoked":
            await self._bye("consent_revoked", CloseCode.CONSENT_MISSING)
        elif kind == "purge":
            await self._fail(CloseCode.SESSION_ENDED, "session purged")

    async def _ack_alert(self, risk_event_id: int) -> None:
        """REST-equivalent of ``POST /v1/alerts/{id}/ack`` (§6.9) under the viewer's own tenant context."""
        assert self.sess is not None and self.ticket is not None
        sess, ticket = self.sess, self.ticket
        ctx = TenantCtx(sess.tenant_id, ticket.user_id, ticket.role)
        try:
            async with tenant_tx(self.rt.engine, ctx) as s:
                event = await risk_repo.acknowledge(
                    s, risk_event_id, by=ticket.user_id, now=self.rt.clock.now()
                )
                if event is None or event.session_id != sess.session_id:
                    return
                await audit.record(
                    s,
                    tenant_id=sess.tenant_id,
                    actor_id=ticket.user_id,
                    actor_role=ticket.role,
                    action="alert.acked",
                    resource_type="risk_event",
                    resource_id=str(risk_event_id),
                    detail={"session_id": str(sess.session_id), "via": "ws"},
                )
        except SQLAlchemyError:
            await self._send_now(
                act.error_msg(CloseCode.DEPENDENCY_UNAVAILABLE, "ack failed", retryable=True)
            )
            return
        with contextlib.suppress(RedisError, OSError):
            await self.rt.redis.zrem(keys.ALERTS_SLA, keys.alerts_sla_member(sess.tenant_id, risk_event_id))
            by = ticket.user_id or NOBODY
            await self.rt.state.publish_event(
                sess.session_id, m.dump(m.RiskAckEvent(risk_event_id=risk_event_id, by=by))
            )

    # --- producers ---------------------------------------------------------------------------------

    async def _receiver(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.events.put_nowait(("disconnect", None))
                return
            if msg.get("text") is not None:
                self.events.put_nowait(("text", msg["text"]))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_MS / 1000)
            self.events.put_nowait(("tick", None))

    async def _sender(self) -> None:
        while True:
            msg = await self.out.next()
            await self._send_now(msg)

    def _on_pubsub(self, channel: str, msg: Mapping[str, Any]) -> None:
        self.events.put_nowait(("ctl" if channel == "ctl" else "event", msg))

    async def _replay_task(self, after_seq: int, *, gap_fill: bool) -> None:
        assert self.sess is not None
        sess = self.sess
        fetched = 0
        try:
            while True:
                async with tenant_tx(self.rt.engine, TenantCtx.service(sess.tenant_id)) as s:
                    rows = await segments_repo.replay(
                        s, sess.session_id, after_seq, REPLAY_BATCH, started_at=sess.started_at
                    )
                if not rows:
                    break
                fetched += len(rows)
                self.events.put_nowait(("replay_batch", [self._final_from_row(r) for r in rows]))
                after_seq = rows[-1].seq
                if len(rows) < REPLAY_BATCH:
                    break
            async with tenant_tx(self.rt.engine, TenantCtx.service(sess.tenant_id)) as s:
                alerts = await risk_repo.list_open(s, sess.tenant_id, session_ids=[sess.session_id])
            if alerts:
                self.events.put_nowait(("replay_batch", [self._alert_from_row(a) for a in alerts]))
        except SQLAlchemyError as exc:
            log.warning("replay failed", extra={"error": type(exc).__name__})
            self.events.put_nowait(("event", {"t": "viewer.degraded"}))
        if gap_fill and fetched == 0:
            await asyncio.sleep(GAP_FILL_RETRY_S)
        self.events.put_nowait(("replay_done", None))

    def _final_from_row(self, row: segments_repo.SegmentRow) -> dict[str, Any]:
        assert self.sess is not None
        text = ""
        if self.dek is not None:
            try:
                text = Envelope.decrypt(
                    self.dek, row.text_enc, segment_aad(self.sess.tenant_id, self.sess.session_id, row.seq)
                ).decode("utf-8")
            except DecryptError:
                log.error("segment decrypt failed", extra={"seq": row.seq})
                text = "[복호화 실패]"
        msg = m.TranscriptFinal(
            seq=row.seq,
            speaker=row.speaker,  # type: ignore[arg-type]
            t_start_ms=row.t_start_ms,
            t_end_ms=row.t_end_ms,
            text=text,
            confidence=row.confidence if row.confidence is not None else 0.0,
            segment_id=row.id,
        )
        return m.dump(msg)

    @staticmethod
    def _alert_from_row(row: Any) -> dict[str, Any]:
        msg = m.RiskAlert(
            risk_event_id=row.id,
            category=row.category,
            severity=row.severity,
            segment_seq=row.segment_seq,
            span=(row.span_start, row.span_end),
            sla_deadline_at=row.sla_deadline_at.isoformat() if row.sla_deadline_at else None,
        )
        return m.dump(msg)

    # --- actions -----------------------------------------------------------------------------------

    async def _run(self, actions: Iterable[act.Action]) -> None:
        assert self.core is not None and self.sess is not None
        for a in actions:
            if self.closed:
                return
            if isinstance(a, act.Subscribe):
                await self._subscribe()
            elif isinstance(a, act.Send):
                if not self.out.put(a.msg, monotonic=self.rt.clock.monotonic()):
                    await self._close(
                        int(CloseCode.VIEWER_TOO_SLOW), act.CLOSE_REASONS[CloseCode.VIEWER_TOO_SLOW]
                    )
            elif isinstance(a, act.Replay):
                self._replay = asyncio.get_running_loop().create_task(
                    self._replay_task(a.after_seq, gap_fill=self._replay is not None), name="watch-replay"
                )
            elif isinstance(a, act.Close):
                await self._close(a.code, a.reason)

    async def _subscribe(self) -> None:
        assert self.sess is not None
        try:
            await self.rt.subscribers.subscribe(self.sess.session_id, self._on_pubsub)
            self._subscribed = True
            count = await self.rt.state.viewer_join(self.sess.session_id, self.conn_id)
            self._joined = True
            await self.rt.state.publish_event(self.sess.session_id, {"t": "viewer.presence", "count": count})
        except (RedisError, OSError):
            self.out.put({"t": "viewer.degraded"}, monotonic=self.rt.clock.monotonic())

    # --- socket ------------------------------------------------------------------------------------

    async def _send_now(self, msg: Mapping[str, Any]) -> None:
        try:
            await self.ws.send_text(orjson.dumps(msg).decode())
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.closed = True

    async def _bye(self, reason: m.ByeReason, code: CloseCode) -> None:
        await self._send_now(m.dump(m.Bye(reason=reason)))
        await self._close(int(code), act.CLOSE_REASONS[code])

    async def _fail(self, code: CloseCode, message: str, *, retryable: bool = False) -> bool:
        await self._send_now(act.error_msg(code, message, retryable=retryable))
        await self._close(int(code), act.CLOSE_REASONS.get(code, "error"))
        return False

    async def _close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        for task in self._tasks:  # a sender blocked on a full socket would otherwise delay the close frame
            if task.get_name() == "watch-send":
                task.cancel()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
            await asyncio.wait_for(self.ws.close(code=code, reason=reason), timeout=CLOSE_TIMEOUT_S)
