"""Tenant context and the transaction wrapper every repository call runs inside.

RLS policies read three transaction-local GUCs (``app.tenant_id``, ``app.user_id``,
``app.role``). ``tenant_tx`` sets them with ``set_config(..., true)`` inside ``BEGIN`` so
they vanish at COMMIT/ROLLBACK and can never leak through the connection pool (test
``tests/rls/test_guc_leak.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

ROLES: frozenset[str] = frozenset({"clinician", "staff", "admin", "auditor", "recorder", "service"})

_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :t, true), set_config('app.user_id', :u, true), set_config('app.role', :r, true)"
)


@dataclass(frozen=True, slots=True)
class TenantCtx:
    tenant_id: UUID
    user_id: UUID | None
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")

    @classmethod
    def service(cls, tenant_id: UUID) -> TenantCtx:
        """Context for worker handlers (no human actor)."""
        return cls(tenant_id=tenant_id, user_id=None, role="service")


async def apply_ctx(session: AsyncSession, ctx: TenantCtx) -> None:
    """Set the three GUCs on the session's current transaction."""
    await session.execute(
        _SET_CONTEXT,
        {"t": str(ctx.tenant_id), "u": "" if ctx.user_id is None else str(ctx.user_id), "r": ctx.role},
    )


@asynccontextmanager
async def tenant_tx(engine: AsyncEngine, ctx: TenantCtx) -> AsyncIterator[AsyncSession]:
    """One transaction under the tenant context; commits on exit, rolls back on error."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await apply_ctx(session, ctx)
        yield session
