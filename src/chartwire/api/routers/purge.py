"""Purge jobs (§6.9, §8.4): request (admin → outbox ``purge.requested``), receipt, ``verify-decrypt``.

``verify-decrypt`` is the console's "복호화 시도" button: it really tries to unwrap the session DEK and
to decrypt the ciphertext sample kept in the job, and reports both failures — it does not read the
job's stored verdict.
"""

from __future__ import annotations

import contextlib
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from redis.exceptions import RedisError

from chartwire.api.deps import load_tenant, not_found, open_tx, request_id
from chartwire.api.schemas import PurgeJobCreate, PurgeReceiptOut, VerifyDecryptOut
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import purge as purge_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.outbox import writer
from chartwire.outbox.registry import PURGE_REQUESTED
from chartwire.purge import pipeline, receipt, verify
from chartwire.redis import keys

router = APIRouter(prefix="/v1", tags=["purge"])
READERS = ("admin", "auditor")


@router.post("/purge-jobs", response_model=PurgeReceiptOut, status_code=status.HTTP_202_ACCEPTED)
async def create_purge_job(
    body: PurgeJobCreate, request: Request, principal: Principal = Depends(require("admin"))
) -> PurgeReceiptOut:
    async with open_tx(request, principal) as (deps, s):
        if body.subject_type == "session":
            exists = await sessions_repo.get_session(s, body.subject_id) is not None
        else:
            exists = await patients_repo.get_patient(s, body.subject_id) is not None
        if not exists:
            raise not_found("세션" if body.subject_type == "session" else "환자")
        job = await pipeline.create_job(
            s,
            tenant_id=principal.tenant_id,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            reason="admin",
            requested_by=principal.user_id,
        )
        await writer.emit(
            s,
            tenant_id=principal.tenant_id,
            aggregate_type="purge_job",
            aggregate_id=job.id,
            event_type=PURGE_REQUESTED,
            payload={"purge_job_id": str(job.id)},
            idempotency_key=writer.idempotency_key(PURGE_REQUESTED, job.id, 1),
        )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="purge.requested",
            resource_type="purge_job",
            resource_id=job.id,
            request_id=request_id(request),
            detail={"subject_type": body.subject_type, "subject_id": str(body.subject_id), "reason": "admin"},
        )
    with contextlib.suppress(RedisError, OSError):
        await deps.redis.publish(keys.OUTBOX_WAKE, "1")
    return PurgeReceiptOut(**receipt.build(job))


@router.get("/purge-jobs/{id}", response_model=PurgeReceiptOut)
async def get_purge_job(id: UUID, request: Request, principal: Principal = Depends(require(*READERS))) -> PurgeReceiptOut:
    async with open_tx(request, principal) as (_deps, s):
        job = await purge_repo.get_job(s, id)
        if job is None:
            raise not_found("파기 작업")
        return PurgeReceiptOut(**receipt.build(job))


@router.post("/purge-jobs/{id}/verify-decrypt", response_model=VerifyDecryptOut)
async def verify_decrypt(
    id: UUID, request: Request, principal: Principal = Depends(require(*READERS))
) -> VerifyDecryptOut:
    async with open_tx(request, principal) as (deps, s):
        job = await purge_repo.get_job(s, id)
        if job is None:
            raise not_found("파기 작업")
        tenant = await load_tenant(s, principal.tenant_id)
        if job.subject_type == "session":
            session_ids = [job.subject_id]
        else:
            session_ids = await purge_repo.sessions_of_patient(s, job.subject_id)
        sample = None if job.sample_ciphertext is None else bytes(job.sample_ciphertext)
        result = {"unwrap": verify.UNWRAP_DESTROYED, "decrypt_sample": verify.DECRYPT_NO_SAMPLE}
        for sid in session_ids:
            sess = await sessions_repo.get_session(s, sid)
            wrapped = None if sess is None else sess.dek_wrapped
            result = verify.attempt_decrypt(deps.kek, deps.keycache, tenant=tenant, session_id=sid, dek_wrapped=wrapped, sample=sample)
            if result["unwrap"] != verify.UNWRAP_DESTROYED:
                break  # report the first session whose DEK is still alive
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="purge.verify_decrypt",
            resource_type="purge_job",
            resource_id=job.id,
            request_id=request_id(request),
            detail={"unwrap": result["unwrap"], "decrypt_sample": result["decrypt_sample"], "sessions": len(session_ids)},
        )
        return VerifyDecryptOut(decrypt_attempted=True, job_state=job.state, **result)
