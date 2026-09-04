"""One Redis PUB/SUB reader per api process, shared by every live session on the node (spec §6.7).

Each session subscribes two channels — ``sess:{sid}:events`` (viewer fan-out) and ``ctl:{sid}``
(superseded / consent_revoked / purge). Connections register a callback per session; channels are
subscribed on the first registration and unsubscribed on the last. Callbacks must not block: they
only enqueue into the connection's inbound queue. If the reader loses Redis every callback gets a
``{"t": "viewer.degraded"}`` event, and the reader reconnects with backoff and re-subscribes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from chartwire.redis import keys

log = logging.getLogger(__name__)

Callback = Callable[[str, Mapping[str, Any]], None]
"""``callback(channel_kind, message)`` where ``channel_kind`` is ``"events"`` or ``"ctl"``."""

DEGRADED: Mapping[str, Any] = {"t": "viewer.degraded"}
RECONNECT_BACKOFF_S = (0.1, 0.5, 1.0, 2.0, 5.0)
SUBSCRIBE_ACK_TIMEOUT_S = 2.0
"""Bound on waiting for Redis to confirm a SUBSCRIBE. On timeout the caller proceeds anyway: the
viewer's ``_pending`` gap-fill replay and its reconnect are the second line of defence, and blocking
a connect on a sick Redis is worse than the residual race."""


class SubscriberManager:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._callbacks: dict[UUID, set[Callback]] = defaultdict(set)
        # ignore_subscribe_messages=False: redis-py's handle_message drops subscribe/unsubscribe
        # frames when *either* the constructor flag or the per-call flag is set, and
        # :meth:`subscribe` needs to see them. ``_read_loop`` filters them out itself.
        self._pubsub = redis.pubsub(ignore_subscribe_messages=False)
        self._acks: dict[str, asyncio.Event] = {}
        """channel → event set when Redis's SUBSCRIBE confirmation for it is read (see :meth:`subscribe`)."""
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.degraded_events = 0
        self.subscribe_timeouts = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="ws-pubsub")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with contextlib.suppress(RedisError, OSError):
            await self._pubsub.aclose()

    @property
    def sessions(self) -> int:
        return len(self._callbacks)

    async def subscribe(self, sid: UUID, callback: Callback) -> None:
        """Register ``callback`` for the session; subscribes the channels on first use.

        Awaiting this returns only after Redis *confirmed* the subscription. redis-py's
        ``PubSub.subscribe`` only writes the SUBSCRIBE bytes and never reads a reply, so without the
        wait below a viewer could return here while the command was still in flight, have Redis
        process a PUBLISH before it, and then take a DB replay snapshot older than that publish — the
        final would appear in neither and be invisible until the next gap-fill or reconnect. The reader
        task is the only socket reader, so the confirmation frames are resolved there
        (:meth:`_confirm`) and the waiters are armed *before* the command is written.
        """
        first = not self._callbacks[sid]
        self._callbacks[sid].add(callback)
        if first:
            channels = (keys.sess_events(sid), keys.ctl(sid))
            waiters = [self._acks.setdefault(c, asyncio.Event()) for c in channels]
            try:
                await self._pubsub.subscribe(*channels)
                await self._await_acks(waiters)
            finally:
                for channel in channels:
                    self._acks.pop(channel, None)

    async def _await_acks(self, waiters: list[asyncio.Event]) -> None:
        try:
            async with asyncio.timeout(SUBSCRIBE_ACK_TIMEOUT_S):
                for waiter in waiters:
                    await waiter.wait()
        except TimeoutError:
            self.subscribe_timeouts += 1
            log.warning("subscribe confirmation timed out", extra={"channels": len(waiters)})

    def _confirm(self, channel: str) -> None:
        event = self._acks.get(channel)
        if event is not None:
            event.set()

    async def unsubscribe(self, sid: UUID, callback: Callback) -> None:
        callbacks = self._callbacks.get(sid)
        if callbacks is None:
            return
        callbacks.discard(callback)
        if not callbacks:
            del self._callbacks[sid]
            with contextlib.suppress(RedisError, OSError):
                await self._pubsub.unsubscribe(keys.sess_events(sid), keys.ctl(sid))

    # --- reader ------------------------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._closed:
            try:
                await self._read_loop()
                attempt = 0
            except (RedisError, OSError) as exc:
                log.warning("pubsub reader lost redis", extra={"error": type(exc).__name__})
                self._broadcast_degraded()
                await asyncio.sleep(RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)])
                attempt += 1
                await self._resubscribe()

    async def _read_loop(self) -> None:
        while not self._closed:
            if not self._pubsub.subscribed:
                await asyncio.sleep(0.05)
                continue
            message = await self._pubsub.get_message(ignore_subscribe_messages=False, timeout=1.0)
            if message is None:
                continue
            kind = message.get("type")
            if kind in ("subscribe", "unsubscribe"):  # the reply `subscribe()` waits for
                self._confirm(str(message["channel"]))
                continue
            if kind != "message":
                continue
            self._dispatch(str(message["channel"]), message["data"])

    async def _resubscribe(self) -> None:
        """Re-arm every channel after a reconnect. This runs in the reader task itself, so it cannot
        wait for confirmations (nothing would read them); viewers were already told ``viewer.degraded``
        and recover through the gap-fill replay."""
        with contextlib.suppress(RedisError, OSError):
            await self._pubsub.aclose()
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=False)
        channels = [c for sid in self._callbacks for c in (keys.sess_events(sid), keys.ctl(sid))]
        if channels:
            with contextlib.suppress(RedisError, OSError):
                await self._pubsub.subscribe(*channels)

    def _dispatch(self, channel: str, data: Any) -> None:
        kind, sid = _parse_channel(channel)
        if sid is None:
            return
        try:
            payload = orjson.loads(data)
        except orjson.JSONDecodeError:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("t"), str):
            return
        for callback in list(self._callbacks.get(sid, ())):
            callback(kind, payload)

    def _broadcast_degraded(self) -> None:
        self.degraded_events += 1
        for callbacks in list(self._callbacks.values()):
            for callback in list(callbacks):
                callback("events", DEGRADED)


def _parse_channel(channel: str) -> tuple[str, UUID | None]:
    try:
        if channel.startswith("ctl:"):
            return "ctl", UUID(channel[4:])
        if channel.startswith("sess:") and channel.endswith(":events"):
            return "events", UUID(channel[5:-7])
    except ValueError:
        pass
    return "", None
