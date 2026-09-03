"""Append-only ``audit_events``. INSERT/SELECT only for the app role; the trigger rejects UPDATE/DELETE for everyone."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import AuditEvent


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID | None,
    actor_role: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Insert one row and return its id. No ``RETURNING`` (``inline()`` stops SQLAlchemy from adding
    one for the identity PK): a clinician may append but not read audit rows (RESTRICTIVE
    ``audit_read_gate``), and RETURNING is evaluated under the SELECT policies."""
    stmt = (
        insert(AuditEvent.__table__)
        .inline()
        .values(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            detail=detail or {},
        )
    )
    await session.execute(stmt)
    new_id = await session.execute(select(func.currval(func.pg_get_serial_sequence("audit_events", "id"))))
    return int(new_id.scalar_one())


async def list_events(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    before: int | None = None,
    limit: int = 100,
    action: str | None = None,
) -> Sequence[AuditEvent]:
    """Keyset by id (newest first); visible only to auditor/admin/service (RLS ``audit_select``)."""
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if before is not None:
        stmt = stmt.where(AuditEvent.id < before)
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    return (await session.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit))).all()
