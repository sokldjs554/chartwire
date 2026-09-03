"""``outbox.writer.emit`` — enqueue a domain event in the caller's transaction (spec §7.1)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.repo import outbox as outbox_repo


def idempotency_key(event_type: str, aggregate_id: UUID, version: int | str) -> str:
    """``f"{event_type}:{aggregate_id}:{version_or_ts}"`` — the convention every emitter uses."""
    return f"{event_type}:{aggregate_id}:{version}"


async def emit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> int | None:
    """Insert an ``outbox_events`` row alongside the domain change. Returns the event id, or
    ``None`` when the idempotency key was seen before (the event is already queued or done).
    The caller publishes ``outbox:wake`` after commit; the poller polls every second regardless."""
    return await outbox_repo.insert_event(
        session,
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
