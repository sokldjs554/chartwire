"""Per-process objects the WebSocket shells share (built by ``ws.routes.on_startup``)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.clock import Clock
from chartwire.crypto import KeyCache
from chartwire.objectstore import ObjectStore
from chartwire.redis.session_state import SessionState
from chartwire.ws.drain import ConnectionRegistry
from chartwire.ws.ledger import LedgerBatcher
from chartwire.ws.pubsub import SubscriberManager

try:
    from chartwire.ops import metrics
except ImportError:  # pragma: no cover - ops package is WP-G; the shells still work without metrics
    from chartwire.ws import _nometrics as metrics  # type: ignore[no-redef]

__all__ = ["WsRuntime", "metrics", "now_ms"]


def now_ms() -> int:
    """Monotonic milliseconds for the cores (only differences matter; also the ``ping.ts`` value)."""
    return int(time.monotonic() * 1000)


@dataclass(slots=True)
class WsRuntime:
    settings: Any
    engine: AsyncEngine
    redis: Redis
    keycache: KeyCache
    objectstore: ObjectStore
    clock: Clock
    node_id: str
    state: SessionState
    ledger: LedgerBatcher
    subscribers: SubscriberManager
    registry: ConnectionRegistry
