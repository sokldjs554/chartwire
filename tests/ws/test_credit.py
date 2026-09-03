"""Credit formula, change threshold, tolerance rule and the heartbeat timer (spec §6.4 rules 3 and 5)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartwire.ws import credit
from chartwire.ws.actions import Close, CloseCode, Send


@pytest.mark.parametrize(
    ("base", "lag", "pending", "expected"),
    [
        (50, 0, 0, 50),
        (50, 10, 0, 45),  # lag // 2
        (50, 0, 250, 48),  # pending // 100
        (50, 60, 1000, 10),
        (50, 1000, 0, 0),  # clamp low
        (200, 0, 0, 100),  # clamp high
        (50, 1, 99, 50),  # integer division floors both terms
    ],
)
def test_compute_formula(base, lag, pending, expected):
    assert credit.compute(base, lag, pending) == expected


@given(st.integers(0, 500), st.integers(0, 10_000), st.integers(0, 100_000))
def test_compute_is_clamped_and_monotone(base, lag, pending):
    c = credit.compute(base, lag, pending)
    assert 0 <= c <= credit.MAX_CREDIT
    assert credit.compute(base, lag + 2, pending) <= c
    assert credit.compute(base, lag, pending + 100) <= c


def test_compute_rejects_negative_inputs():
    with pytest.raises(ValueError, match="non-negative"):
        credit.compute(50, -1, 0)


@pytest.mark.parametrize(
    ("advertised", "current", "expected"),
    [
        (50, 37, True),  # 26 % drop
        (50, 38, False),  # 24 % drop
        (50, 63, True),
        (50, 62, False),
        (0, 1, True),  # any recovery from 0 is announced
        (0, 0, False),
        (4, 3, False),  # 25 % exactly is not "more than 25 %"
    ],
)
def test_changed_significantly(advertised, current, expected):
    assert credit.changed_significantly(advertised, current) is expected


def test_violates_uses_plus_twenty_tolerance():
    assert not credit.violates(70, 50)
    assert credit.violates(71, 50)
    assert not credit.violates(20, 0)
    assert credit.violates(21, 0)


def test_heartbeat_pings_every_15s_and_closes_after_two_missed_pongs():
    hb = credit.Heartbeat(0)
    assert hb.tick(14_999) == []
    assert hb.tick(15_000) == [Send({"t": "ping", "ts": 15_000})]
    assert hb.tick(20_000) == []
    assert hb.tick(30_000) == [Send({"t": "ping", "ts": 30_000})]
    assert hb.tick(45_000) == [Close(CloseCode.HEARTBEAT_TIMEOUT, "heartbeat_timeout")]


def test_heartbeat_pong_resets_the_counter():
    hb = credit.Heartbeat(0)
    hb.tick(15_000)
    hb.tick(30_000)
    hb.pong()
    assert isinstance(hb.tick(45_000)[0], Send)
    hb.pong()
    assert isinstance(hb.tick(60_000)[0], Send)
