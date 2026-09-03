"""Periodic worker jobs that are not outbox events (§7.2): a coroutine run every ``interval_s``.

A failing tick is logged and the ticker continues; a tick in progress counts as in-flight work for
the :class:`~chartwire.ops.drain.Drainer`, and the ticker stops as soon as draining begins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from chartwire.ops.drain import Drainer

log = logging.getLogger(__name__)

TickFn = Callable[[], Awaitable[object]]


class Ticker:
    def __init__(
        self,
        name: str,
        interval_s: float,
        fn: TickFn,
        *,
        drainer: Drainer | None = None,
        run_at_start: bool = False,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.name = name
        self.interval_s = interval_s
        self._fn = fn
        self._drainer = drainer
        self._run_at_start = run_at_start
        self._stop = asyncio.Event()
        self.ticks = 0
        self.failures = 0

    @property
    def accepting(self) -> bool:
        return not self._stop.is_set() and (self._drainer is None or self._drainer.accepting)

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> None:
        """Run ``fn`` once, tracked as in-flight work; exceptions are logged, never raised."""
        self.ticks += 1
        try:
            if self._drainer is None:
                await self._fn()
            else:
                async with self._drainer.track():
                    await self._fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            log.exception("ticker failed", extra={"ticker": self.name})

    async def run(self) -> None:
        if self._run_at_start and self.accepting:
            await self.tick()
        while self.accepting:
            await self._sleep()
            if self.accepting:
                await self.tick()

    async def _sleep(self) -> None:
        """Sleep ``interval_s`` but return early when the ticker is stopped or draining begins."""
        waits: list[asyncio.Task[object]] = [asyncio.create_task(self._stop.wait())]
        if self._drainer is not None:
            waits.append(asyncio.create_task(self._drainer.wait_draining()))
        try:
            await asyncio.wait(waits, timeout=self.interval_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await w
