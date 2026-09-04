"""``purge_verify`` (spec §8.4 step 7) and the ``verify-decrypt`` demonstration.

For every session of the subject: row counts in every purged table are 0, the object store prefix is
empty, ``SCAN sess:{sid}*`` finds nothing, ``sessions.dek_wrapped IS NULL``, an unwrap attempt raises
:class:`DekDestroyedError`, and decrypting the kept ``sample_ciphertext`` with the tenant key raises
:class:`DecryptError`. Patient-level jobs also check the patient's identifiers and DEK.

Verification writes its verdict in its **own** transaction (not the poller's): a ``failed`` verdict
must survive the handler raising — raising is what sends the event through retry and, after
``max_attempts``, into the dead-letter queue where an operator sees it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.audit import service as audit
from chartwire.crypto.envelope import Envelope
from chartwire.crypto.errors import DecryptError, DekDestroyedError
from chartwire.db.models import PurgeJob, Tenant
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import purge as purge_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.base import session_prefix
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys
from chartwire.ws.watch import segment_aad

log = logging.getLogger(__name__)

UNWRAP_DESTROYED = "failed:dek_destroyed"
UNWRAP_SUCCEEDED = "succeeded"
DECRYPT_INVALID = "failed:invalid_tag"
DECRYPT_NO_SAMPLE = "skipped:no_sample"
DECRYPT_SUCCEEDED = "succeeded"


class PurgeVerifyFailed(RuntimeError):
    def __init__(self, job_id: UUID, failed: list[str]) -> None:
        super().__init__(f"purge verification failed for {job_id}: {', '.join(failed)}")
        self.job_id = job_id
        self.failed = failed


class PurgeNotCompleted(RuntimeError):
    """The job is not in a verifiable state yet (retryable through the outbox)."""


@dataclass
class Report:
    checks: dict[str, bool] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, **info: Any) -> None:
        self.checks[name] = bool(ok)
        if info:
            self.detail[name] = info

    @property
    def ok(self) -> bool:
        return all(self.checks.values())

    @property
    def failed(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks, "detail": self.detail, "failed": self.failed}


def _key_for_attempt(kek: Any, keycache: Any, tenant: Tenant) -> bytes:
    """The demo decrypts with the tenant KEK (``LocalKek.derive``); a provider without a derivable
    KEK falls back to the tenant record key — neither is the session DEK, so the tag cannot verify."""
    derive = getattr(kek, "derive", None)
    if callable(derive):
        return bytes(derive(tenant.kek_ref))
    return bytes(keycache.get(tenant.id, tenant.kek_ref, tenant.record_key_wrapped))


def attempt_decrypt(
    kek: Any,
    keycache: Any,
    *,
    tenant: Tenant,
    session_id: UUID,
    dek_wrapped: bytes | None,
    sample: bytes | None,
) -> dict[str, str]:
    """``{unwrap, decrypt_sample}`` — the two failures the console's "복호화 시도" button shows."""
    try:
        keycache.get(session_id, tenant.kek_ref, dek_wrapped)
        unwrap = UNWRAP_SUCCEEDED
    except DekDestroyedError:
        unwrap = UNWRAP_DESTROYED
    except DecryptError as exc:
        unwrap = f"failed:{exc.reason}"
    if sample is None:
        return {"unwrap": unwrap, "decrypt_sample": DECRYPT_NO_SAMPLE}
    try:
        Envelope.decrypt(
            _key_for_attempt(kek, keycache, tenant), bytes(sample), segment_aad(tenant.id, session_id, 0)
        )
        decrypt = DECRYPT_SUCCEEDED
    except DecryptError as exc:
        decrypt = f"failed:{exc.reason}"
    return {"unwrap": unwrap, "decrypt_sample": decrypt}


async def _verify_session(
    ctx: HandlerContext, s: AsyncSession, tenant: Tenant, sid: UUID, sample: bytes | None, report: Report
) -> None:
    counts = await purge_repo.count_session_data(s, sid)
    report.add(f"rows:{sid}", all(v == 0 for v in counts.values()), counts=counts)
    objects = await ctx.objectstore.list(session_prefix(tenant.id, sid))
    report.add(f"objectstore:{sid}", not objects, objects=len(objects))
    redis_keys = [k async for k in ctx.redis.scan_iter(match=keys.sess_pattern(sid), count=200)]
    report.add(f"redis:{sid}", not redis_keys, keys=len(redis_keys))
    sess = await sessions_repo.get_session(s, sid)
    wrapped = None if sess is None else sess.dek_wrapped
    report.add(f"dek_null:{sid}", sess is not None and wrapped is None and sess.state == "purged")
    attempt = attempt_decrypt(
        ctx.kek, ctx.keycache, tenant=tenant, session_id=sid, dek_wrapped=wrapped, sample=sample
    )
    report.add(f"unwrap:{sid}", attempt["unwrap"] == UNWRAP_DESTROYED, result=attempt["unwrap"])
    report.add(
        f"decrypt_sample:{sid}",
        attempt["decrypt_sample"] in (DECRYPT_INVALID, DECRYPT_NO_SAMPLE)
        or attempt["decrypt_sample"].startswith("failed:"),
        result=attempt["decrypt_sample"],
    )


async def run(ctx: HandlerContext, purge_job_id: UUID, *, tenant_id: UUID) -> PurgeJob:
    """Verify ``purge_job_id`` → state ``verified`` or ``failed`` (+ raise :class:`PurgeVerifyFailed`)."""
    async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as s:
        job = await purge_repo.get_job(s, purge_job_id)
        if job is None:
            raise PurgeNotCompleted(f"purge job {purge_job_id} not found")
        if job.state == "verified":
            return job
        if job.state not in ("completed", "failed"):
            raise PurgeNotCompleted(f"purge job {purge_job_id} is {job.state}")
        tenant = await s.get(Tenant, tenant_id)
        assert tenant is not None
        report = Report()
        sample = None if job.sample_ciphertext is None else bytes(job.sample_ciphertext)
        if job.subject_type == "session":
            session_ids = [job.subject_id]
        else:
            session_ids = await purge_repo.sessions_of_patient(s, job.subject_id)
            patient = await patients_repo.get_patient(s, job.subject_id)
            report.add(
                "patient_shredded",
                patient is not None
                and patient.dek_wrapped is None
                and patient.name_enc is None
                and patient.phone_enc is None
                # the blind index is keyed from the KEK master, not the DEK, so it survives a
                # crypto-shred unless the purge nulls it; a receipt must not report `verified`
                # while `GET /v1/patients?name=` can still confirm the name
                and patient.name_hmac is None
                and patient.consent_state == "purged",
            )
        for sid in session_ids:
            await _verify_session(ctx, s, tenant, sid, sample, report)
        now = ctx.clock.now()
        verified = await purge_repo.update_job(
            s,
            job.id,
            state="verified" if report.ok else "failed",
            verified_at=now,
            verify_result=report.as_json(),
        )
        assert verified is not None
        await audit.record(
            s,
            tenant_id=tenant_id,
            actor_id=None,
            actor_role="service",
            action="purge.verified",
            resource_type="purge_job",
            resource_id=job.id,
            detail={"ok": report.ok, "failed": report.failed, "sessions": len(session_ids)},
        )
    if not report.ok:
        log.error("purge verification failed", extra={"purge_job_id": str(job.id), "failed": report.failed})
        raise PurgeVerifyFailed(job.id, report.failed)
    return verified
