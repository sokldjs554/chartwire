"""``consent.revoked`` and ``purge.requested`` → :func:`chartwire.purge.pipeline.run` (spec §7.2, §8.4).

Importing this module registers both handlers; ``worker/main.py`` imports it by name.

* ``purge.requested {purge_job_id}`` — an admin asked for one subject (session or patient).
* ``consent.revoked {patient_id, consent_id, purge_job_id?}`` — the revoke transaction already
  created the patient-level job and put its id in the payload; a payload without one (another
  emitter, an older row) gets a job created here, inside the handler transaction.

Both run the pipeline under ``ctx.tenant_tx``, which under the poller *joins* the handler
transaction: every DELETE, the DEK tombstone, the receipt and ``processed_events`` commit together.
A job that is already ``completed``/``verified`` is returned untouched, so redelivery is safe.
"""

from __future__ import annotations

import logging
from uuid import UUID

from chartwire.db.repo import patients as patients_repo
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.registry import CONSENT_REVOKED, PURGE_REQUESTED, handler
from chartwire.purge import pipeline

log = logging.getLogger(__name__)

LEASE_S = 300
"""A patient with many sessions deletes rows in every partition and the object store; 5 minutes."""


def _job_id(event: OutboxEvent) -> UUID | None:
    raw = event.payload.get("purge_job_id")
    return UUID(str(raw)) if raw else None


@handler(PURGE_REQUESTED, lease_s=LEASE_S)
async def purge_requested(ctx: HandlerContext, event: OutboxEvent) -> None:
    job_id = _job_id(event) or event.aggregate_id
    job = await pipeline.run(ctx, job_id, tenant_id=event.tenant_id)
    log.info(
        "purge_run handled",
        extra={"event_id": event.id, "purge_job_id": str(job.id), "subject_type": job.subject_type},
    )


@handler(CONSENT_REVOKED, lease_s=LEASE_S)
async def consent_revoked(ctx: HandlerContext, event: OutboxEvent) -> None:
    job_id = _job_id(event)
    patient_id = UUID(str(event.payload["patient_id"]))
    if job_id is None:
        async with ctx.tenant_tx(event.tenant_id) as session:
            if await patients_repo.get_patient(session, patient_id) is None:
                log.warning(
                    "consent.revoked for unknown patient; nothing to purge", extra={"event_id": event.id}
                )
                return
            job = await pipeline.create_job(
                session,
                tenant_id=event.tenant_id,
                subject_type="patient",
                subject_id=patient_id,
                reason="consent_revoked",
                requested_by=None,
            )
            job_id = job.id
    job = await pipeline.run(ctx, job_id, tenant_id=event.tenant_id)
    log.info(
        "consent revocation purged",
        extra={"event_id": event.id, "purge_job_id": str(job.id), "patient_id": str(patient_id)},
    )
