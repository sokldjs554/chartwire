"""Admin ops views (§6.9): outbox stats, dead letters (+ replay via ``outbox.dlq``), segment partitions.

All three are tenant-scoped like everything else: an admin sees their tenant's outbox and DLQ. The
partition list is catalog metadata (``pg_inherits``) and carries no tenant data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import get_deps, not_found, open_tx, request_id
from chartwire.api.schemas import DeadLetterOut, OutboxStatsOut, PartitionOut, ReplayOut
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.db.repo import outbox as outbox_repo
from chartwire.outbox import dlq

router = APIRouter(prefix="/v1", tags=["ops"])

_PARTITIONS_SQL = text(
    "SELECT c.relname AS name, pg_get_expr(c.relpartbound, c.oid) AS bounds "
    "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
    "WHERE p.relname = 'transcript_segments' ORDER BY c.relname"
)


@router.get("/ops/outbox", response_model=OutboxStatsOut)
async def outbox_stats(request: Request, principal: Principal = Depends(require("admin"))) -> OutboxStatsOut:
    async with open_tx(request, principal) as (deps, s):
        stats = await outbox_repo.stats(s, now=deps.clock.now())
    return OutboxStatsOut(**stats)


@router.get("/ops/dead-letters", response_model=list[DeadLetterOut])
async def dead_letters(
    request: Request, principal: Principal = Depends(require("admin"))
) -> list[DeadLetterOut]:
    async with open_tx(request, principal) as (_deps, s):
        rows = await outbox_repo.list_dead_letters(s, limit=100)
    return [
        DeadLetterOut(
            id=int(r.id),
            outbox_event_id=int(r.outbox_event_id),
            event_type=r.event_type,
            attempts=int(r.attempts),
            last_error=r.last_error,
            died_at=r.died_at,
            replayed_at=r.replayed_at,
        )
        for r in rows
    ]


@router.post("/ops/dead-letters/{id}/replay", response_model=ReplayOut)
async def replay_dead_letter(
    id: int, request: Request, principal: Principal = Depends(require("admin"))
) -> ReplayOut:
    deps = get_deps(request)
    replayed = await dlq.replay(deps.engine, id, tenant_id=principal.tenant_id, now=deps.clock.now())
    if not replayed:
        raise not_found("dead letter")
    async with open_tx(request, principal) as (_deps, s):
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="outbox.replayed",
            resource_type="outbox_event",
            resource_id=str(id),
            request_id=request_id(request),
        )
    return ReplayOut(outbox_event_id=id, replayed=True)


@router.get("/ops/partitions", response_model=list[PartitionOut])
async def partitions(
    request: Request, principal: Principal = Depends(require("admin"))
) -> list[PartitionOut]:
    deps = get_deps(request)
    async with AsyncSession(deps.engine) as s:
        rows = (await s.execute(_PARTITIONS_SQL)).all()
    return [
        PartitionOut(name=str(name), bounds=bounds, is_default=(bounds or "").upper() == "DEFAULT")
        for name, bounds in rows
    ]
