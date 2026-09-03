"""Poison-message logic without a database: retry updates, DLQ record on the 8th failure, replay reset."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from chartwire.outbox.context import OutboxEvent, OutboxStatus
from chartwire.outbox.dlq import (
    MAX_ERROR_CHARS,
    DeadLetterRecord,
    done_updates,
    format_error,
    on_failure,
    replay_updates,
)

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)


def make_event(attempts: int = 0) -> OutboxEvent:
    return OutboxEvent(
        id=11,
        tenant_id=uuid4(),
        aggregate_type="session",
        aggregate_id=uuid4(),
        event_type="session.transcribed",
        payload={"session_id": "s", "patient_id": "p"},
        idempotency_key="session.transcribed:s:1",
        attempts=attempts,
        created_at=NOW - timedelta(minutes=1),
    )


def test_format_error_has_type_and_message_and_redacts_phone_and_rrn() -> None:
    exc = RuntimeError("call 010-1234-5678 or 900101-1234567 now")
    assert format_error(exc) == "RuntimeError: call [REDACTED] or [REDACTED] now"


def test_format_error_is_capped() -> None:
    text = format_error(ValueError("x" * (MAX_ERROR_CHARS * 2)))
    assert len(text) == MAX_ERROR_CHARS
    assert text.endswith("…")


def test_first_failure_retries_with_backoff_and_clears_lock() -> None:
    outcome = on_failure(make_event(0), RuntimeError("boom"), max_attempts=8, now=NOW, rng=random.Random(0))
    assert outcome.dead is False and outcome.dead_letter is None
    u = outcome.event_updates
    assert u["status"] == OutboxStatus.PENDING.value
    assert u["attempts"] == 1
    assert u["last_error"] == "RuntimeError: boom"
    assert (u["locked_by"], u["locked_at"], u["lease_until"]) == (None, None, None)
    delay = (u["next_attempt_at"] - NOW).total_seconds()  # type: ignore[operator]
    assert 1.6 <= delay <= 2.4


def test_poison_message_dies_on_eighth_failure_only() -> None:
    """A handler that always raises: attempts 1..7 retry, the 8th execution moves the row to the DLQ."""
    rng = random.Random(7)
    event = make_event(0)
    for _ in range(7):
        outcome = on_failure(event, RuntimeError("always"), max_attempts=8, now=NOW, rng=rng)
        assert outcome.dead is False
        event = OutboxEvent(**{**event.__dict__, "attempts": int(outcome.event_updates["attempts"])})  # type: ignore[arg-type,call-overload]
    assert event.attempts == 7
    final = on_failure(event, RuntimeError("always"), max_attempts=8, now=NOW, rng=rng)
    assert final.dead is True
    assert final.event_updates["status"] == OutboxStatus.DEAD.value
    assert final.event_updates["attempts"] == 8
    assert "next_attempt_at" not in final.event_updates
    record = final.dead_letter
    assert isinstance(record, DeadLetterRecord)
    assert (record.tenant_id, record.outbox_event_id, record.event_type) == (
        event.tenant_id,
        11,
        "session.transcribed",
    )
    assert record.payload == event.payload
    assert record.attempts == 8
    assert record.last_error == "RuntimeError: always"
    assert record.died_at == NOW


def test_custom_max_attempts_from_handler_spec() -> None:
    assert on_failure(make_event(2), OSError("x"), max_attempts=3, now=NOW, rng=random.Random(0)).dead is True
    assert (
        on_failure(make_event(1), OSError("x"), max_attempts=3, now=NOW, rng=random.Random(0)).dead is False
    )


def test_done_and_replay_updates() -> None:
    done = done_updates(NOW)
    assert done["status"] == "done" and done["done_at"] == NOW
    assert (done["locked_by"], done["locked_at"], done["lease_until"]) == (None, None, None)

    replay = replay_updates(NOW)
    assert replay["status"] == "pending"
    assert replay["attempts"] == 0
    assert replay["next_attempt_at"] == NOW
    assert replay["last_error"] is None
    assert (replay["locked_by"], replay["locked_at"], replay["lease_until"]) == (None, None, None)
