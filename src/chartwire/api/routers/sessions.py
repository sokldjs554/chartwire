"""Sessions (§6.9): create (consent gate ``recording`` → CW-4031; session DEK wrapped with the tenant
KEK; ``scopes_snapshot``), keyset list, get, one-time WS ticket, REST ``end``.

"clinician (own)" is a row rule on ``sessions.clinician_id``: a clinician only lists and reads their
own sessions; staff (and admin for the list) see the tenant. RLS already limits everything to the tenant.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import AppDeps, actor_user_id, load_tenant, not_found, open_tx, request_id
from chartwire.api.schemas import SessionCreate, SessionList, SessionOut, WsTicketOut, WsTicketRequest
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.consent import service as consent_service
from chartwire.consent.scopes import RECORDING, SCOPE_ORDER
from chartwire.core.errors import AppError, Conflict, Forbidden
from chartwire.core.ids import uuid7
from chartwire.crypto.envelope import Envelope, dek_fingerprint
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.redis import keys, tickets
from chartwire.redis.session_state import SessionState

router = APIRouter(prefix="/v1", tags=["sessions"])
CLINICAL = ("clinician", "staff")
LIVE_STATES = ("recording", "paused", "created")


def session_out(row: SessionModel) -> SessionOut:
    return SessionOut(
        id=row.id,
        state=row.state,
        patient_id=row.patient_id,
        clinician_id=row.clinician_id,
        script_ref=row.script_ref,
        started_at=row.started_at,
        ended_at=row.ended_at,
        ack_seq=int(row.ack_seq),
        final_seq=None if row.final_seq is None else int(row.final_seq),
        epoch=int(row.epoch),
        scopes_snapshot=list(row.scopes_snapshot),
        created_at=row.created_at,
    )


def check_owner(row: SessionModel, principal: Principal) -> None:
    if principal.role == "clinician" and row.clinician_id != principal.user_id:
        raise Forbidden("본인이 진행한 세션만 볼 수 있습니다")


async def owned_session(s: AsyncSession, session_id: UUID, principal: Principal) -> SessionModel:
    row = await sessions_repo.get_session(s, session_id)
    if row is None:
        raise not_found("세션")
    check_owner(row, principal)
    return row


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate, request: Request, principal: Principal = Depends(require(*CLINICAL))
) -> SessionOut:
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        patient = await patients_repo.get_patient(s, body.patient_id)
        if patient is None:
            raise not_found("환자")
        if patient.consent_state == "purged":
            raise Conflict("파기된 환자입니다", "CW-4096")
        scopes = await consent_service.require_scope_for_patient(s, patient.id, RECORDING)  # CW-4031
        clinician_id = body.clinician_id if body.clinician_id is not None else actor_user_id(principal)
        clinician = await tenancy_repo.get_user(s, clinician_id)
        if clinician is None or clinician.role != "clinician":
            raise AppError("CW-4224", 422, "clinician_id 는 이 테넌트의 임상의여야 합니다")
        dek = Envelope.new_dek()
        wrapped = deps.kek.wrap(dek, tenant.kek_ref)
        row = await sessions_repo.create_session(
            s,
            id=uuid7(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            clinician_id=clinician.id,
            script_ref=body.script_ref,
            scopes_snapshot=[sc for sc in SCOPE_ORDER if sc in scopes],
            dek_wrapped=wrapped,
            dek_fingerprint=dek_fingerprint(wrapped),
            chunk_ms=deps.settings.chunk_ms,
        )
        await audit.record(
            s,
            tenant_id=tenant.id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="session.created",
            resource_type="session",
            resource_id=row.id,
            request_id=request_id(request),
            detail={"patient_id": str(patient.id), "clinician_id": str(clinician.id), "scopes": row.scopes_snapshot},
        )
        return session_out(row)


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    request: Request,
    state: str | None = Query(default=None, max_length=16),
    clinician_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = None,
    principal: Principal = Depends(require("clinician", "staff", "admin")),
) -> SessionList:
    """Keyset list newest first; ``before`` is the ``created_at`` of the last row of the previous page."""
    if principal.role == "clinician":
        clinician_id = actor_user_id(principal)
    async with open_tx(request, principal) as (_deps, s):
        rows = await sessions_repo.list_sessions(
            s, tenant_id=principal.tenant_id, state=state, clinician_id=clinician_id, before=before, limit=limit
        )
    items = [session_out(r) for r in rows]
    return SessionList(items=items, next_before=items[-1].created_at if len(items) == limit else None)


@router.get("/sessions/{id}", response_model=SessionOut)
async def get_session(id: UUID, request: Request, principal: Principal = Depends(require(*CLINICAL))) -> SessionOut:
    async with open_tx(request, principal) as (_deps, s):
        return session_out(await owned_session(s, id, principal))


@router.post("/sessions/{id}/ws-ticket", response_model=WsTicketOut)
async def ws_ticket(
    id: UUID,
    body: WsTicketRequest,
    request: Request,
    principal: Principal = Depends(require("clinician", "staff", "recorder")),
) -> WsTicketOut:
    """clinician: ingest/watch on own sessions · staff: watch · recorder: ingest (§6.9). 30/min."""
    async with open_tx(request, principal) as (deps, s):
        row = await sessions_repo.get_session(s, id)
        if row is None:
            raise not_found("세션")
        check_owner(row, principal)
        if (principal.role == "staff" and body.kind != "watch") or (
            principal.role == "recorder" and body.kind != "ingest"
        ):
            raise Forbidden(f"역할 '{principal.role}'은(는) {body.kind} 티켓을 받을 수 없습니다")
        if body.kind == "ingest" and row.state not in LIVE_STATES:
            raise Conflict(f"상태 '{row.state}' 세션에는 녹음을 이어갈 수 없습니다", "CW-4099")
    token = await tickets.issue(
        deps.redis,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id or principal.sub,
        role=principal.role,
        session_id=id,
        kind=body.kind,
    )
    return WsTicketOut(ticket=token, expires_in=keys.TTL_TICKET)


@router.post("/sessions/{id}/end", response_model=SessionOut)
async def end_session(id: UUID, request: Request, principal: Principal = Depends(require(*CLINICAL))) -> SessionOut:
    """REST alternative to the WS ``end``: the ledger ``ack_seq`` becomes ``final_seq``; the end marker
    goes on the chunk stream so the stt-worker flushes and marks the session transcribed (§7.4)."""
    async with open_tx(request, principal) as (deps, s):
        row = await owned_session(s, id, principal)
        if row.state not in ("recording", "paused"):
            raise Conflict(f"상태 '{row.state}' 세션은 종료할 수 없습니다", "CW-4099")
        now = deps.clock.now()
        ended = await sessions_repo.update_session(
            s, row.id, state="ended", ended_at=now, final_seq=int(row.ack_seq)
        )
        assert ended is not None
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="session.ended",
            resource_type="session",
            resource_id=row.id,
            request_id=request_id(request),
            detail={"final_seq": int(row.ack_seq), "via": "rest"},
        )
    await _announce_end(deps, ended)
    return session_out(ended)


async def _announce_end(deps: AppDeps, row: SessionModel) -> None:
    """Redis side of ending (idempotent, best effort): end marker, hash state, viewers, 24 h TTLs."""
    state = SessionState(deps.redis, stream_maxlen=deps.settings.stream_maxlen, scripts_dir=deps.settings.scripts_dir)
    with contextlib.suppress(RedisError, OSError):
        await state.xadd_end(row.id, int(row.epoch))
        await state.set_fields(row.id, state="ended", updated_at=deps.clock.now().isoformat())
        await state.publish_event(row.id, {"t": "session.state", "state": "ended"})
        await state.expire_after_end(row.id)
