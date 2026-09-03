"""Drainer: idempotent begin, hooks once, in-flight accounting, deadline, real SIGTERM via the loop."""

from __future__ import annotations

import asyncio
import signal

import pytest

from chartwire.ops.drain import DEFAULT_DEADLINE_S, Drainer
from chartwire.ops.health import Health


class FakeMono:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_initial_state_and_validation() -> None:
    d = Drainer()
    assert d.deadline_s == DEFAULT_DEADLINE_S == 25.0
    assert d.accepting and not d.draining and not d.drained
    assert d.remaining_s() is None and d.deadline_passed is False and d.reason is None
    with pytest.raises(ValueError, match="positive"):
        Drainer(deadline_s=0)


async def test_begin_is_idempotent_and_runs_hooks_once() -> None:
    mono = FakeMono()
    d = Drainer(deadline_s=25, monotonic=mono)
    health = Health()
    d.on_begin(health.mark_draining)
    hits: list[str] = []
    d.on_begin(lambda: hits.append("stop_claiming"))

    def bad() -> None:
        raise RuntimeError("hook failure must not abort the drain")

    d.on_begin(bad)
    d.on_begin(lambda: hits.append("after_bad"))

    assert d.begin("SIGTERM") is True
    assert d.begin("SIGTERM") is False
    assert hits == ["stop_claiming", "after_bad"]
    assert health.draining and d.draining and not d.accepting and d.reason == "SIGTERM"
    assert d.drained, "nothing in flight → drained immediately"
    assert (await health.readyz())[0] == 503


async def test_track_sets_drained_when_last_work_finishes() -> None:
    d = Drainer(deadline_s=5)
    started = asyncio.Event()
    release = asyncio.Event()

    async def job() -> None:
        async with d.track():
            started.set()
            await release.wait()

    tasks = [asyncio.create_task(job()) for _ in range(2)]
    await started.wait()
    await asyncio.sleep(0)
    assert d.in_flight == 2
    d.begin()
    assert not d.drained
    release.set()
    assert await d.wait_drained() is True
    assert d.in_flight == 0
    await asyncio.gather(*tasks)


async def test_track_decrements_even_when_work_raises() -> None:
    d = Drainer(deadline_s=5)
    with pytest.raises(RuntimeError):
        async with d.track():
            raise RuntimeError("handler failed")
    assert d.in_flight == 0
    d.begin()
    assert d.drained


async def test_wait_drained_returns_false_at_deadline() -> None:
    d = Drainer(deadline_s=0.05)
    block = asyncio.Event()

    async def stuck() -> None:
        async with d.track():
            await block.wait()

    task = asyncio.create_task(stuck())
    await asyncio.sleep(0)
    d.begin()
    assert await d.wait_drained() is False
    assert d.in_flight == 1
    block.set()
    await task


def test_remaining_and_deadline_with_fake_clock() -> None:
    mono = FakeMono()
    d = Drainer(deadline_s=20, monotonic=mono)
    d.begin()
    assert d.remaining_s() == 20.0
    mono.t += 12
    assert d.remaining_s() == 8.0 and not d.deadline_passed
    mono.t += 30
    assert d.remaining_s() == 0.0 and d.deadline_passed


async def test_mark_drained_short_circuits_wait() -> None:
    d = Drainer(deadline_s=5)
    async with d.track():
        d.begin()
        d.mark_drained()
        assert await d.wait_drained() is True


async def test_wait_draining_wakes_on_begin() -> None:
    d = Drainer()
    waiter = asyncio.create_task(d.wait_draining())
    await asyncio.sleep(0)
    assert not waiter.done()
    d.begin("test")
    await asyncio.wait_for(waiter, timeout=1)


async def test_sigterm_delivered_through_event_loop() -> None:
    d = Drainer(deadline_s=1)
    d.install(signals=(signal.SIGTERM,))
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(d.wait_draining(), timeout=1)
        assert d.reason == "SIGTERM"
    finally:
        d.uninstall()
    assert d.draining and d.drained
