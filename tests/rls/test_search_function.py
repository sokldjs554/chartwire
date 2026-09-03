"""``search_segments()`` runs as the app role through the SECURITY DEFINER boundary (§4.6 Q2d)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from chartwire.core.errors import AppError
from chartwire.db.repo import search as search_repo
from chartwire.db.tenant import tenant_tx

pytestmark = pytest.mark.integration


async def _index(engine, ctx, session_row, segment_id: int, text_plain: str, terms: list[str]) -> None:
    async with tenant_tx(engine, ctx) as session:
        assert await search_repo.index_segment(
            session,
            segment_id=segment_id,
            segment_created_at=datetime.now(tz=UTC),
            tenant_id=session_row.tenant_id,
            session_id=session_row.id,
            patient_id=session_row.patient_id,
            speaker="patient",
            text_plain=text_plain,
            terms=terms,
        )


async def test_text_and_term_search_are_tenant_scoped(app_engine, session_a, session_b, ctx_a, ctx_b):
    await _index(app_engine, ctx_a, session_a, 1, "요즘 불면증이 심해서 새벽까지 잠을 못 잡니다", ["불면"])
    await _index(app_engine, ctx_b, session_b, 2, "불면증 때문에 힘들어요", ["불면"])
    async with tenant_tx(app_engine, ctx_a) as session:
        hits = await search_repo.search(session, "불면증", "text")
        term_hits = await search_repo.search(session, "불면", "term")
        scoped = await search_repo.search(session, "불면증", "text", patient_id=session_b.patient_id)
    assert [h.segment_id for h in hits] == [1]
    assert hits[0].session_id == session_a.id and hits[0].snippet.startswith("요즘")
    assert [h.segment_id for h in term_hits] == [1]
    assert scoped == []
    async with tenant_tx(app_engine, ctx_b) as session:
        assert [h.segment_id for h in await search_repo.search(session, "불면증", "text")] == [2]


async def test_function_fails_closed_without_context(app_engine, session_a, ctx_a):
    await _index(app_engine, ctx_a, session_a, 1, "불면증", [])
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text("SELECT * FROM search_segments('불면증', 'text')"))
    assert getattr(exc.value.orig, "sqlstate", None) == "42501"


async def test_short_free_text_is_rejected_in_python_and_in_sql(app_engine, ctx_a):
    async with tenant_tx(app_engine, ctx_a) as session:
        with pytest.raises(AppError) as exc:
            await search_repo.search(session, "불면", "text")
        assert exc.value.code == "CW-4220" and exc.value.status == 422
    with pytest.raises(DBAPIError) as db_exc:
        async with tenant_tx(app_engine, ctx_a) as session:
            await session.execute(text("SELECT * FROM search_segments('불면', 'text')"))
    assert getattr(db_exc.value.orig, "sqlstate", None) == "22023"


async def test_app_cannot_read_segment_search_of_other_tenant_directly(
    app_engine, session_a, session_b, ctx_a, ctx_b
):
    await _index(app_engine, ctx_a, session_a, 1, "불면증", [])
    await _index(app_engine, ctx_b, session_b, 2, "불면증", [])
    async with tenant_tx(app_engine, ctx_a) as session:
        n = (
            await session.execute(text("SELECT count(*) FROM segment_search WHERE text ILIKE '%불면증%'"))
        ).scalar_one()
        deleted = await search_repo.delete_for_session(session, session_b.id)
    assert n == 1 and deleted == 0
