"""Per-viewer outbound isolation (spec §6.7) without sockets: ``OutboundQueues`` semantics."""

from __future__ import annotations

import asyncio

import pytest

from chartwire.ws.watch import OutboundQueues


def partial(n: int) -> dict[str, object]:
    return {"t": "transcript.partial", "from_seq": n, "text": "…"}


def final(n: int) -> dict[str, object]:
    return {"t": "transcript.final", "seq": n}


def test_partial_overflow_drops_oldest_and_reports_lag_once_per_five_seconds():
    q = OutboundQueues(partial_max=3, critical_max=8)
    for n in range(5):
        assert q.put(partial(n), monotonic=0.0)
    assert q.dropped_partials == 2
    assert [m["from_seq"] for m in list(q.partial._queue)] == [2, 3, 4]  # type: ignore[attr-defined]
    notices = [m for m in list(q.critical._queue) if m["t"] == "viewer.lagged"]  # type: ignore[attr-defined]
    assert notices == [{"t": "viewer.lagged", "dropped_partials": 1}], "first drop reports immediately"
    q.put(partial(5), monotonic=4.9)  # inside the 5 s window: counted, not reported yet
    q.put(partial(6), monotonic=5.0)  # window elapsed: one notice carrying the unreported drops
    notices = [m for m in list(q.critical._queue) if m["t"] == "viewer.lagged"]  # type: ignore[attr-defined]
    assert notices[-1] == {"t": "viewer.lagged", "dropped_partials": 3} and len(notices) == 2
    assert q.dropped_partials == 4


def test_critical_overflow_is_reported_to_the_caller_and_partials_are_unaffected():
    q = OutboundQueues(partial_max=2, critical_max=2)
    assert q.put(final(1), monotonic=0.0) and q.put({"t": "risk.alert", "risk_event_id": 1}, monotonic=0.0)
    assert q.put(final(3), monotonic=0.0) is False, "third critical message: the viewer must be closed (4013)"
    assert q.put(partial(9), monotonic=0.0) is True


async def test_sender_sees_critical_before_partials_and_waits_when_empty():
    q = OutboundQueues(partial_max=4, critical_max=4)
    q.put(partial(1), monotonic=0.0)
    q.put(final(1), monotonic=0.0)
    q.put({"t": "session.state", "state": "ended"}, monotonic=0.0)
    q.put(partial(2), monotonic=0.0)
    kinds = [(await q.next())["t"] for _ in range(4)]
    assert kinds == ["transcript.final", "session.state", "transcript.partial", "transcript.partial"]
    waiter = asyncio.ensure_future(q.next())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    q.put(final(2), monotonic=1.0)
    assert (await asyncio.wait_for(waiter, 1))["seq"] == 2


@pytest.mark.parametrize("n", [1, 300])
def test_lagged_notice_needs_room_in_the_critical_queue(n: int):
    """A full critical queue cannot take the lag notice either; the drop is still counted."""
    q = OutboundQueues(partial_max=1, critical_max=1)
    q.put(final(1), monotonic=0.0)
    for i in range(n + 1):
        q.put(partial(i), monotonic=0.0)
    assert q.dropped_partials == n and q.critical.qsize() == 1
