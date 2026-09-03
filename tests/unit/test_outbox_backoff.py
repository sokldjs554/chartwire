"""Backoff: 2**attempts capped at 300 s, ±20 % jitter, seeded determinism, dead after max_attempts."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartwire.outbox.backoff import (
    JITTER_RATIO,
    MAX_BACKOFF_S,
    base_delay_s,
    next_attempt,
    plan_retry,
)

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (0, 1.0),
        (1, 2.0),
        (2, 4.0),
        (3, 8.0),
        (7, 128.0),
        (8, 256.0),
        (9, 300.0),
        (10, 300.0),
        (10_000, 300.0),
    ],
)
def test_base_delay(attempts: int, expected: float) -> None:
    assert base_delay_s(attempts) == expected


def test_negative_attempts_rejected() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        base_delay_s(-1)


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_attempt(1, NOW.replace(tzinfo=None), random.Random(0))


@settings(max_examples=300)
@given(attempts=st.integers(min_value=0, max_value=64), seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_jitter_stays_within_20_percent(attempts: int, seed: int) -> None:
    delay = (next_attempt(attempts, NOW, random.Random(seed)) - NOW).total_seconds()
    base = base_delay_s(attempts)
    assert base * (1 - JITTER_RATIO) <= delay <= base * (1 + JITTER_RATIO)
    assert delay <= MAX_BACKOFF_S * (1 + JITTER_RATIO)


def test_same_seed_same_schedule_and_different_seed_differs() -> None:
    a = next_attempt(3, NOW, random.Random(42))
    b = next_attempt(3, NOW, random.Random(42))
    c = next_attempt(3, NOW, random.Random(43))
    assert a == b
    assert a != c


def test_plan_retry_increments_and_schedules() -> None:
    plan = plan_retry(0, 8, NOW, random.Random(1))
    assert plan.attempts == 1
    assert plan.dead is False
    assert plan.next_attempt_at is not None
    assert 1.6 <= (plan.next_attempt_at - NOW).total_seconds() <= 2.4


def test_plan_retry_dead_exactly_at_max_attempts() -> None:
    alive = plan_retry(6, 8, NOW, random.Random(1))
    dead = plan_retry(7, 8, NOW, random.Random(1))
    assert alive.dead is False and alive.attempts == 7
    assert dead.dead is True and dead.attempts == 8 and dead.next_attempt_at is None


def test_plan_retry_max_attempts_one_dies_on_first_failure() -> None:
    assert plan_retry(0, 1, NOW, random.Random(1)).dead is True
    with pytest.raises(ValueError, match="max_attempts"):
        plan_retry(0, 0, NOW, random.Random(1))
