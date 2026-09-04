"""Consent lifecycle over the database (spec §8.3): grant a new version, revoke, read live scopes.

``revoke`` is the one transaction §6.9 describes for ``POST /consents/{id}/revoke``: the consent row
is stamped, ``patients.consent_state`` flips, the patient-level purge job is created, the
``consent.revoked`` outbox event is queued and both audit rows are written — all or nothing. The
Redis side effects (``ctl`` → live sessions close with 4011, ``outbox:wake``) run after commit through
:func:`notify_revoked`; they are idempotent and harmless if the commit had failed.

Gates elsewhere (session create, WS hello, stt-worker, notes) call :func:`active_scopes_for_patient`
and the pure functions in :mod:`chartwire.consent.gates`; nothing caches consent across requests.
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import orjson
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.audit import service as audit
from chartwire.consent import gates
from chartwire.core.errors import Conflict, NotFound
from chartwire.core.ids import uuid7
from chartwire.db.models import Consent
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import purge as purge_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx
from chartwire.outbox import writer
from chartwire.outbox.registry import CONSENT_REVOKED
from chartwire.redis import keys

LIVE_STATES = ("recording", "paused")
CTL_CONSENT_REVOKED: dict[str, str] = {"t": "consent_revoked"}


@dataclass(frozen=True, slots=True)
class Revoked:
    consent: Consent
    purge_job_id: UUID
    outbox_event_id: int | None
    live_session_ids: list[UUID]


async def active_scopes_for_patient(session: AsyncSession, patient_id: UUID) -> set[str]:
    """Scopes of the latest non-revoked consent version (§8.3), read fresh from the database."""
    return gates.active_scopes(await patients_repo.list_consents(session, patient_id))


async def require_scope_for_patient(session: AsyncSession, patient_id: UUID, scope: str) -> set[str]:
    """Fail-closed gate: raises :class:`gates.ConsentScopeMissing` (REST ``CW-4031`` / WS ``4011``)."""
    scopes = await active_scopes_for_patient(session, patient_id)
    gates.require_scope(scopes, scope)
    return scopes


def policy_hash(scopes: list[str], channel: str) -> bytes:
    """What the patient agreed to, hashed: the scope set and the channel (console, paper, …)."""
    return hashlib.sha256(orjson.dumps({"scopes": sorted(scopes), "channel": channel})).digest()


async def grant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    scopes: list[str],
    actor: TenantCtx,
    channel: str = "console",
    request_id: str | None = None,
) -> Consent:
    patient = await patients_repo.get_patient(session, patient_id)
    if patient is None:
        raise NotFound("환자")
    if patient.consent_state == "purged":
        raise Conflict("파기된 환자에게는 동의를 기록할 수 없습니다", "CW-4096")
    unknown = set(scopes) - gates.SCOPES
    if unknown or not scopes:
        raise ValueError(f"invalid consent scopes: {sorted(unknown) or 'empty'}")
    consent = await patients_repo.grant_consent(
        session,
        tenant_id=tenant_id,
        patient_id=patient_id,
        scopes=list(scopes),
        granted_by=actor.user_id,
        channel=channel,
        policy_hash=policy_hash(scopes, channel),
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="consent.granted",
        resource_type="consent",
        resource_id=consent.id,
        request_id=request_id,
        detail={"patient_id": str(patient_id), "version": consent.version, "scopes": list(consent.scopes)},
    )
    return consent


async def revoke(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    consent_id: UUID,
    actor: TenantCtx,
    reason: str | None,
    now: datetime,
    request_id: str | None = None,
) -> Revoked:
    """Revoke + ``patients.consent_state`` + patient-level purge job + outbox ``consent.revoked`` + audit,
    in the caller's transaction. Re-revoking is a 409: the first revocation already queued the purge."""
    current = await patients_repo.get_consent(session, consent_id)
    if current is None:
        raise NotFound("동의")
    if current.revoked_at is not None:
        raise Conflict("이미 철회된 동의입니다", "CW-4097")
    consent = await patients_repo.revoke_consent(
        session, consent_id, revoked_by=actor.user_id, reason=reason, now=now
    )
    if consent is None:  # lost a race with a concurrent revoke
        raise Conflict("이미 철회된 동의입니다", "CW-4097")
    live: list[UUID] = []
    for state in LIVE_STATES:
        rows = await sessions_repo.list_sessions(
            session, tenant_id=tenant_id, patient_id=consent.patient_id, state=state, limit=1000
        )
        live.extend(row.id for row in rows)
    job = await purge_repo.create_job(
        session,
        id=uuid7(),
        tenant_id=tenant_id,
        subject_type="patient",
        subject_id=consent.patient_id,
        reason="consent_revoked",
        requested_by=actor.user_id,
    )
    event_id = await writer.emit(
        session,
        tenant_id=tenant_id,
        aggregate_type="consent",
        aggregate_id=consent.id,
        event_type=CONSENT_REVOKED,
        payload={"patient_id": str(consent.patient_id), "consent_id": str(consent.id), "purge_job_id": str(job.id)},
        idempotency_key=writer.idempotency_key(CONSENT_REVOKED, consent.id, consent.version),
    )
    detail: dict[str, Any] = {
        "patient_id": str(consent.patient_id),
        "version": consent.version,
        "purge_job_id": str(job.id),
        "live_sessions": len(live),
        "reason_given": reason is not None,
    }
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="consent.revoked",
        resource_type="consent",
        resource_id=consent.id,
        request_id=request_id,
        detail=detail,
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="purge.requested",
        resource_type="purge_job",
        resource_id=job.id,
        request_id=request_id,
        detail={"subject_type": "patient", "subject_id": str(consent.patient_id), "reason": "consent_revoked"},
    )
    return Revoked(consent=consent, purge_job_id=job.id, outbox_event_id=event_id, live_session_ids=live)


async def notify_revoked(redis: Any, revoked: Revoked) -> int:
    """After commit: ``ctl:{sid}`` ``{"t":"consent_revoked"}`` to every live session of the patient
    (ingest closes 4011, viewers 4011) and ``outbox:wake`` for the purge handler. Returns sessions told."""
    told = 0
    payload = orjson.dumps(CTL_CONSENT_REVOKED).decode()
    for sid in revoked.live_session_ids:
        with contextlib.suppress(RedisError, OSError):
            await redis.publish(keys.ctl(sid), payload)
            told += 1
    with contextlib.suppress(RedisError, OSError):
        await redis.publish(keys.OUTBOX_WAKE, "1")
    return told
