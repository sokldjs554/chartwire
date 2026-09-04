"""The one test that connects as superuser: the same query returns every tenant, proving the
other RLS tests exercise a real non-bypass role rather than an accident of the fixtures (§0.8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from chartwire.db.repo import search as search_repo
from chartwire.db.tenant import tenant_tx

pytestmark = pytest.mark.integration


async def test_superuser_sees_both_tenants(su_engine, app_engine, patient_a, patient_b):
    async with su_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT tenant_id FROM patients ORDER BY tenant_id"))).scalars().all()
        flags = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert len(rows) == 2 and rows[0] != rows[1]  # leak: no GUC, still both tenants
    assert flags[0] is True
    async with app_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM patients"))).scalar_one() == 0


async def _index(engine, ctx, session_row, segment_id: int) -> None:
    async with tenant_tx(engine, ctx) as session:
        assert await search_repo.index_segment(
            session,
            segment_id=segment_id,
            segment_created_at=datetime.now(tz=UTC),
            tenant_id=session_row.tenant_id,
            session_id=session_row.id,
            patient_id=session_row.patient_id,
            speaker="patient",
            text_plain="요즘 불면증이 심합니다",
            terms=["불면"],
        )


async def test_owner_is_subject_to_force_rls_except_segment_search(
    owner_engine, app_engine, session_a, session_b, ctx_a, ctx_b, patient_a, patient_b
):
    """FORCE ROW LEVEL SECURITY: even the table owner gets 0 rows without a tenant context.

    ``segment_search`` is the documented exception (ENABLE, not FORCE) so that the SECURITY DEFINER
    ``search_segments()`` can use the trigram index (ADR-0005) — the owner therefore sees **both**
    tenants' plaintext. The rows are indexed first on purpose: asserting the owner's count against an
    empty table would pass whether the exemption existed or not, and would keep passing if the table
    were ever FORCEd by accident. This is also why the runtime role must never be the owner (§0.7);
    the single-role fallback has no ``chartwire_app`` role, so migration 0006 FORCEs the table there.
    """
    await _index(app_engine, ctx_a, session_a, 1)
    await _index(app_engine, ctx_b, session_b, 2)
    async with owner_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM patients"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT count(*) FROM segment_search"))).scalar_one() == 2
    async with app_engine.connect() as conn:  # the app role sees neither without a tenant context
        assert (await conn.execute(text("SELECT count(*) FROM segment_search"))).scalar_one() == 0
