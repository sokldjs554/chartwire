"""``GET /v1/audit?limit=&before=&action=`` (auditor, admin) — keyset by id, newest first.

The RESTRICTIVE ``audit_read_gate`` policy makes the same rule structural: a clinician token
reaches this route only through ``rbac.require`` and would see zero rows even without it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from chartwire.api.deps import open_tx
from chartwire.api.schemas import AuditList, AuditOut
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.db.models import AuditEvent
from chartwire.db.repo import audit as audit_repo

router = APIRouter(prefix="/v1", tags=["audit"])


def audit_out(row: AuditEvent) -> AuditOut:
    return AuditOut(
        id=int(row.id),
        at=row.at,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        request_id=row.request_id,
        detail=dict(row.detail or {}),
    )


@router.get("/audit", response_model=AuditList)
async def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    before: int | None = Query(default=None, ge=1),
    action: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(require("auditor", "admin")),
) -> AuditList:
    async with open_tx(request, principal) as (_deps, s):
        rows = await audit_repo.list_events(s, principal.tenant_id, before=before, limit=limit, action=action)
    items = [audit_out(r) for r in rows]
    return AuditList(items=items, next_before=items[-1].id if len(items) == limit else None)
