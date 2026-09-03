"""``purge_jobs`` plus the hard-delete / crypto-shred / verification queries of §8.4.

Every deletion is by ``session_id`` (all partitions, via the parent) and idempotent; the
counts feed the purge receipt. Signed notes (``legal_hold IS NOT NULL``) are never touched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import (
    AudioChunk,
    Note,
    NoteAssessment,
    NoteStatement,
    Patient,
    PurgeJob,
    RiskEvent,
    SegmentSearch,
    SttOffset,
    TranscriptSegment,
)
from chartwire.db.models import Session as SessionModel


async def create_job(
    session: AsyncSession,
    *,
    id: UUID,
    tenant_id: UUID,
    subject_type: str,
    subject_id: UUID,
    reason: str,
    requested_by: UUID | None,
) -> PurgeJob:
    stmt = (
        insert(PurgeJob)
        .values(
            id=id,
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=reason,
            requested_by=requested_by,
        )
        .returning(PurgeJob)
    )
    return (await session.scalars(stmt)).one()


async def get_job(session: AsyncSession, job_id: UUID) -> PurgeJob | None:
    return await session.get(PurgeJob, job_id)


async def update_job(session: AsyncSession, job_id: UUID, **values: Any) -> PurgeJob | None:
    return (
        await session.scalars(
            update(PurgeJob).where(PurgeJob.id == job_id).values(**values).returning(PurgeJob)
        )
    ).one_or_none()


async def append_step(session: AsyncSession, job_id: UUID, step: dict[str, Any]) -> None:
    """``steps = steps || step`` in SQL so concurrent readers never see a truncated list."""
    stmt = (
        update(PurgeJob)
        .where(PurgeJob.id == job_id)
        .values(steps=PurgeJob.steps.op("||")(func.jsonb_build_array(func.to_jsonb(step))))
    )
    await session.execute(stmt)


async def sessions_of_patient(session: AsyncSession, patient_id: UUID) -> list[UUID]:
    stmt = (
        select(SessionModel.id).where(SessionModel.patient_id == patient_id).order_by(SessionModel.created_at)
    )
    return list((await session.scalars(stmt)).all())


async def sample_ciphertext(session: AsyncSession, session_id: UUID) -> bytes | None:
    """First ``text_enc`` of the session — kept in the job for the failed-decrypt demonstration."""
    stmt = (
        select(TranscriptSegment.text_enc)
        .where(TranscriptSegment.session_id == session_id)
        .order_by(TranscriptSegment.seq)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_session_data(session: AsyncSession, session_id: UUID) -> dict[str, int]:
    """Step 4 of §8.4. Unsigned notes go with their statements and (clinician) assessment rows —
    the FK requires it; signed notes and their assessments survive as medical records."""
    unsigned = select(Note.id).where(Note.session_id == session_id, Note.legal_hold.is_(None))
    counts: dict[str, int] = {}
    for name, stmt in (
        ("segment_search", delete(SegmentSearch).where(SegmentSearch.session_id == session_id)),
        ("transcript_segments", delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)),
        ("risk_events", delete(RiskEvent).where(RiskEvent.session_id == session_id)),
        ("note_statements", delete(NoteStatement).where(NoteStatement.note_id.in_(unsigned))),
        ("note_assessments", delete(NoteAssessment).where(NoteAssessment.note_id.in_(unsigned))),
        ("notes", delete(Note).where(Note.session_id == session_id, Note.legal_hold.is_(None))),
        ("stt_offsets", delete(SttOffset).where(SttOffset.session_id == session_id)),
        ("audio_chunks", delete(AudioChunk).where(AudioChunk.session_id == session_id)),
    ):
        counts[name] = int((await session.execute(stmt)).rowcount or 0)
    return counts


async def count_session_data(session: AsyncSession, session_id: UUID) -> dict[str, int]:
    """Verification (step 7): the same tables as :func:`delete_session_data`; all must be 0."""
    unsigned = select(Note.id).where(Note.session_id == session_id, Note.legal_hold.is_(None))
    queries = {
        "segment_search": select(func.count())
        .select_from(SegmentSearch)
        .where(SegmentSearch.session_id == session_id),
        "transcript_segments": select(func.count())
        .select_from(TranscriptSegment)
        .where(TranscriptSegment.session_id == session_id),
        "risk_events": select(func.count()).select_from(RiskEvent).where(RiskEvent.session_id == session_id),
        "note_statements": select(func.count())
        .select_from(NoteStatement)
        .where(NoteStatement.note_id.in_(unsigned)),
        "notes": select(func.count())
        .select_from(Note)
        .where(Note.session_id == session_id, Note.legal_hold.is_(None)),
        "stt_offsets": select(func.count()).select_from(SttOffset).where(SttOffset.session_id == session_id),
        "audio_chunks": select(func.count())
        .select_from(AudioChunk)
        .where(AudioChunk.session_id == session_id),
    }
    return {name: int((await session.execute(q)).scalar_one()) for name, q in queries.items()}


async def destroy_session_dek(
    session: AsyncSession, session_id: UUID, *, now: datetime
) -> SessionModel | None:
    """Step 5: crypto-shred. ``sessions_dek_guard`` prevents any later resurrection of the DEK."""
    stmt = (
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(dek_wrapped=None, dek_destroyed_at=now, state="purged", purged_at=now, updated_at=now)
        .returning(SessionModel)
    )
    return (await session.scalars(stmt)).one_or_none()


async def destroy_patient_dek(session: AsyncSession, patient_id: UUID, *, now: datetime) -> Patient | None:
    stmt = (
        update(Patient)
        .where(Patient.id == patient_id)
        .values(
            name_enc=None,
            phone_enc=None,
            dek_wrapped=None,
            dek_destroyed_at=now,
            consent_state="purged",
            purged_at=now,
        )
        .returning(Patient)
    )
    return (await session.scalars(stmt)).one_or_none()
