"""RLS isolation as the real ``chartwire_app`` role (spec §0.8, §8.1)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.db.tenant import TenantCtx, tenant_tx

pytestmark = pytest.mark.integration


async def _count(engine: AsyncEngine, ctx: TenantCtx | None, sql: str) -> int:
    if ctx is None:
        async with engine.connect() as conn:
            return int((await conn.execute(text(sql))).scalar_one())
    async with tenant_tx(engine, ctx) as session:
        return int((await session.execute(text(sql))).scalar_one())


async def test_query_without_tenant_predicate_sees_only_own_tenant(
    app_engine, patient_a, patient_b, ctx_a, ctx_b
):
    assert await _count(app_engine, ctx_a, "SELECT count(*) FROM patients") == 1
    assert await _count(app_engine, ctx_b, "SELECT count(*) FROM patients") == 1
    async with tenant_tx(app_engine, ctx_a) as session:
        ids = (await session.execute(text("SELECT id FROM patients"))).scalars().all()
    assert ids == [patient_a.id]


async def test_no_context_yields_zero_rows_fail_closed(app_engine, patient_a, patient_b):
    for table in (
        "patients",
        "users",
        "sessions",
        "consents",
        "transcript_segments",
        "audit_events",
        "outbox_events",
    ):
        assert await _count(app_engine, None, f"SELECT count(*) FROM {table}") == 0


async def test_empty_string_context_is_not_a_cast_error(app_engine, patient_a):
    async with app_engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        assert (await conn.execute(text("SELECT count(*) FROM patients"))).scalar_one() == 0


async def test_insert_with_foreign_tenant_id_is_rejected(app_engine, tenant_a, tenant_b, ctx_a):
    with pytest.raises(DBAPIError) as exc:
        async with tenant_tx(app_engine, ctx_a) as session:
            await session.execute(
                text("INSERT INTO patients (tenant_id, pseudonym) VALUES (:t, '가상환자-9999')"),
                {"t": tenant_b.id},
            )
    assert getattr(exc.value.orig, "sqlstate", None) == "42501"  # new row violates row-level security policy


async def test_cross_tenant_update_and_delete_touch_nothing(app_engine, patient_a, patient_b, ctx_a, ctx_b):
    async with tenant_tx(app_engine, ctx_a) as session:
        updated = await session.execute(
            text("UPDATE patients SET birth_year = 1900 WHERE id = :p"), {"p": patient_b.id}
        )
        deleted = await session.execute(text("DELETE FROM patients WHERE id = :p"), {"p": patient_b.id})
    assert updated.rowcount == 0 and deleted.rowcount == 0
    async with tenant_tx(app_engine, ctx_b) as session:
        year = (
            await session.execute(text("SELECT birth_year FROM patients WHERE id = :p"), {"p": patient_b.id})
        ).scalar_one()
    assert year == 1990


async def test_app_cannot_escalate_to_owner_or_bypass(app_engine):
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text("SET ROLE chartwire_owner"))
        assert getattr(exc.value.orig, "sqlstate", None) == "42501"
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert tuple(row) == (False, False)


async def test_app_has_no_write_grant_on_tenants(app_engine, tenant_a, ctx_a):
    with pytest.raises(DBAPIError) as exc:
        async with tenant_tx(app_engine, ctx_a) as session:
            await session.execute(text("UPDATE tenants SET name = 'x' WHERE id = :t"), {"t": tenant_a.id})
    assert getattr(exc.value.orig, "sqlstate", None) == "42501"


async def test_role_gate_hides_notes_from_staff_and_admin(app_engine, session_a, tenant_a, clinician_a):
    from chartwire.core.ids import uuid7
    from chartwire.db.repo import notes as notes_repo

    clinician = TenantCtx(tenant_a.id, clinician_a.id, "clinician")
    async with tenant_tx(app_engine, clinician) as session:
        await notes_repo.create_note(
            session,
            id=uuid7(),
            tenant_id=tenant_a.id,
            session_id=session_a.id,
            version=1,
            status="drafting",
            provider="extractive",
        )
    for role, expected in (("clinician", 1), ("service", 1), ("auditor", 1), ("staff", 0), ("admin", 0)):
        ctx = TenantCtx(tenant_a.id, None if role == "service" else clinician_a.id, role)
        assert await _count(app_engine, ctx, "SELECT count(*) FROM notes") == expected, role


def test_tenant_ctx_rejects_unknown_role():
    with pytest.raises(ValueError):
        TenantCtx(UUID(int=1), None, "superadmin")
