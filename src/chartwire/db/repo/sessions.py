"""``sessions`` state machine, the ``audio_chunks`` ledger and ``stt_offsets``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import AudioChunk, SttOffset
from chartwire.db.models import Session as SessionModel


async def create_session(
    session: AsyncSession,
    *,
    id: UUID,
    tenant_id: UUID,
    patient_id: UUID,
    clinician_id: UUID,
    script_ref: str | None = None,
    scopes_snapshot: list[str] | None = None,
    dek_wrapped: bytes | None = None,
    dek_fingerprint: bytes | None = None,
    chunk_ms: int = 200,
    stt_provider: str = "simulator",
) -> SessionModel:
    stmt = (
        insert(SessionModel)
        .values(
            id=id,
            tenant_id=tenant_id,
            patient_id=patient_id,
            clinician_id=clinician_id,
            script_ref=script_ref,
            scopes_snapshot=scopes_snapshot or [],
            dek_wrapped=dek_wrapped,
            dek_fingerprint=dek_fingerprint,
            chunk_ms=chunk_ms,
            stt_provider=stt_provider,
        )
        .returning(SessionModel)
    )
    return (await session.scalars(stmt)).one()


async def get_session(session: AsyncSession, session_id: UUID) -> SessionModel | None:
    return await session.get(SessionModel, session_id)


async def list_sessions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    state: str | None = None,
    clinician_id: UUID | None = None,
    patient_id: UUID | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> Sequence[SessionModel]:
    """Keyset list, newest first (``before`` = ``created_at`` of the last row seen)."""
    stmt = select(SessionModel).where(SessionModel.tenant_id == tenant_id)
    if state is not None:
        stmt = stmt.where(SessionModel.state == state)
    if clinician_id is not None:
        stmt = stmt.where(SessionModel.clinician_id == clinician_id)
    if patient_id is not None:
        stmt = stmt.where(SessionModel.patient_id == patient_id)
    if before is not None:
        stmt = stmt.where(SessionModel.created_at < before)
    return (await session.scalars(stmt.order_by(SessionModel.created_at.desc()).limit(limit))).all()


async def update_session(session: AsyncSession, session_id: UUID, **values: Any) -> SessionModel | None:
    """Generic column update (state transitions, timestamps, ack/final seq, epoch); bumps ``updated_at``."""
    stmt = (
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(updated_at=func.now(), **values)
        .returning(SessionModel)
    )
    return (await session.scalars(stmt)).one_or_none()


async def set_state(
    session: AsyncSession, session_id: UUID, state: str, *, now: datetime | None = None
) -> SessionModel | None:
    stamp = {
        "recording": "started_at",
        "ended": "ended_at",
        "transcribed": "transcribed_at",
        "signed": "signed_at",
    }
    values: dict[str, Any] = {"state": state}
    if state in stamp and now is not None:
        values[stamp[state]] = now
    return await update_session(session, session_id, **values)


async def bump_epoch(session: AsyncSession, session_id: UUID) -> int | None:
    stmt = (
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(epoch=SessionModel.epoch + 1, updated_at=func.now())
        .returning(SessionModel.epoch)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------- ledger


async def insert_chunks(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Batch-insert ledger rows (``LedgerBatcher``). Duplicates (resume re-sends) are ignored."""
    if not rows:
        return 0
    stmt = pg_insert(AudioChunk).values(rows).on_conflict_do_nothing(index_elements=["session_id", "seq"])
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def ledgered_seqs(session: AsyncSession, session_id: UUID, from_seq: int, to_seq: int) -> list[int]:
    """Sequence numbers present in the ledger within ``[from_seq, to_seq]`` (resume: ``missing``)."""
    stmt = (
        select(AudioChunk.seq)
        .where(AudioChunk.session_id == session_id, AudioChunk.seq >= from_seq, AudioChunk.seq <= to_seq)
        .order_by(AudioChunk.seq)
    )
    return [int(s) for s in (await session.scalars(stmt)).all()]


async def chunks_between(
    session: AsyncSession, session_id: UUID, from_seq: int, to_seq: int
) -> Sequence[AudioChunk]:
    """Ledger rows for the stt-worker gap rebuild (§7.4)."""
    stmt = (
        select(AudioChunk)
        .where(AudioChunk.session_id == session_id, AudioChunk.seq >= from_seq, AudioChunk.seq <= to_seq)
        .order_by(AudioChunk.seq)
    )
    return (await session.scalars(stmt)).all()


async def max_ledger_seq(session: AsyncSession, session_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(AudioChunk.seq), 0)).where(AudioChunk.session_id == session_id)
    return int((await session.execute(stmt)).scalar_one())


# ---------------------------------------------------------------- stt offsets


async def get_stt_offset(session: AsyncSession, session_id: UUID) -> SttOffset | None:
    return await session.get(SttOffset, session_id)


async def upsert_stt_offset(
    session: AsyncSession, *, session_id: UUID, tenant_id: UUID, last_chunk_seq: int, last_segment_seq: int
) -> None:
    stmt = pg_insert(SttOffset).values(
        session_id=session_id,
        tenant_id=tenant_id,
        last_chunk_seq=last_chunk_seq,
        last_segment_seq=last_segment_seq,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["session_id"],
        set_={
            "last_chunk_seq": stmt.excluded.last_chunk_seq,
            "last_segment_seq": stmt.excluded.last_segment_seq,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
