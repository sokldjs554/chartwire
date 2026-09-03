"""UUIDv7 (RFC 9562) — time-ordered ids for sessions, notes and purge jobs.

Python 3.11 has no ``uuid.uuid7``; this implementation keeps ids monotonic within a
process by using the 12 ``rand_a`` bits as a per-millisecond counter.
"""

from __future__ import annotations

import os
import threading
import time
from uuid import UUID

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7(now_ms: int | None = None) -> UUID:
    """Return a version-7 UUID; ``now_ms`` overrides the timestamp (tests)."""
    global _last_ms, _counter
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    with _lock:
        if ms <= _last_ms:
            ms = _last_ms
            _counter += 1
            if _counter >= 1 << 12:  # counter overflow: borrow the next millisecond
                ms += 1
                _counter = 0
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF  # random start, room to count
        _last_ms = ms
        rand_a = _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return UUID(int=value)


def uuid7_time_ms(value: UUID) -> int:
    """Extract the millisecond timestamp from a v7 UUID."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80
