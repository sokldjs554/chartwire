"""Process-wide dependencies of the api (``AppDeps``) and the small helpers every router uses.

``create_app``'s lifespan builds one :class:`AppDeps` and stores it at ``app.state.deps``; tests that
run the app without a lifespan (``httpx.ASGITransport``) inject the same shape directly. Routers never
touch ``app.state`` themselves — they call :func:`get_deps` and open their transaction with
:func:`open_tx`, which sets the three RLS GUCs from the verified :class:`Principal`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Request, WebSocket
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chartwire.auth.jwt import Principal
from chartwire.core.clock import Clock
from chartwire.core.config import Settings
from chartwire.core.errors import AppError, NotFound
from chartwire.crypto.errors import DekDestroyedError
from chartwire.crypto.kek import KekProvider
from chartwire.crypto.keycache import KeyCache
from chartwire.db.models import Tenant
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.base import ObjectStore

REQUEST_ID_HEADER = "x-request-id"


@dataclass(slots=True)
class AppDeps:
    """Everything a request handler may need; one instance per process (§3.1 contracts)."""

    settings: Settings
    engine: AsyncEngine
    redis: Redis
    kek: KekProvider
    keycache: KeyCache
    objectstore: ObjectStore
    clock: Clock
    node_id: str


def get_deps(request: Request | WebSocket) -> AppDeps:
    deps = getattr(request.app.state, "deps", None)
    if deps is None:
        raise AppError("CW-5030", 503, "서비스가 아직 준비되지 않았습니다", retryable=True)
    return deps  # type: ignore[no-any-return]


def request_id(request: Request) -> str | None:
    """Set by :class:`chartwire.api.middleware.RequestId`; falls back to the inbound header."""
    rid = getattr(request.state, "request_id", None)
    return rid if rid is not None else request.headers.get(REQUEST_ID_HEADER)


def principal_ctx(principal: Principal) -> TenantCtx:
    return TenantCtx(tenant_id=principal.tenant_id, user_id=principal.user_id, role=principal.role)


def actor_user_id(principal: Principal) -> UUID:
    """The caller's ``users.id``; dev tokens (``sub='dev:<role>'``) cannot own rows → 403."""
    if principal.user_id is None:
        raise AppError("CW-4032", 403, "이 작업에는 사용자 계정 토큰이 필요합니다 (개발 토큰 불가)")
    return principal.user_id


@asynccontextmanager
async def open_tx(request: Request, principal: Principal) -> AsyncIterator[tuple[AppDeps, AsyncSession]]:
    """One transaction under the caller's tenant/user/role GUCs; commits on exit, rolls back on error."""
    deps = get_deps(request)
    async with tenant_tx(deps.engine, principal_ctx(principal)) as session:
        yield deps, session


def not_found(resource: str) -> NotFound:
    """A row invisible under RLS and a row that does not exist look identical: both are 404."""
    return NotFound(resource)


async def load_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    """The caller's tenant row (``kek_ref`` for wrapping/unwrapping, record key for user emails)."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("CW-4033", 403, "비활성 테넌트입니다")
    return tenant


def session_dek(deps: AppDeps, tenant: Tenant, session_row: Any) -> bytes:
    """Unwrap the session DEK through the cache; a purged session (``dek_wrapped IS NULL``) is 410."""
    try:
        return deps.keycache.get(session_row.id, tenant.kek_ref, session_row.dek_wrapped)
    except DekDestroyedError as exc:
        raise AppError("CW-4100", 410, "파기된 세션입니다 (DEK 폐기)") from exc


def as_dict(obj: Any) -> dict[str, Any]:
    return dict(obj)
