"""Transactional outbox (§7.1): PostgreSQL is the queue. Claim with ``FOR UPDATE SKIP LOCKED``,
lease, retry with jittered backoff, dead-letter after ``max_attempts``, prune ``done`` rows.

``now`` is injectable everywhere so the poison-message test can drive retries with a
``FakeClock``; ``None`` means the database clock.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from chartwire.db.models import DeadLetter, OutboxEvent, ProcessedEvent
from chartwire.outbox import backoff

MAX_BACKOFF_S = 300
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_LEASE_S = 60


def _now(now: datetime | None) -> ColumnElement[Any]:
    return func.now() if now is None else literal(now)


def backoff_seconds(attempts: int, rng: random.Random | None = None) -> float:
    """``min(300, 2**attempts) ± 20 %`` (attempts = failures so far, ≥1) — the single rule lives in
    :mod:`chartwire.outbox.backoff`; this is the repo-side wrapper."""
    jitter = (rng or random).uniform(-backoff.JITTER_RATIO, backoff.JITTER_RATIO)
    return backoff.base_delay_s(max(attempts, 0)) * (1 + jitter)


async def insert_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> int | None:
    """Returns the new event id, or ``None`` when ``idempotency_key`` was already emitted."""
    stmt = (
        pg_insert(OutboxEvent)
        .values(
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(OutboxEvent.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim_batch(
    session: AsyncSession,
    worker_id: str,
    limit: int = 100,
    *,
    lease_s: int = DEFAULT_LEASE_S,
    now: datetime | None = None,
) -> Sequence[OutboxEvent]:
    """Q4: ``pending`` rows due now → ``in_flight`` with a lease; safe for many workers."""
    ts = _now(now)
    due = (
        select(OutboxEvent.id)
        .where(OutboxEvent.status == "pending", OutboxEvent.next_attempt_at <= ts)
        .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(due))
        .values(
            status="in_flight",
            locked_by=worker_id,
            locked_at=ts,
            lease_until=ts + timedelta(seconds=lease_s),
        )
        .returning(OutboxEvent)
    )
    return (await session.scalars(stmt)).all()


_DONE_VALUES: dict[str, Any] = {
    "status": "done",
    "locked_by": None,
    "locked_at": None,
    "lease_until": None,
    "last_error": None,
}


async def mark_done(session: AsyncSession, event_id: int, *, now: datetime | None = None) -> None:
    await session.execute(
        update(OutboxEvent).where(OutboxEvent.id == event_id).values(done_at=_now(now), **_DONE_VALUES)
    )


async def mark_done_many(
    session: AsyncSession, event_ids: Sequence[int], *, now: datetime | None = None
) -> int:
    """Batch form of :func:`mark_done` (one UPDATE); returns the number of rows updated."""
    if not event_ids:
        return 0
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(list(event_ids)))
        .values(done_at=_now(now), **_DONE_VALUES)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def mark_failed(
    session: AsyncSession,
    event_id: int,
    *,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> str:
    """Retry with backoff or dead-letter; returns the resulting status (``pending`` | ``dead``)."""
    event = (
        await session.scalars(select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update())
    ).one()
    attempts = event.attempts + 1
    if attempts >= max_attempts:
        await mark_dead(session, event_id, error=error, now=now, attempts=attempts)
        return "dead"
    delay = timedelta(seconds=backoff_seconds(attempts, rng))
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            status="pending",
            attempts=attempts,
            next_attempt_at=_now(now) + delay,
            locked_by=None,
            locked_at=None,
            lease_until=None,
            last_error=error[:2000],
        )
    )
    return "pending"


async def mark_dead(
    session: AsyncSession,
    event_id: int,
    *,
    error: str,
    now: datetime | None = None,
    attempts: int | None = None,
) -> int:
    """``status='dead'`` + ``dead_letters`` row; returns the dead-letter id."""
    values: dict[str, Any] = {
        "status": "dead",
        "locked_by": None,
        "lease_until": None,
        "last_error": error[:2000],
    }
    if attempts is not None:
        values["attempts"] = attempts
    event = (
        await session.scalars(
            update(OutboxEvent).where(OutboxEvent.id == event_id).values(**values).returning(OutboxEvent)
        )
    ).one()
    stmt = (
        insert(DeadLetter)
        .values(
            tenant_id=event.tenant_id,
            outbox_event_id=event.id,
            event_type=event.event_type,
            payload=event.payload,
            attempts=event.attempts,
            last_error=event.last_error,
            died_at=_now(now),
        )
        .returning(DeadLetter.id)
    )
    return int((await session.execute(stmt)).scalar_one())


async def replay_dead(
    session: AsyncSession, event_id: int, *, now: datetime | None = None
) -> OutboxEvent | None:
    """``dlq.replay``: reset attempts/status, stamp ``dead_letters.replayed_at``."""
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id, OutboxEvent.status == "dead")
        .values(status="pending", attempts=0, next_attempt_at=_now(now), last_error=None)
        .returning(OutboxEvent)
    )
    event = (await session.scalars(stmt)).one_or_none()
    if event is not None:
        await session.execute(
            update(DeadLetter)
            .where(DeadLetter.outbox_event_id == event_id, DeadLetter.replayed_at.is_(None))
            .values(replayed_at=_now(now))
        )
    return event


async def reclaim_stuck(session: AsyncSession, *, now: datetime | None = None) -> int:
    """``in_flight`` rows whose lease expired go back to ``pending`` (attempts unchanged)."""
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.status == "in_flight", OutboxEvent.lease_until < _now(now))
        .values(status="pending", locked_by=None, locked_at=None, lease_until=None)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def mark_processed(session: AsyncSession, *, handler: str, event_id: int, tenant_id: UUID) -> bool:
    """Idempotency ledger; False = this handler already processed the event (skip)."""
    stmt = (
        pg_insert(ProcessedEvent)
        .values(handler=handler, event_id=event_id, tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["handler", "event_id"])
    )
    return bool((await session.execute(stmt)).rowcount)


async def prune_done(
    session: AsyncSession,
    *,
    older_than: timedelta = timedelta(hours=24),
    batch: int = 5000,
    now: datetime | None = None,
) -> int:
    victims = (
        select(OutboxEvent.id)
        .where(OutboxEvent.status == "done", OutboxEvent.done_at < _now(now) - older_than)
        .order_by(OutboxEvent.id)
        .limit(batch)
    )
    return int((await session.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(victims)))).rowcount or 0)


async def stats(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Per-status counts plus ``lag_seconds`` (age of the oldest pending row) for metrics."""
    counts = dict(
        (await session.execute(select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status))).all()
    )
    oldest = (
        await session.execute(
            select(func.extract("epoch", _now(now) - func.min(OutboxEvent.next_attempt_at))).where(
                OutboxEvent.status == "pending"
            )
        )
    ).scalar()
    return {status: int(counts.get(status, 0)) for status in ("pending", "in_flight", "done", "dead")} | {
        "lag_seconds": float(oldest or 0.0)
    }


async def list_dead_letters(session: AsyncSession, *, limit: int = 100) -> Sequence[DeadLetter]:
    return (await session.scalars(select(DeadLetter).order_by(DeadLetter.id.desc()).limit(limit))).all()
