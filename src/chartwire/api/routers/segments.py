"""Decrypted transcript reads (§6.9): session segments (keyset by ``seq``, ``started_at`` passed for
partition pruning — Q1a) and the patient timeline across sessions (Q1 keyset on ``(created_at, id)``).

Text is decrypted per row under that row's session DEK (AAD ``segment_aad``), never cached, never logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import AppDeps, load_tenant, not_found, open_tx, session_dek
from chartwire.api.routers.sessions import owned_session
from chartwire.api.schemas import SegmentOut, TimelineOut, TimelineSegmentOut
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.core.errors import AppError
from chartwire.crypto.envelope import Envelope
from chartwire.db.models import Tenant
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.repo.segments import SegmentRow
from chartwire.ws.watch import segment_aad

router = APIRouter(prefix="/v1", tags=["segments"])
CLINICAL = ("clinician", "staff")
TIMELINE_WINDOW = timedelta(days=183)


def decrypt_segment(dek: bytes, tenant_id: UUID, row: SegmentRow) -> str:
    return Envelope.decrypt(dek, bytes(row.text_enc), segment_aad(tenant_id, row.session_id, row.seq)).decode(
        "utf-8"
    )


def segment_out(dek: bytes, tenant_id: UUID, row: SegmentRow) -> SegmentOut:
    return SegmentOut(
        seq=row.seq,
        speaker=row.speaker,
        t_start_ms=row.t_start_ms,
        t_end_ms=row.t_end_ms,
        text=decrypt_segment(dek, tenant_id, row),
        confidence=row.confidence,
    )


@router.get("/sessions/{id}/segments", response_model=list[SegmentOut])
async def list_segments(
    id: UUID,
    request: Request,
    after_seq: int = Query(default=-1, ge=-1),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require(*CLINICAL)),
) -> list[SegmentOut]:
    async with open_tx(request, principal) as (deps, s):
        row = await owned_session(s, id, principal)
        tenant = await load_tenant(s, principal.tenant_id)
        dek = session_dek(deps, tenant, row)  # a purged session is 410 even when no rows are left
        rows = await segments_repo.replay(s, row.id, after_seq, limit, started_at=row.started_at)
        return [segment_out(dek, tenant.id, r) for r in rows]


def _parse_before(value: str | None) -> tuple[datetime, int] | None:
    if value is None:
        return None
    try:
        stamp, _, ident = value.rpartition(",")
        return datetime.fromisoformat(stamp), int(ident)
    except ValueError:
        raise AppError("CW-4225", 422, "before 는 '<created_at ISO>,<segment_id>' 형식입니다") from None


@router.get("/patients/{id}/timeline", response_model=TimelineOut)
async def patient_timeline(
    id: UUID,
    request: Request,
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require(*CLINICAL)),
) -> TimelineOut:
    """Q1: the last six months of a patient's segments, newest first, across sessions (partition
    pruning + ``ix_segments_patient_time``); ``next_before`` continues the keyset."""
    async with open_tx(request, principal) as (deps, s):
        if await patients_repo.get_patient(s, id) is None:
            raise not_found("환자")
        tenant = await load_tenant(s, principal.tenant_id)
        rows = await segments_repo.timeline(
            s,
            tenant_id=tenant.id,
            patient_id=id,
            since=deps.clock.now() - TIMELINE_WINDOW,
            before=_parse_before(before),
            limit=limit,
        )
        items = await _decrypt_rows(deps, s, tenant, rows, principal)
    cursor = f"{rows[-1].created_at.isoformat()},{rows[-1].id}" if len(rows) == limit else None
    return TimelineOut(items=items, next_before=cursor)


async def _decrypt_rows(
    deps: AppDeps, s: AsyncSession, tenant: Tenant, rows: list[SegmentRow], principal: Principal
) -> list[TimelineSegmentOut]:
    deks: dict[UUID, bytes | None] = {}
    out: list[TimelineSegmentOut] = []
    for row in rows:
        if row.session_id not in deks:
            sess = await sessions_repo.get_session(s, row.session_id)
            visible = sess is not None and (
                principal.role != "clinician" or sess.clinician_id == principal.user_id
            )
            deks[row.session_id] = (
                None
                if not visible or sess is None or sess.dek_wrapped is None
                else session_dek(deps, tenant, sess)
            )
        dek = deks[row.session_id]
        if dek is None:  # not the clinician's own session, or already crypto-shredded
            continue
        out.append(
            TimelineSegmentOut(
                **segment_out(dek, tenant.id, row).model_dump(),
                session_id=row.session_id,
                segment_id=row.id,
                created_at=row.created_at,
            )
        )
    return out
