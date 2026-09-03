"""``transcript_segments`` — idempotent final inserts, replay and the patient timeline (Q1/Q1a).

Text arrives already encrypted (the stt-worker holds the session DEK); the repository never
sees plaintext. ``SegmentRow`` is the raw row; ``notes.schema.SegmentView`` (WP-D) is built
from it after decryption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, and_, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import Session as SessionModel
from chartwire.db.models import TranscriptSegment as T


@dataclass(frozen=True, slots=True)
class SegmentRow:
    id: int
    tenant_id: UUID
    session_id: UUID
    patient_id: UUID
    seq: int
    speaker: str
    t_start_ms: int
    t_end_ms: int
    text_enc: bytes
    text_len: int
    confidence: float | None
    provider: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: Row[Any] | T) -> SegmentRow:
        mapping = (
            row._mapping if isinstance(row, Row) else {f: getattr(row, f) for f in cls.__dataclass_fields__}
        )
        return cls(**{f: mapping[f] for f in cls.__dataclass_fields__})


_COLUMNS = [getattr(T, f) for f in SegmentRow.__dataclass_fields__]


async def insert_final(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    patient_id: UUID,
    seq: int,
    speaker: str,
    t_start_ms: int,
    t_end_ms: int,
    text_enc: bytes,
    text_len: int,
    confidence: float | None,
    provider: str,
    created_at: datetime,
) -> SegmentRow:
    """``ON CONFLICT (session_id, seq, created_at) DO NOTHING``; returns the existing row on conflict
    so a re-delivered STT final is a no-op (at-least-once stream, exactly-once table)."""
    stmt = (
        pg_insert(T)
        .values(
            tenant_id=tenant_id,
            session_id=session_id,
            patient_id=patient_id,
            seq=seq,
            speaker=speaker,
            t_start_ms=t_start_ms,
            t_end_ms=t_end_ms,
            text_enc=text_enc,
            text_len=text_len,
            confidence=confidence,
            provider=provider,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["session_id", "seq", "created_at"])
        .returning(*_COLUMNS)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        existing = select(*_COLUMNS).where(
            T.session_id == session_id, T.seq == seq, T.created_at == created_at
        )
        row = (await session.execute(existing)).one()
    return SegmentRow.from_row(row)


async def replay(
    session: AsyncSession,
    session_id: UUID,
    after_seq: int,
    limit: int,
    *,
    started_at: datetime | None = None,
) -> list[SegmentRow]:
    """Segments ``seq > after_seq`` in order. The ``created_at >= started_at`` predicate is what
    lets the planner prune to one partition (Q1a); it is looked up when the caller has no value."""
    if started_at is None:
        started_at = (
            await session.execute(select(SessionModel.started_at).where(SessionModel.id == session_id))
        ).scalar()
    stmt = select(*_COLUMNS).where(T.session_id == session_id, T.seq > after_seq)
    if started_at is not None:
        stmt = stmt.where(T.created_at >= started_at)
    rows = await session.execute(stmt.order_by(T.seq).limit(limit))
    return [SegmentRow.from_row(r) for r in rows]


async def timeline(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    since: datetime,
    before: tuple[datetime, int] | None = None,
    limit: int = 50,
) -> list[SegmentRow]:
    """Q1 patient timeline: keyset ``(created_at, id) < before`` over ``ix_segments_patient_time``."""
    stmt = select(*_COLUMNS).where(
        T.tenant_id == tenant_id, T.patient_id == patient_id, T.created_at >= since
    )
    if before is not None:
        stmt = stmt.where(tuple_(T.created_at, T.id) < before)
    rows = await session.execute(stmt.order_by(T.created_at.desc(), T.id.desc()).limit(limit))
    return [SegmentRow.from_row(r) for r in rows]


async def by_keys(session: AsyncSession, keys: list[tuple[datetime, int]]) -> list[SegmentRow]:
    """Fetch rows by their partitioned PK ``(created_at, id)`` (soft references from risk/notes)."""
    if not keys:
        return []
    stmt = (
        select(*_COLUMNS).where(or_(*[and_(T.created_at == c, T.id == i) for c, i in keys])).order_by(T.seq)
    )
    return [SegmentRow.from_row(r) for r in await session.execute(stmt)]
