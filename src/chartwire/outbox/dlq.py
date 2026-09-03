"""Dead-letter bookkeeping as pure data (§7.1).

Everything here is side-effect free: given a claimed event, an exception and the clock, it produces
the column values the poller writes (``outbox_events`` update + optional ``dead_letters`` insert) and
the values ``dlq.replay`` resets. Phase 1 adds the DB-facing ``replay(engine, event_id)`` /
``list_dead(...)`` on top of these builders; the decisions themselves are testable without a database.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from chartwire.outbox.backoff import RetryPlan, plan_retry
from chartwire.outbox.context import OutboxEvent, OutboxStatus

MAX_ERROR_CHARS = 2000
"""``last_error`` is diagnostic, not a log sink: type + message, truncated."""

_PHI_PATTERNS = (re.compile(r"\d{3}-\d{3,4}-\d{4}"), re.compile(r"\d{6}-\d{7}"))
"""Same phone / resident-number shapes that ``core.logging.redact_phi`` scrubs (§3.1)."""


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """Column values for one ``dead_letters`` row (§4.2, migration 0005)."""

    tenant_id: UUID
    outbox_event_id: int
    event_type: str
    payload: Mapping[str, Any]
    attempts: int
    last_error: str
    died_at: datetime


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    """What to persist after a handler raised: the row update and, if exhausted, the DLQ row."""

    plan: RetryPlan
    event_updates: Mapping[str, object]
    dead_letter: DeadLetterRecord | None

    @property
    def dead(self) -> bool:
        return self.dead_letter is not None


def format_error(exc: BaseException) -> str:
    """``"TypeName: message"`` with phone/RRN shapes redacted and length capped."""
    text = f"{type(exc).__qualname__}: {exc}".strip()
    for pattern in _PHI_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1] + "…"
    return text


def build_dead_letter(
    event: OutboxEvent, *, attempts: int, error: str, died_at: datetime
) -> DeadLetterRecord:
    return DeadLetterRecord(
        tenant_id=event.tenant_id,
        outbox_event_id=event.id,
        event_type=event.event_type,
        payload=event.payload,
        attempts=attempts,
        last_error=error,
        died_at=died_at,
    )


def on_failure(
    event: OutboxEvent,
    exc: BaseException,
    *,
    max_attempts: int,
    now: datetime,
    rng: random.Random,
) -> FailureOutcome:
    """Retry with backoff, or move to the DLQ once ``max_attempts`` executions have failed."""
    plan = plan_retry(event.attempts, max_attempts, now, rng)
    error = format_error(exc)
    unlock: dict[str, object] = {"locked_by": None, "locked_at": None, "lease_until": None}
    if plan.dead:
        updates: dict[str, object] = {
            "status": OutboxStatus.DEAD.value,
            "attempts": plan.attempts,
            "last_error": error,
            **unlock,
        }
        record = build_dead_letter(event, attempts=plan.attempts, error=error, died_at=now)
        return FailureOutcome(plan=plan, event_updates=updates, dead_letter=record)
    updates = {
        "status": OutboxStatus.PENDING.value,
        "attempts": plan.attempts,
        "next_attempt_at": plan.next_attempt_at,
        "last_error": error,
        **unlock,
    }
    return FailureOutcome(plan=plan, event_updates=updates, dead_letter=None)


def done_updates(now: datetime) -> Mapping[str, object]:
    """``mark_done``: the row is finished; locks cleared so ``stuck reclaim`` never touches it."""
    return {
        "status": OutboxStatus.DONE.value,
        "done_at": now,
        "locked_by": None,
        "locked_at": None,
        "lease_until": None,
    }


def replay_updates(now: datetime) -> Mapping[str, object]:
    """``dlq.replay``: reset attempts/status so the poller picks the row up on its next tick (§7.1)."""
    return {
        "status": OutboxStatus.PENDING.value,
        "attempts": 0,
        "next_attempt_at": now,
        "locked_by": None,
        "locked_at": None,
        "lease_until": None,
        "last_error": None,
    }
