"""Retry schedule for failed outbox handlers (§7.1).

``next_attempt_at = now + min(300 s, 2**attempts s) ± 20 % jitter`` where ``attempts`` is the count
*after* the failed execution has been added (first failure → ~2 s, second → ~4 s, …, 8th → dead).
The RNG is injected so tests are deterministic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_BACKOFF_S = 300.0
JITTER_RATIO = 0.2
_CAP_EXPONENT = 9  # 2**9 = 512 > MAX_BACKOFF_S; avoids huge powers for large attempt counts


def base_delay_s(attempts: int) -> float:
    """Un-jittered delay in seconds for a row that has now failed ``attempts`` times."""
    if attempts < 0:
        raise ValueError("attempts must be >= 0")
    if attempts >= _CAP_EXPONENT:
        return MAX_BACKOFF_S
    return min(MAX_BACKOFF_S, float(2**attempts))


def next_attempt(attempts: int, now: datetime, rng: random.Random) -> datetime:
    """``now`` + jittered backoff. ``now`` must be timezone-aware (DB columns are timestamptz)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    factor = 1.0 + rng.uniform(-JITTER_RATIO, JITTER_RATIO)
    return now + timedelta(seconds=base_delay_s(attempts) * factor)


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """Outcome of one failed execution: retry later or give up."""

    attempts: int
    """Attempt count to store (previous value + 1)."""
    dead: bool
    next_attempt_at: datetime | None
    """When to retry; ``None`` when ``dead``."""


def plan_retry(attempts_before: int, max_attempts: int, now: datetime, rng: random.Random) -> RetryPlan:
    """Decide what to do after a handler raised on a row that had ``attempts_before`` prior failures."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    attempts = attempts_before + 1
    if attempts >= max_attempts:
        return RetryPlan(attempts=attempts, dead=True, next_attempt_at=None)
    return RetryPlan(attempts=attempts, dead=False, next_attempt_at=next_attempt(attempts, now, rng))
