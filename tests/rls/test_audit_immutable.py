"""``audit_events`` is append-only: grants stop the app role, the trigger stops the owner (§8.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from chartwire.audit import service as audit_service
from chartwire.db.repo import audit as audit_repo
from chartwire.db.tenant import TenantCtx, tenant_tx

pytestmark = pytest.mark.integration


async def _record(engine, ctx, **detail) -> int:
    async with tenant_tx(engine, ctx) as session:
        return await audit_service.record(
            session,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            actor_role=ctx.role,
            action="session.created",
            resource_type="session",
            resource_id="00000000-0000-0000-0000-000000000001",
            request_id="req-1",
            detail=detail or None,
        )


async def test_app_update_is_permission_denied(app_engine, ctx_a):
    event_id = await _record(app_engine, ctx_a, chunk_count=3)
    for sql in (
        "UPDATE audit_events SET action = 'x' WHERE id = :i",
        "DELETE FROM audit_events WHERE id = :i",
    ):
        with pytest.raises(DBAPIError) as exc:
            async with tenant_tx(app_engine, ctx_a) as session:
                await session.execute(text(sql), {"i": event_id})
        assert getattr(exc.value.orig, "sqlstate", None) == "42501"


async def test_owner_update_hits_the_trigger(app_engine, owner_engine, ctx_a):
    event_id = await _record(app_engine, ctx_a)
    with pytest.raises(DBAPIError) as exc:  # owner has the privilege; the trigger still refuses
        async with tenant_tx(owner_engine, ctx_a) as session:
            await session.execute(text("UPDATE audit_events SET action = 'x' WHERE id = :i"), {"i": event_id})
    assert "append-only" in str(exc.value.orig)


async def test_select_requires_auditor_admin_or_service(app_engine, tenant_a, clinician_a, ctx_a):
    await _record(app_engine, ctx_a)
    for role, expected in (("clinician", 0), ("staff", 0), ("auditor", 1), ("admin", 1), ("service", 1)):
        ctx = TenantCtx(tenant_a.id, None if role == "service" else clinician_a.id, role)
        async with tenant_tx(app_engine, ctx) as session:
            rows = await audit_repo.list_events(session, tenant_a.id)
        assert len(rows) == expected, role


async def test_service_rejects_phi_keys_before_touching_the_db(app_engine, ctx_a):
    for detail in ({"text": "x"}, {"ids": [{"name": "x"}]}, {"nested": {"phone": "010-0000-0000"}}):
        with pytest.raises(ValueError):
            await _record(app_engine, ctx_a, **detail)
    async with tenant_tx(app_engine, TenantCtx(ctx_a.tenant_id, ctx_a.user_id, "auditor")) as session:
        assert await audit_repo.list_events(session, ctx_a.tenant_id) == []
