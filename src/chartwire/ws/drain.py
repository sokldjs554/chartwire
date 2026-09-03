"""Drain choreography for live WebSocket connections (spec §6.4 rule 7, §7.3).

``ConnectionRegistry`` tracks the recorder and viewer shells of this process. ``begin()`` tells
each one to send ``bye{drain}`` (recorders keep acking until their stored chunks are durable, then
close 1012 — the cores own that timing); ``wait_closed()`` blocks until every connection is gone
or the budget (20 s for the api) is spent. When WP-G's ``ops.drain.Drainer`` is on ``app.state``,
``routes.on_startup`` registers ``begin`` as one of its ``on_begin`` hooks so SIGTERM drives it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)

API_DRAIN_DEADLINE_S = 20.0


class Drainable(Protocol):
    closed: bool

    def drain(self) -> None: ...


class ConnectionRegistry:
    def __init__(self) -> None:
        self._conns: set[Drainable] = set()
        self._draining = False
        self._empty = asyncio.Event()
        self._empty.set()

    @property
    def draining(self) -> bool:
        return self._draining

    def __len__(self) -> int:
        return len(self._conns)

    def add(self, conn: Drainable) -> None:
        self._conns.add(conn)
        self._empty.clear()
        if self._draining:  # a connection that raced the drain start still gets its bye
            conn.drain()

    def remove(self, conn: Drainable) -> None:
        self._conns.discard(conn)
        if not self._conns:
            self._empty.set()

    def begin(self) -> int:
        """Idempotent. Returns how many connections were told to drain."""
        if self._draining:
            return 0
        self._draining = True
        for conn in list(self._conns):
            conn.drain()
        log.info("ws drain started", extra={"connections": len(self._conns)})
        return len(self._conns)

    async def wait_closed(self, deadline_s: float = API_DRAIN_DEADLINE_S) -> bool:
        """``True`` when every connection closed within the budget."""
        try:
            await asyncio.wait_for(self._empty.wait(), timeout=deadline_s)
        except TimeoutError:
            log.warning("ws drain deadline reached", extra={"connections": len(self._conns)})
            return False
        return True
