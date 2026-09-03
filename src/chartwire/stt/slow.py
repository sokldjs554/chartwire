"""``SlowStt`` — wraps any adapter and adds a fixed delay per chunk (spec §3.1).

Load scenario B (§11.2) makes the STT stage the deliberate bottleneck with ``delay_ms=400`` on a
200 ms chunk cadence, so credit backpressure and ``pause{reason:'stt_lag'}`` can be observed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from chartwire.stt.base import Chunk, SessionInfo, SttAdapter, SttEvent, SttStream

Sleep = Callable[[float], Awaitable[None]]


class SlowStream:
    def __init__(self, inner: SttStream, delay_s: float, sleep: Sleep) -> None:
        self._inner = inner
        self._delay_s = delay_s
        self._sleep = sleep

    async def feed(self, chunk: Chunk) -> list[SttEvent]:
        if self._delay_s > 0:
            await self._sleep(self._delay_s)
        return await self._inner.feed(chunk)

    async def flush(self) -> list[SttEvent]:
        return await self._inner.flush()


class SlowStt:
    def __init__(self, adapter: SttAdapter, delay_ms: int, *, sleep: Sleep = asyncio.sleep) -> None:
        if delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")
        self._adapter = adapter
        self._delay_s = delay_ms / 1000.0
        self._sleep = sleep

    async def open(self, session: SessionInfo) -> SlowStream:
        return SlowStream(await self._adapter.open(session), self._delay_s, self._sleep)
