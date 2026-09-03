"""Injectable clocks so time-dependent code (leases, SLA timers, retention) is testable."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC wall-clock time."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; never goes backwards."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock; ``advance()`` moves both wall and monotonic time."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._mono = 1_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move time backwards")
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, at: datetime) -> None:
        if at < self._now:
            raise ValueError("cannot move time backwards")
        self.advance((at - self._now).total_seconds())
