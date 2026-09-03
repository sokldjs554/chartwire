"""The one test that connects as superuser: the same query returns every tenant, proving the
other RLS tests exercise a real non-bypass role rather than an accident of the fixtures (§0.8)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

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


async def test_owner_is_subject_to_force_rls(owner_engine, patient_a, patient_b):
    """FORCE ROW LEVEL SECURITY: even the table owner gets 0 rows without a tenant context."""
    async with owner_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM patients"))).scalar_one() == 0
        # segment_search is ENABLE-only: the owner (= the SECURITY DEFINER search function) is exempt
        assert (await conn.execute(text("SELECT count(*) FROM segment_search"))).scalar_one() == 0
