"""``purge_run`` (spec §8.4 steps 1–6): crypto-shred + hard delete for one session or one patient.

One purge is one transaction: under the outbox poller ``ctx.tenant_tx`` *joins* the handler
transaction, so the ``purge_jobs`` row, every DELETE, the DEK tombstone, the audit rows and the
``purge.completed`` event commit together with ``processed_events`` — a crash anywhere before that
commit leaves the database exactly as it was and the redelivery starts over. The non-database
effects (Redis keys, object store, DEK cache eviction) are idempotent by construction, so running
them twice is harmless. A job already ``completed``/``verified`` is returned untouched.

Every step is appended to ``purge_jobs.steps`` with its counts (the receipt), audited as
``purge.step`` with ids/counts only, and summed into ``purge_jobs.counts``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from redis.exceptions import RedisError

from chartwire.audit import service as audit
from chartwire.core.ids import uuid7
from chartwire.crypto.envelope import dek_fingerprint
from chartwire.db.models import PurgeJob
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import purge as purge_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.objectstore.base import session_prefix
from chartwire.ops import metrics
from chartwire.outbox import writer
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.registry import PURGE_COMPLETED
from chartwire.purge import receipt
from chartwire.redis import keys

log = logging.getLogger(__name__)

STEP_CAPTURE: Final = "capture"
STEP_REDIS: Final = "redis"
STEP_OBJECTSTORE: Final = "objectstore"
STEP_ROWS: Final = "rows"
STEP_SHRED: Final = "crypto_shred"
STEP_PATIENT: Final = "patient_shred"
STEP_SKIPPED: Final = "skipped"
STEP_FINALIZE: Final = "finalize"
TERMINAL_STATES: Final = frozenset({"completed", "verified"})
CTL_PURGE: Final[dict[str, str]] = {"t": "purge"}


class PurgeJobNotFound(LookupError):
    def __init__(self, job_id: UUID) -> None:
        super().__init__(f"purge job {job_id} not found")
        self.job_id = job_id


@dataclass
class _Run:
    """One execution. Holds plain values copied from the job row, not the mapped instance: every
    ``UPDATE purge_jobs`` expires the instance's columns and a lazy reload is not possible here."""

    ctx: HandlerContext
    session: Any
    job_id: UUID
    tenant_id: UUID
    now: datetime
    prior_steps: list[dict[str, Any]]
    steps: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    fingerprints: list[str] = field(default_factory=list)
    sample: bytes | None = None

    # --- bookkeeping ---------------------------------------------------------------------------

    async def _record(self, step: str, subject: UUID, counts: dict[str, int]) -> None:
        entry = {
            "step": step,
            "name": step,
            "at": self.now.isoformat(),
            "subject_id": str(subject),
            "counts": counts,
            "count": int(sum(counts.values())),
        }
        self.steps.append(entry)
        for name, value in counts.items():
            self.counts[name] = self.counts.get(name, 0) + int(value)
        await purge_repo.append_step(self.session, self.job_id, entry)
        await audit.record(
            self.session,
            tenant_id=self.tenant_id,
            actor_id=None,
            actor_role="service",
            action="purge.step",
            resource_type="purge_job",
            resource_id=self.job_id,
            detail={"step": step, "subject_id": str(subject), "counts": counts},
        )

    async def _invalidate(self, scope_id: UUID) -> None:
        self.ctx.keycache.invalidate(scope_id)
        with contextlib.suppress(RedisError, OSError):
            await self.ctx.redis.publish(keys.KEYS_INVALIDATE, str(scope_id))

    # --- session ------------------------------------------------------------------------------

    async def purge_session(self, sid: UUID) -> None:
        sess = await sessions_repo.get_session(self.session, sid)
        if sess is None:
            await self._record(STEP_SKIPPED, sid, {"missing_session": 1})
            return
        if sess.state == "purged" and sess.dek_wrapped is None:
            await self._record(STEP_SKIPPED, sid, {"already_purged": 1})
            return
        # 1. running: sample ciphertext (failed-decrypt demo) + DEK fingerprint
        sample = await purge_repo.sample_ciphertext(self.session, sid)
        if self.sample is None and sample is not None:
            self.sample = bytes(sample)
        fp = sess.dek_fingerprint or (dek_fingerprint(bytes(sess.dek_wrapped)) if sess.dek_wrapped else None)
        if fp is not None:
            self.fingerprints.append(bytes(fp).hex())
        await sessions_repo.update_session(self.session, sid, state="purging")
        await self._record(
            STEP_CAPTURE,
            sid,
            {"sample_ciphertext": int(sample is not None), "dek_fingerprint": int(fp is not None)},
        )
        # 2. force-close live ingest/viewers, drop every Redis key of the session
        await self._record(STEP_REDIS, sid, await self._purge_redis(sid))
        # 3. object store
        deleted = await self.ctx.objectstore.delete_prefix(session_prefix(self.tenant_id, sid))
        await self._record(STEP_OBJECTSTORE, sid, {"objects": int(deleted)})
        # 4. rows (segment_search, transcript_segments, risk_events, unsigned notes, stt_offsets, audio_chunks)
        await self._record(STEP_ROWS, sid, await purge_repo.delete_session_data(self.session, sid))
        # 5. crypto-shred: dek_wrapped = NULL, tombstone state
        shredded = await purge_repo.destroy_session_dek(self.session, sid, now=self.now)
        await self._invalidate(sid)
        await self._record(STEP_SHRED, sid, {"session_dek_destroyed": int(shredded is not None)})
        await audit.record(
            self.session,
            tenant_id=self.tenant_id,
            actor_id=None,
            actor_role="service",
            action="session.purged",
            resource_type="session",
            resource_id=sid,
            detail={"purge_job_id": str(self.job_id)},
        )

    async def _purge_redis(self, sid: UUID) -> dict[str, int]:
        redis = self.ctx.redis
        await redis.publish(keys.ctl(sid), '{"t":"purge"}')
        removed = int(await redis.srem(keys.STT_ACTIVE, str(sid)))
        deleted = 0
        async for key in redis.scan_iter(match=keys.sess_pattern(sid), count=200):
            deleted += int(await redis.delete(key))
        deleted += int(await redis.delete(keys.stt_owner(sid)))
        lag = int(await redis.hdel(keys.STT_LAG, str(sid)))
        return {"redis_keys": deleted, "stt_active": removed, "stt_lag": lag}

    # --- patient ------------------------------------------------------------------------------

    async def purge_patient(self, pid: UUID) -> None:
        patient = await patients_repo.get_patient(self.session, pid)
        if patient is None:
            await self._record(STEP_SKIPPED, pid, {"missing_patient": 1})
            return
        for sid in await purge_repo.sessions_of_patient(self.session, pid):
            await self.purge_session(sid)
        if patient.dek_fingerprint is not None:
            self.fingerprints.append(bytes(patient.dek_fingerprint).hex())
        had_identifiers = int(patient.name_enc is not None or patient.phone_enc is not None)
        shredded = await purge_repo.destroy_patient_dek(self.session, pid, now=self.now)
        await self._invalidate(pid)
        await self._record(
            STEP_PATIENT,
            pid,
            {"patient_identifiers": had_identifiers, "patient_dek_destroyed": int(shredded is not None)},
        )

    # --- finalize -----------------------------------------------------------------------------

    async def finalize(self) -> PurgeJob:
        digest = receipt.receipt_hash(self.prior_steps + self.steps, self.counts, self.fingerprints)
        job = await purge_repo.update_job(
            self.session,
            self.job_id,
            state="completed",
            completed_at=self.now,
            counts=self.counts,
            dek_fingerprints=self.fingerprints,
            sample_ciphertext=self.sample,
            receipt_hash=digest,
        )
        assert job is not None
        await writer.emit(
            self.session,
            tenant_id=self.tenant_id,
            aggregate_type="purge_job",
            aggregate_id=job.id,
            event_type=PURGE_COMPLETED,
            payload={"purge_job_id": str(job.id)},
            idempotency_key=writer.idempotency_key(PURGE_COMPLETED, job.id, 1),
        )
        await audit.record(
            self.session,
            tenant_id=self.tenant_id,
            actor_id=None,
            actor_role="service",
            action="purge.completed",
            resource_type="purge_job",
            resource_id=job.id,
            detail={
                "subject_type": job.subject_type,
                "subject_id": str(job.subject_id),
                "counts": self.counts,
                "dek_fingerprints": len(self.fingerprints),
                "receipt_hash": digest.hex(),
            },
        )
        with contextlib.suppress(RedisError, OSError):
            await self.ctx.redis.publish(keys.OUTBOX_WAKE, "1")
        return job


async def run(ctx: HandlerContext, purge_job_id: UUID, *, tenant_id: UUID) -> PurgeJob:
    """Execute the purge for ``purge_job_id`` (§8.4 steps 1–6) and return the completed job row."""
    started = time.monotonic()
    async with ctx.tenant_tx(tenant_id) as session:
        job = await purge_repo.get_job(session, purge_job_id)
        if job is None:
            raise PurgeJobNotFound(purge_job_id)
        if job.state in TERMINAL_STATES:
            return job
        subject_type, subject_id = job.subject_type, job.subject_id
        run_ = _Run(
            ctx=ctx,
            session=session,
            job_id=job.id,
            tenant_id=job.tenant_id,
            now=ctx.clock.now(),
            prior_steps=list(job.steps),
        )
        await purge_repo.update_job(session, job.id, state="running")
        if subject_type == "session":
            await run_.purge_session(subject_id)
        else:
            await run_.purge_patient(subject_id)
        job = await run_.finalize()
    _observe(time.monotonic() - started)
    log.info(
        "purge completed",
        extra={"purge_job_id": str(job.id), "subject_type": job.subject_type, "steps": len(run_.steps)},
    )
    return job


async def create_job(
    session: Any,
    *,
    tenant_id: UUID,
    subject_type: str,
    subject_id: UUID,
    reason: str,
    requested_by: UUID | None,
) -> PurgeJob:
    """``purge_jobs`` row with a fresh uuid7 (used by the REST route, the CLI and the revoke handler)."""
    return await purge_repo.create_job(
        session,
        id=uuid7(),
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        reason=reason,
        requested_by=requested_by,
    )


def _observe(seconds: float) -> None:
    metrics.PURGE_DURATION_SECONDS.observe(seconds)
