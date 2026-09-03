"""No-op stand-ins for :mod:`chartwire.ops.metrics` so the shells import without the ops package."""

from __future__ import annotations

from typing import Any


class _Noop:
    def labels(self, *_: Any) -> _Noop:
        return self

    def inc(self, *_: Any) -> None:
        pass

    def dec(self, *_: Any) -> None:
        pass

    def observe(self, *_: Any) -> None:
        pass

    def set(self, *_: Any) -> None:
        pass


WS_CONNECTIONS = WS_CHUNKS_TOTAL = WS_ACK_LATENCY_SECONDS = WS_CREDIT = WS_RESUME_TOTAL = _Noop()
WS_DROPPED_PARTIALS_TOTAL = _Noop()
