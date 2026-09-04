"""Consents (§6.9): grant a new version, list, revoke (→ purge). The domain logic is in
:mod:`chartwire.consent.service`; this module only maps HTTP to it."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from chartwire.api.deps import not_found, open_tx, principal_ctx, request_id
from chartwire.api.schemas import ConsentGrant, ConsentOut, ConsentRevoke, ConsentRevokedOut
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.consent import service
from chartwire.db.models import Consent
from chartwire.db.repo import patients as patients_repo

router = APIRouter(prefix="/v1", tags=["consents"])
CLINICAL = ("clinician", "staff")


def consent_out(row: Consent) -> ConsentOut:
    return ConsentOut(
        id=row.id,
        patient_id=row.patient_id,
        version=row.version,
        scopes=list(row.scopes),
        granted_at=row.granted_at,
        granted_by=row.granted_by,
        channel=row.channel,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoked_reason=row.revoked_reason,
    )


@router.post("/patients/{id}/consents", response_model=ConsentOut, status_code=status.HTTP_201_CREATED)
async def grant_consent(
    id: UUID, body: ConsentGrant, request: Request, principal: Principal = Depends(require(*CLINICAL))
) -> ConsentOut:
    async with open_tx(request, principal) as (_deps, s):
        consent = await service.grant(
            s,
            tenant_id=principal.tenant_id,
            patient_id=id,
            scopes=list(body.scopes),
            actor=principal_ctx(principal),
            channel=body.channel,
            request_id=request_id(request),
        )
        return consent_out(consent)


@router.get("/patients/{id}/consents", response_model=list[ConsentOut])
async def list_consents(
    id: UUID, request: Request, principal: Principal = Depends(require(*CLINICAL))
) -> list[ConsentOut]:
    async with open_tx(request, principal) as (_deps, s):
        if await patients_repo.get_patient(s, id) is None:
            raise not_found("환자")
        return [consent_out(c) for c in await patients_repo.list_consents(s, id)]


@router.post("/consents/{id}/revoke", response_model=ConsentRevokedOut, status_code=status.HTTP_202_ACCEPTED)
async def revoke_consent(
    id: UUID,
    request: Request,
    body: ConsentRevoke | None = None,
    principal: Principal = Depends(require("clinician", "staff", "admin")),
) -> ConsentRevokedOut:
    """Same transaction: consent revoked, ``patients.consent_state``, patient-level purge job, outbox
    ``consent.revoked``, audit. After commit: ``ctl`` → live sessions (4011), ``outbox:wake``."""
    async with open_tx(request, principal) as (deps, s):
        revoked = await service.revoke(
            s,
            tenant_id=principal.tenant_id,
            consent_id=id,
            actor=principal_ctx(principal),
            reason=body.reason if body else None,
            now=deps.clock.now(),
            request_id=request_id(request),
        )
    told = await service.notify_revoked(deps.redis, revoked)
    assert revoked.consent.revoked_at is not None
    return ConsentRevokedOut(
        consent_id=revoked.consent.id,
        patient_id=revoked.consent.patient_id,
        revoked_at=revoked.consent.revoked_at,
        purge_job_id=revoked.purge_job_id,
        outbox_event_id=revoked.outbox_event_id,
        live_sessions_notified=told,
    )
