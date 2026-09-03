"""Dead-letter bookkeeping as pure data (§7.1).

The first half is side-effect free: given a claimed event, an exception and the clock, it produces
the column values a failure writes (``outbox_events`` update + optional ``dead_letters`` insert) and
the values a replay resets — the executable specification of the retry rule, tested without a
database. The poller persists failures through ``db.repo.outbox.mark_failed`` (same rule, one SQL
home); the DB-facing ``list_dead`` / ``replay`` / ``stats_all`` at the bottom back the CLI and the
admin ops views.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.db.models import DeadLetter
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.outbox.backoff import RetryPlan, plan_retry
from chartwire.outbox.context import OutboxEvent, OutboxStatus
from chartwire.outbox.runtime import active_tenant_ids

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


# --- database-facing operations (Phase 1) -----------------------------------------------------------
# ``dead_letters`` / ``outbox_events`` are RLS tables, so every read or write happens under one tenant's
# context; cross-tenant CLI views iterate the active tenants (ADR-0001).


async def list_dead(
    engine: AsyncEngine, *, tenant_id: UUID | None = None, limit: int = 100
) -> list[DeadLetter]:
    """Dead letters newest first, for one tenant or across all active tenants."""
    tenants = [tenant_id] if tenant_id is not None else await active_tenant_ids(engine)
    rows: list[DeadLetter] = []
    for tid in tenants:
        async with tenant_tx(engine, TenantCtx.service(tid)) as session:
            rows.extend(await outbox_repo.list_dead_letters(session, limit=limit))
    rows.sort(key=lambda r: (r.died_at, r.id), reverse=True)
    return rows[:limit]


async def replay(
    engine: AsyncEngine, event_id: int, *, tenant_id: UUID | None = None, now: datetime | None = None
) -> bool:
    """``dlq.replay(id)`` (§7.1): ``dead`` → ``pending`` with ``attempts=0``; ``False`` if no such row."""
    tenants = [tenant_id] if tenant_id is not None else await active_tenant_ids(engine)
    for tid in tenants:
        async with tenant_tx(engine, TenantCtx.service(tid)) as session:
            if await outbox_repo.replay_dead(session, event_id, now=now) is not None:
                return True
    return False


async def stats_all(engine: AsyncEngine, *, now: datetime | None = None) -> dict[str, dict[str, float]]:
    """``chartwire outbox stats``: per-tenant status counts and lag, plus a ``total`` row."""
    out: dict[str, dict[str, float]] = {}
    total: dict[str, float] = {"pending": 0, "in_flight": 0, "done": 0, "dead": 0, "lag_seconds": 0.0}
    for tid in await active_tenant_ids(engine):
        async with tenant_tx(engine, TenantCtx.service(tid)) as session:
            row = await outbox_repo.stats(session, now=now)
        out[str(tid)] = {k: float(v) for k, v in row.items()}
        for key in ("pending", "in_flight", "done", "dead"):
            total[key] += float(row[key])
        total["lag_seconds"] = max(total["lag_seconds"], float(row["lag_seconds"]))
    out["total"] = total
    return out
