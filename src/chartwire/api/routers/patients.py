"""Patients (§6.9): create with a per-patient DEK + blind index, exact blind-index lookup (Q6), get.

``name_enc``/``phone_enc`` are AES-GCM under the patient DEK with AAD ``tenant:patient:<pseudonym>:field``
(the pseudonym is the immutable per-tenant unique handle the row is created with). The name is only
decrypted for clinician/staff — the two roles allowed on these routes — and is ``None`` once the
patient's DEK has been crypto-shredded.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.exc import IntegrityError

from chartwire.api.deps import AppDeps, load_tenant, not_found, open_tx, request_id
from chartwire.api.schemas import PatientCreate, PatientOut
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.core.errors import Conflict
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, aad, dek_fingerprint
from chartwire.crypto.errors import CryptoError
from chartwire.db.models import Patient, Tenant
from chartwire.db.repo import patients as patients_repo
from chartwire.redis import keys as _keys  # noqa: F401  (key names never spelled here)

router = APIRouter(prefix="/v1", tags=["patients"])
CLINICAL = ("clinician", "staff")
PSEUDONYM_PREFIX = "가상환자-"


def _aad(tenant: Tenant, pseudonym: str, field: str) -> str:
    return aad(tenant.id, "patient", pseudonym, field)


def patient_out(deps: AppDeps, tenant: Tenant, patient: Patient) -> PatientOut:
    name: str | None = None
    if patient.name_enc is not None and patient.dek_wrapped is not None:
        try:
            dek = deps.keycache.get(patient.id, tenant.kek_ref, bytes(patient.dek_wrapped))
            name = Envelope.decrypt(dek, bytes(patient.name_enc), _aad(tenant, patient.pseudonym, "name")).decode()
        except (CryptoError, UnicodeDecodeError):
            name = None  # placeholder fixtures / foreign key material: the row is still listable
    return PatientOut(
        id=patient.id,
        pseudonym=patient.pseudonym,
        name=name,
        birth_year=patient.birth_year,
        sex=patient.sex,
        consent_state=patient.consent_state,
        is_synthetic=patient.is_synthetic,
        created_at=patient.created_at,
    )


@router.post("/patients", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: PatientCreate, request: Request, principal: Principal = Depends(require(*CLINICAL))
) -> PatientOut:
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        pseudonym = f"{PSEUDONYM_PREFIX}{await patients_repo.count_patients(s, tenant.id) + 1:04d}"
        dek = Envelope.new_dek()
        wrapped = deps.kek.wrap(dek, tenant.kek_ref)
        name_enc = Envelope.encrypt(dek, body.name.encode("utf-8"), _aad(tenant, pseudonym, "name"))
        phone_enc = (
            Envelope.encrypt(dek, body.phone.encode("utf-8"), _aad(tenant, pseudonym, "phone"))
            if body.phone
            else None
        )
        try:
            patient = await patients_repo.create_patient(
                s,
                tenant_id=tenant.id,
                pseudonym=pseudonym,
                name_enc=name_enc,
                name_hmac=blind_index(deps.settings.kek_master_bytes, tenant.id, body.name),
                birth_year=body.birth_year,
                sex=body.sex,
                phone_enc=phone_enc,
                dek_wrapped=wrapped,
                dek_fingerprint=dek_fingerprint(wrapped),
            )
        except IntegrityError as exc:  # two creates raced on the same pseudonym number
            raise Conflict("환자 번호 충돌 — 다시 시도하세요", "CW-4098") from exc
        await audit.record(
            s,
            tenant_id=tenant.id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="patient.created",
            resource_type="patient",
            resource_id=patient.id,
            request_id=request_id(request),
            detail={"pseudonym": patient.pseudonym, "has_phone": phone_enc is not None},
        )
        return patient_out(deps, tenant, patient)


@router.get("/patients", response_model=list[PatientOut])
async def find_patients(
    request: Request,
    name: str = Query(min_length=1, max_length=120),
    principal: Principal = Depends(require(*CLINICAL)),
) -> list[PatientOut]:
    """Exact-match lookup on the encrypted name through its blind index (Q6) — no substring search."""
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        digest = blind_index(deps.settings.kek_master_bytes, tenant.id, name)
        rows = await patients_repo.find_patients_by_name_hmac(s, tenant.id, digest)
        return [patient_out(deps, tenant, p) for p in rows]


@router.get("/patients/{id}", response_model=PatientOut)
async def get_patient(id: str, request: Request, principal: Principal = Depends(require(*CLINICAL))) -> PatientOut:
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        patient = await patients_repo.get_patient(s, _uuid(id))
        if patient is None:
            raise not_found("환자")
        return patient_out(deps, tenant, patient)


def _uuid(value: str):  # type: ignore[no-untyped-def]
    from uuid import UUID

    try:
        return UUID(value)
    except ValueError:
        raise not_found("환자") from None
