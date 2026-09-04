"""``POST /v1/auth/token`` (scrypt login → 15 min HS256 JWT, audit ``auth.login``) and ``GET /v1/me``."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import get_deps, request_id
from chartwire.api.schemas import MeOut, TokenRequest, TokenResponse
from chartwire.audit import service as audit
from chartwire.auth import jwt, passwords
from chartwire.auth.deps import current_principal
from chartwire.auth.jwt import Principal
from chartwire.core.errors import AppError
from chartwire.crypto.blind_index import blind_index
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx

router = APIRouter(prefix="/v1", tags=["auth"])

ACCESS_TTL: Final = timedelta(minutes=15)
_DUMMY_HASH: Final = passwords.hash("chartwire-timing-equaliser")
"""Verified against when the tenant or user does not exist, so a wrong slug/email costs the same
scrypt work as a wrong password (no account enumeration by timing)."""


def _rejected() -> AppError:
    return AppError("CW-4010", 401, "테넌트, 이메일 또는 비밀번호가 올바르지 않습니다")


@router.post("/auth/token", response_model=TokenResponse)
async def login(body: TokenRequest, request: Request) -> TokenResponse:
    deps = get_deps(request)
    async with AsyncSession(deps.engine) as s:  # ``tenants`` has no RLS: readable without context
        tenant = await tenancy_repo.get_tenant_by_slug(s, body.tenant_slug)
    if tenant is None or tenant.status != "active":
        passwords.verify(body.password, _DUMMY_HASH)
        raise _rejected()
    email_hmac = blind_index(deps.settings.kek_master_bytes, tenant.id, body.email)
    async with tenant_tx(deps.engine, TenantCtx.service(tenant.id)) as s:
        user = await tenancy_repo.find_user_by_email_hmac(s, tenant.id, email_hmac)
        if user is None or not user.is_active:
            passwords.verify(body.password, _DUMMY_HASH)
            raise _rejected()
        if not passwords.verify(body.password, user.password_hash):
            raise _rejected()
        token = jwt.issue(
            {"sub": str(user.id), "tid": str(tenant.id), "role": user.role},
            ACCESS_TTL,
            secret=deps.settings.jwt_secret,
            clock=deps.clock,
        )
        await audit.record(
            s,
            tenant_id=tenant.id,
            actor_id=user.id,
            actor_role=user.role,
            action="auth.login",
            resource_type="user",
            resource_id=user.id,
            request_id=request_id(request),
        )
    return TokenResponse(access_token=token, expires_in=int(ACCESS_TTL.total_seconds()))


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(current_principal)) -> MeOut:
    return MeOut(
        sub=principal.sub,
        tenant_id=principal.tenant_id,
        role=principal.role,
        user_id=principal.user_id,
        exp=principal.exp,
    )
