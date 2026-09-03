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


class SubscriberManager:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._callbacks: dict[UUID, set[Callback]] = defaultdict(set)
        self._pubsub = redis.pubsub(ignore_subscribe_messages=True)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.degraded_events = 0

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
        """Register ``callback`` for the session; subscribes the channels on first use. Awaiting this
        returns only after Redis confirmed the subscription, so a following DB replay cannot miss a live event."""
        first = not self._callbacks[sid]
        self._callbacks[sid].add(callback)
        if first:
            await self._pubsub.subscribe(keys.sess_events(sid), keys.ctl(sid))

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
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None or message.get("type") != "message":
                continue
            self._dispatch(str(message["channel"]), message["data"])

    async def _resubscribe(self) -> None:
        with contextlib.suppress(RedisError, OSError):
            await self._pubsub.aclose()
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
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
