"""``notes``, ``note_statements`` and the clinician-only ``note_assessments``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import Note, NoteAssessment, NoteStatement


async def next_version(session: AsyncSession, session_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(Note.version), 0) + 1).where(Note.session_id == session_id)
    return int((await session.execute(stmt)).scalar_one())


async def create_note(
    session: AsyncSession, *, id: UUID, tenant_id: UUID, session_id: UUID, **values: Any
) -> Note:
    stmt = insert(Note).values(id=id, tenant_id=tenant_id, session_id=session_id, **values).returning(Note)
    return (await session.scalars(stmt)).one()


async def get_note(session: AsyncSession, note_id: UUID) -> Note | None:
    return await session.get(Note, note_id)


async def latest_for_session(session: AsyncSession, session_id: UUID) -> Note | None:
    stmt = select(Note).where(Note.session_id == session_id).order_by(Note.version.desc()).limit(1)
    return (await session.scalars(stmt)).one_or_none()


async def update_note(session: AsyncSession, note_id: UUID, **values: Any) -> Note | None:
    return (
        await session.scalars(update(Note).where(Note.id == note_id).values(**values).returning(Note))
    ).one_or_none()


async def sign_note(
    session: AsyncSession,
    note_id: UUID,
    *,
    signed_by: UUID,
    signed_at: datetime,
    signed_content_enc: bytes,
    retention_until: datetime,
) -> Note | None:
    """Signed note = medical record (ADR-0004): ``legal_hold`` + 10-year retention set atomically."""
    return await update_note(
        session,
        note_id,
        status="signed",
        signed_by=signed_by,
        signed_at=signed_at,
        signed_content_enc=signed_content_enc,
        legal_hold="medical_record",
        retention_until=retention_until,
    )


async def insert_statements(session: AsyncSession, rows: list[dict[str, Any]]) -> Sequence[NoteStatement]:
    if not rows:
        return []
    return (await session.scalars(insert(NoteStatement).values(rows).returning(NoteStatement))).all()


async def list_statements(session: AsyncSession, note_id: UUID) -> Sequence[NoteStatement]:
    stmt = (
        select(NoteStatement)
        .where(NoteStatement.note_id == note_id)
        .order_by(NoteStatement.section, NoteStatement.ordinal, NoteStatement.id)
    )
    return (await session.scalars(stmt)).all()


async def set_decision(
    session: AsyncSession, statement_id: int, *, decision: str, edited_text_enc: bytes | None
) -> NoteStatement | None:
    stmt = (
        update(NoteStatement)
        .where(NoteStatement.id == statement_id)
        .values(clinician_decision=decision, edited_text_enc=edited_text_enc)
        .returning(NoteStatement)
    )
    return (await session.scalars(stmt)).one_or_none()


async def upsert_assessment(
    session: AsyncSession, *, note_id: UUID, tenant_id: UUID, text_enc: bytes, author_id: UUID, now: datetime
) -> NoteAssessment:
    """The only write path into ``note_assessments`` (PUT /notes/{id}/assessment)."""
    stmt = pg_insert(NoteAssessment).values(
        note_id=note_id,
        tenant_id=tenant_id,
        text_enc=text_enc,
        author_id=author_id,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["note_id"], set_={"text_enc": text_enc, "author_id": author_id, "updated_at": now}
    ).returning(NoteAssessment)
    return (await session.scalars(stmt)).one()


async def get_assessment(session: AsyncSession, note_id: UUID) -> NoteAssessment | None:
    return await session.get(NoteAssessment, note_id)
