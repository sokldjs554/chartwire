"""Graceful-shutdown state machine shared by api, worker and stt-worker (§6.4 step 7, §7.3).

SIGTERM → ``begin()`` → hooks run once (readiness 503, stop claiming, send ``bye{drain}``) → the
process finishes in-flight work under ``track()`` → ``drained`` is set when the count reaches zero →
``wait_drained()`` returns ``True``; past the deadline it returns ``False`` and the caller exits anyway.

This module owns only the state; the WebSocket ``bye``/flush choreography lives in ``ws/drain.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import AsyncIterator, Callable, Iterable

log = logging.getLogger(__name__)

DEFAULT_DEADLINE_S = 25.0
"""Worker budget (§7.3); the api passes 20 s (§6.4 step 7)."""

DEFAULT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

Hook = Callable[[], None]


class Drainer:
    def __init__(
        self,
        *,
        deadline_s: float = DEFAULT_DEADLINE_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if deadline_s <= 0:
            raise ValueError("deadline_s must be positive")
        self._deadline_s = deadline_s
        self._monotonic = monotonic
        self._started_at: float | None = None
        self._reason: str | None = None
        self._in_flight = 0
        self._hooks: list[Hook] = []
        self._draining = asyncio.Event()
        self._drained = asyncio.Event()
        self._installed: dict[signal.Signals, asyncio.AbstractEventLoop] = {}

    # --- inspection ---------------------------------------------------------------------------------
    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    @property
    def accepting(self) -> bool:
        """Inverse of ``draining``: may new connections/claims be taken?"""
        return not self._draining.is_set()

    @property
    def drained(self) -> bool:
        return self._drained.is_set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def deadline_s(self) -> float:
        return self._deadline_s

    def remaining_s(self) -> float | None:
        """Seconds left in the drain budget; ``None`` before ``begin()``; never negative."""
        if self._started_at is None:
            return None
        return max(0.0, self._deadline_s - (self._monotonic() - self._started_at))

    @property
    def deadline_passed(self) -> bool:
        remaining = self.remaining_s()
        return remaining is not None and remaining <= 0.0

    # --- lifecycle ----------------------------------------------------------------------------------
    def on_begin(self, hook: Hook) -> None:
        """Register a callback run exactly once when draining starts (readiness flip, stop claiming…)."""
        self._hooks.append(hook)

    def begin(self, reason: str = "signal") -> bool:
        """Start draining. Idempotent: returns ``False`` if already draining."""
        if self._draining.is_set():
            return False
        self._started_at = self._monotonic()
        self._reason = reason
        self._draining.set()
        log.info("drain started", extra={"reason": reason, "in_flight": self._in_flight})
        for hook in self._hooks:
            try:
                hook()
            except Exception:  # one failing hook must not stop the others or the drain itself
                log.exception("drain hook raised")
        if self._in_flight == 0:
            self._drained.set()
        return True

    def install(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        signals: Iterable[signal.Signals] = DEFAULT_SIGNALS,
    ) -> None:
        """Route ``signals`` to ``begin()`` on the running loop (Unix only, main thread)."""
        loop = loop or asyncio.get_running_loop()
        for sig in signals:
            loop.add_signal_handler(sig, self.begin, sig.name)
            self._installed[sig] = loop

    def uninstall(self) -> None:
        for sig, loop in self._installed.items():
            loop.remove_signal_handler(sig)
        self._installed.clear()

    # --- in-flight accounting -----------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def track(self) -> AsyncIterator[None]:
        """Wrap one unit of work (a handler run, a live session) that must finish before exit."""
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            if self._in_flight == 0 and self._draining.is_set():
                self._drained.set()

    def mark_drained(self) -> None:
        """Declare the drain complete regardless of the counter (e.g. after force-closing sockets)."""
        self._drained.set()

    async def wait_draining(self) -> None:
        await self._draining.wait()

    async def wait_drained(self) -> bool:
        """Block until in-flight work is done or the deadline expires; ``True`` = clean drain."""
        await self._draining.wait()
        remaining = self.remaining_s()
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=remaining)
        except TimeoutError:
            log.warning("drain deadline reached", extra={"in_flight": self._in_flight})
            return False
        return True
