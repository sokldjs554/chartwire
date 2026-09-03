"""Transaction-local GUCs never survive a pool checkout (§4.4)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from chartwire.db.engine import make_engine
from chartwire.db.tenant import TenantCtx, tenant_tx

pytestmark = pytest.mark.integration


async def _current_tenant(engine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one() or ""


async def test_tenant_tx_context_is_gone_after_checkin(migrated_db, tenant_a):
    engine = make_engine(migrated_db.app, pool_size=1, max_overflow=0)  # the same connection is reused
    try:
        async with tenant_tx(engine, TenantCtx.service(tenant_a.id)) as session:
            seen = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
            assert seen == str(tenant_a.id)
        assert await _current_tenant(engine) == ""
    finally:
        await engine.dispose()


async def test_rolled_back_context_is_gone_too(migrated_db, tenant_a):
    engine = make_engine(migrated_db.app, pool_size=1, max_overflow=0)
    try:
        with pytest.raises(RuntimeError):
            async with tenant_tx(engine, TenantCtx.service(tenant_a.id)):
                raise RuntimeError("boom")
        assert await _current_tenant(engine) == ""
    finally:
        await engine.dispose()


async def test_session_level_set_config_would_leak_which_is_why_is_local_is_mandatory(migrated_db, tenant_a):
    """Documents the hazard the contract guards against: ``set_config(..., false)`` committed on a
    pooled connection is visible to the next checkout. ``tenant_tx`` therefore always passes ``true``."""
    engine = make_engine(migrated_db.app, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_a.id)})
        assert await _current_tenant(engine) == str(tenant_a.id)  # leaked across checkout
        async with engine.begin() as conn:  # clean up so later tests on this engine are unaffected
            await conn.execute(text("RESET app.tenant_id"))
    finally:
        await engine.dispose()
