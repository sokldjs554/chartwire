"""``patients`` and versioned ``consents``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import Consent, Patient


async def create_patient(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    pseudonym: str,
    name_enc: bytes | None,
    name_hmac: bytes | None,
    birth_year: int | None,
    sex: str | None,
    phone_enc: bytes | None,
    dek_wrapped: bytes | None,
    dek_fingerprint: bytes | None,
) -> Patient:
    stmt = (
        insert(Patient)
        .values(
            tenant_id=tenant_id,
            pseudonym=pseudonym,
            name_enc=name_enc,
            name_hmac=name_hmac,
            birth_year=birth_year,
            sex=sex,
            phone_enc=phone_enc,
            dek_wrapped=dek_wrapped,
            dek_fingerprint=dek_fingerprint,
            is_synthetic=True,  # spec §0.1: always
        )
        .returning(Patient)
    )
    return (await session.scalars(stmt)).one()


async def get_patient(session: AsyncSession, patient_id: UUID) -> Patient | None:
    return await session.get(Patient, patient_id)


async def find_patients_by_name_hmac(
    session: AsyncSession, tenant_id: UUID, name_hmac: bytes
) -> Sequence[Patient]:
    """Q6: exact match on the blind index of an encrypted column."""
    stmt = (
        select(Patient)
        .where(Patient.tenant_id == tenant_id, Patient.name_hmac == name_hmac)
        .order_by(Patient.id)
    )
    return (await session.scalars(stmt)).all()


async def count_patients(session: AsyncSession, tenant_id: UUID) -> int:
    return (
        await session.execute(select(func.count()).select_from(Patient).where(Patient.tenant_id == tenant_id))
    ).scalar_one()


async def set_consent_state(session: AsyncSession, patient_id: UUID, state: str) -> None:
    await session.execute(update(Patient).where(Patient.id == patient_id).values(consent_state=state))


# ---------------------------------------------------------------- consents


async def grant_consent(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    scopes: list[str],
    granted_by: UUID | None,
    channel: str = "console",
    policy_hash: bytes | None = None,
) -> Consent:
    """New consent version = max(version)+1; ``UNIQUE (patient_id, version)`` serializes races."""
    next_version = (
        select(func.coalesce(func.max(Consent.version), 0) + 1)
        .where(Consent.patient_id == patient_id)
        .scalar_subquery()
    )
    stmt = (
        insert(Consent)
        .values(
            tenant_id=tenant_id,
            patient_id=patient_id,
            scopes=scopes,
            version=next_version,
            granted_by=granted_by,
            channel=channel,
            policy_hash=policy_hash,
        )
        .returning(Consent)
    )
    consent = (await session.scalars(stmt)).one()
    await set_consent_state(session, patient_id, "granted")
    return consent


async def latest_active_consent(session: AsyncSession, patient_id: UUID) -> Consent | None:
    stmt = (
        select(Consent)
        .where(Consent.patient_id == patient_id, Consent.revoked_at.is_(None))
        .order_by(Consent.version.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).one_or_none()


async def get_consent(session: AsyncSession, consent_id: UUID) -> Consent | None:
    return await session.get(Consent, consent_id)


async def list_consents(session: AsyncSession, patient_id: UUID) -> Sequence[Consent]:
    stmt = select(Consent).where(Consent.patient_id == patient_id).order_by(Consent.version.desc())
    return (await session.scalars(stmt)).all()


async def revoke_consent(
    session: AsyncSession, consent_id: UUID, *, revoked_by: UUID | None, reason: str | None, now: datetime
) -> Consent | None:
    stmt = (
        update(Consent)
        .where(Consent.id == consent_id, Consent.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=revoked_by, revoked_reason=reason)
        .returning(Consent)
    )
    consent = (await session.scalars(stmt)).one_or_none()
    if consent is not None:
        await set_consent_state(session, consent.patient_id, "revoked")
    return consent
