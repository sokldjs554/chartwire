"""``GET /v1/search?q=&mode=term|text&patient_id=`` → ``search_segments()`` (SECURITY DEFINER, §4.6).

``term`` mode matches the lexicon tags (``terms @> ARRAY[q]``); ``text`` mode is a trigram ILIKE and
needs ≥ 3 characters (a 2-syllable Korean pattern yields no trigram — 422 ``CW-4220``).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from chartwire.api.schemas import SearchHit
from chartwire.api.deps import open_tx
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.db.repo import search as search_repo
from chartwire.db.repo import segments as segments_repo

router = APIRouter(prefix="/v1", tags=["search"])
CLINICAL = ("clinician", "staff")


@router.get("/search", response_model=list[SearchHit])
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=120),
    mode: Literal["term", "text"] = "text",
    patient_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require(*CLINICAL)),
) -> list[SearchHit]:
    async with open_tx(request, principal) as (_deps, s):
        hits = await search_repo.search(s, q, mode, patient_id, limit)  # raises CW-4220 for text < 3 chars
        seqs = {
            (r.created_at, r.id): r.seq
            for r in await segments_repo.by_keys(s, [(h.segment_created_at, h.segment_id) for h in hits])
        }
    return [
        SearchHit(
            session_id=h.session_id,
            segment_id=h.segment_id,
            seq=seqs.get((h.segment_created_at, h.segment_id)),
            speaker=h.speaker,
            snippet=h.snippet,
            created_at=h.segment_created_at,
        )
        for h in hits
    ]
