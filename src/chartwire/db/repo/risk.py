"""``risk_events`` — detector hits, the open-alert SLA query (Q3), ack and escalation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import RiskEvent


async def insert_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    patient_id: UUID,
    segment_id: int,
    segment_created_at: datetime,
    segment_seq: int,
    category: str,
    severity: int,
    phrase: str,
    span_start: int,
    span_end: int,
    scope: dict,
    detector_version: str,
    detected_at: datetime,
    sla_deadline_at: datetime | None,
) -> RiskEvent:
    stmt = (
        insert(RiskEvent)
        .values(
            tenant_id=tenant_id,
            session_id=session_id,
            patient_id=patient_id,
            segment_id=segment_id,
            segment_created_at=segment_created_at,
            segment_seq=segment_seq,
            category=category,
            severity=severity,
            phrase=phrase,
            span_start=span_start,
            span_end=span_end,
            scope=scope,
            detector_version=detector_version,
            detected_at=detected_at,
            sla_deadline_at=sla_deadline_at,
        )
        .returning(RiskEvent)
    )
    return (await session.scalars(stmt)).one()


async def get_event(session: AsyncSession, event_id: int) -> RiskEvent | None:
    return await session.get(RiskEvent, event_id)


async def list_open(
    session: AsyncSession, tenant_id: UUID, *, limit: int = 100, session_ids: Sequence[UUID] | None = None
) -> Sequence[RiskEvent]:
    """Q3: unacknowledged alerts ordered by SLA deadline (partial index ``ix_risk_open_sla``)."""
    stmt = select(RiskEvent).where(RiskEvent.tenant_id == tenant_id, RiskEvent.acknowledged_at.is_(None))
    if session_ids is not None:
        stmt = stmt.where(RiskEvent.session_id.in_(list(session_ids)))
    return (
        await session.scalars(
            stmt.order_by(RiskEvent.sla_deadline_at.nulls_last(), RiskEvent.id).limit(limit)
        )
    ).all()


async def acknowledge(
    session: AsyncSession, event_id: int, *, by: UUID | None, now: datetime
) -> RiskEvent | None:
    """Idempotent: an already-acknowledged event is returned unchanged (``None`` = not visible)."""
    stmt = (
        update(RiskEvent)
        .where(RiskEvent.id == event_id, RiskEvent.acknowledged_at.is_(None))
        .values(acknowledged_at=now, acknowledged_by=by)
        .returning(RiskEvent)
    )
    event = (await session.scalars(stmt)).one_or_none()
    return event if event is not None else await get_event(session, event_id)


async def escalate(session: AsyncSession, event_id: int, *, now: datetime) -> RiskEvent | None:
    """One escalation step only (§7.2); returns ``None`` when nothing was escalated."""
    stmt = (
        update(RiskEvent)
        .where(RiskEvent.id == event_id, RiskEvent.acknowledged_at.is_(None), RiskEvent.escalation_level == 0)
        .values(escalation_level=1, escalated_at=now)
        .returning(RiskEvent)
    )
    return (await session.scalars(stmt)).one_or_none()
