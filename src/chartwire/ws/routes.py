"""WebSocket endpoints and the per-process runtime they share (spec §6.1).

``create_app`` (WP-E) includes :data:`router` and calls :func:`on_startup` / :func:`on_shutdown`
from its lifespan. Both return an awaitable and do their synchronous part immediately, so they
work whether or not the caller awaits them; awaiting ``on_shutdown`` is what gives the drain its
20 s budget. ``app.state.ledger`` exposes the :class:`LedgerBatcher` (credit input, metrics).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, WebSocket
from redis.exceptions import RedisError

from chartwire.ops.drain import Drainer
from chartwire.redis import keys
from chartwire.redis.session_state import SessionState
from chartwire.ws.actions import CloseCode
from chartwire.ws.drain import API_DRAIN_DEADLINE_S, ConnectionRegistry
from chartwire.ws.ingest import IngestConnection
from chartwire.ws.ledger import LedgerBatcher
from chartwire.ws.pubsub import SubscriberManager
from chartwire.ws.runtime import WsRuntime
from chartwire.ws.watch import WatchConnection

log = logging.getLogger(__name__)

router = APIRouter()
NODE_HEARTBEAT_S = 10


def _runtime(websocket: WebSocket) -> WsRuntime | None:
    return getattr(websocket.app.state, "ws_runtime", None)


@router.websocket("/ws/v1/ingest")
async def ws_ingest(websocket: WebSocket) -> None:
    rt = _runtime(websocket)
    if rt is None or rt.registry.draining:
        await _refuse(websocket, rt)
        return
    await IngestConnection(websocket, rt).run()


@router.websocket("/ws/v1/watch")
async def ws_watch(websocket: WebSocket) -> None:
    rt = _runtime(websocket)
    if rt is None or rt.registry.draining:
        await _refuse(websocket, rt)
        return
    await WatchConnection(websocket, rt).run()


async def _refuse(websocket: WebSocket, rt: WsRuntime | None) -> None:
    """Draining node (1012: reconnect elsewhere) or runtime not started (4503)."""
    await websocket.accept()
    code = CloseCode.SERVICE_RESTART if rt is not None else CloseCode.DEPENDENCY_UNAVAILABLE
    await websocket.close(code=int(code), reason="drain" if rt is not None else "not_ready")


def on_startup(app: Any, deps: Any) -> Awaitable[None]:
    """Build the runtime from ``AppDeps`` (settings, engine, redis, keycache, objectstore, clock, node_id)."""
    settings = deps.settings
    ledger = LedgerBatcher(
        deps.engine, flush_ms=settings.ledger_flush_ms, flush_rows=settings.ledger_flush_rows
    )
    rt = WsRuntime(
        settings=settings,
        engine=deps.engine,
        redis=deps.redis,
        keycache=deps.keycache,
        objectstore=deps.objectstore,
        clock=deps.clock,
        node_id=deps.node_id,
        state=SessionState(deps.redis, stream_maxlen=settings.stream_maxlen),
        ledger=ledger,
        subscribers=SubscriberManager(deps.redis),
        registry=ConnectionRegistry(),
    )
    ledger.start()
    rt.subscribers.start()
    app.state.ws_runtime = rt
    app.state.ledger = ledger
    drainer = _drainer(app)  # SIGTERM → Drainer.begin → registry.begin (bye{drain} to every connection)
    if drainer is not None:
        drainer.on_begin(rt.registry.begin)
    app.state.ws_heartbeat = asyncio.get_running_loop().create_task(
        _node_heartbeat(rt), name="ws-node-heartbeat"
    )
    return asyncio.sleep(0)


def _drainer(app: Any) -> Any:
    """The process ``ops.drain.Drainer`` (WP-G). Created here when the ws router starts first; WP-G's
    ``ops.routes.on_startup`` reuses whatever sits at ``app.state.drainer``, so start order does not matter."""
    drainer = getattr(app.state, "drainer", None)
    if drainer is None:
        drainer = app.state.drainer = Drainer(deadline_s=API_DRAIN_DEADLINE_S)
    return drainer if hasattr(drainer, "on_begin") else None


def on_shutdown(app: Any) -> Awaitable[None]:
    """``bye{drain}`` to every connection → wait ≤20 s → flush the ledger → stop the pub/sub reader."""
    return asyncio.ensure_future(_shutdown(app))


async def _shutdown(app: Any) -> None:
    rt: WsRuntime | None = getattr(app.state, "ws_runtime", None)
    if rt is None:
        return
    heartbeat = getattr(app.state, "ws_heartbeat", None)
    if heartbeat is not None:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    rt.registry.begin()
    await rt.registry.wait_closed(API_DRAIN_DEADLINE_S)
    await rt.ledger.stop()
    await rt.subscribers.stop()
    with contextlib.suppress(RedisError, OSError):
        await rt.redis.delete(keys.node(rt.node_id))
    app.state.ws_runtime = None


async def _node_heartbeat(rt: WsRuntime) -> None:
    """``node:{node_id}`` HASH {conns, started_at} with a 30 s TTL (§5) — drain/ops visibility."""
    started_at = rt.clock.now().isoformat()
    while True:
        with contextlib.suppress(RedisError, OSError):
            async with rt.redis.pipeline(transaction=False) as pipe:
                pipe.hset(
                    keys.node(rt.node_id), mapping={"conns": len(rt.registry), "started_at": started_at}
                )
                pipe.expire(keys.node(rt.node_id), keys.TTL_NODE)
                await pipe.execute()
        await asyncio.sleep(NODE_HEARTBEAT_S)
