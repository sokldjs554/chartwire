"""SlowStt adds ``delay_ms`` before every chunk and passes events through unchanged."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from chartwire.stt.base import Chunk, Final, SessionInfo
from chartwire.stt.simulator import ScriptedSimulator
from chartwire.stt.slow import SlowStt

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scripts"


async def test_delay_per_chunk_and_passthrough():
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    slow = SlowStt(ScriptedSimulator(FIXTURES, latency=None), delay_ms=400, sleep=fake_sleep)
    info = SessionInfo(uuid4(), uuid4(), "t03", 100)
    stream = await slow.open(info)
    events = []
    for i in range(7):
        events += await stream.feed(Chunk(info.session_id, i + 1, i * 100, 0, b""))
    events += await stream.flush()
    assert slept == [0.4] * 7
    finals = [e for e in events if isinstance(e, Final)]
    assert [f.text for f in finals] == ["새벽 4시에 깨서 다시 못 자요"]


async def test_zero_delay_does_not_sleep_and_negative_is_rejected():
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    slow = SlowStt(ScriptedSimulator(FIXTURES, latency=None), delay_ms=0, sleep=fake_sleep)
    stream = await slow.open(SessionInfo(uuid4(), uuid4(), "t03", 100))
    await stream.feed(Chunk(uuid4(), 1, 0, 0, b""))
    assert slept == []
    with pytest.raises(ValueError):
        SlowStt(ScriptedSimulator(FIXTURES), delay_ms=-1)
