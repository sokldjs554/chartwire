"""Consent-gated plaintext search index and the SECURITY DEFINER ``search_segments()`` call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.core.errors import AppError
from chartwire.db.models import SegmentSearch

SearchMode = Literal["term", "text"]
MIN_TEXT_QUERY_CHARS = 3  # a 2-syllable Korean pattern yields zero trigrams (§4.6 Q2a)


@dataclass(frozen=True, slots=True)
class SearchHit:
    segment_id: int
    session_id: UUID
    patient_id: UUID
    speaker: str
    snippet: str
    segment_created_at: datetime


async def search(
    session: AsyncSession,
    query: str,
    mode: SearchMode,
    patient_id: UUID | None = None,
    limit: int = 50,
) -> list[SearchHit]:
    """``SELECT * FROM search_segments(...)``: runs as the function owner (owner-exempt on the
    ENABLE-only ``segment_search`` RLS), so the trigram/array GIN indexes are usable (ADR-0005)."""
    if mode not in ("term", "text"):
        raise AppError("CW-4221", 422, "mode는 term 또는 text여야 합니다")
    if mode == "text" and len(query) < MIN_TEXT_QUERY_CHARS:
        raise AppError("CW-4220", 422, f"자유 검색어는 {MIN_TEXT_QUERY_CHARS}자 이상이어야 합니다")
    stmt = text("SELECT * FROM search_segments(:q, :mode, CAST(:p AS uuid), :limit)")
    rows = await session.execute(stmt, {"q": query, "mode": mode, "p": patient_id, "limit": limit})
    return [SearchHit(**row._mapping) for row in rows]


async def index_segment(
    session: AsyncSession,
    *,
    segment_id: int,
    segment_created_at: datetime,
    tenant_id: UUID,
    session_id: UUID,
    patient_id: UUID,
    speaker: str,
    text_plain: str,
    terms: list[str],
) -> bool:
    """Insert the plaintext search row (only when the ``search_index`` scope is active — the
    caller checks consent). Returns False if the segment was already indexed."""
    stmt = (
        pg_insert(SegmentSearch)
        .values(
            segment_id=segment_id,
            segment_created_at=segment_created_at,
            tenant_id=tenant_id,
            session_id=session_id,
            patient_id=patient_id,
            speaker=speaker,
            text=text_plain,
            terms=terms,
        )
        .on_conflict_do_nothing(index_elements=["segment_id"])
    )
    return bool((await session.execute(stmt)).rowcount)


async def delete_for_session(session: AsyncSession, session_id: UUID) -> int:
    result = await session.execute(delete(SegmentSearch).where(SegmentSearch.session_id == session_id))
    return int(result.rowcount or 0)


async def count_for_session(session: AsyncSession, session_id: UUID) -> int:
    stmt = select(func.count()).select_from(SegmentSearch).where(SegmentSearch.session_id == session_id)
    return int((await session.execute(stmt)).scalar_one())
