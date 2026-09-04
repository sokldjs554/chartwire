"""``POST /v1/users`` · ``GET /v1/users`` (admin, tenant-scoped).

E-mail is stored twice: ``email_hmac`` (blind index, login lookup) and ``email_enc`` under the tenant
record key with AAD bound to the blind index — so a ciphertext copied onto another user's row does not
decrypt (§8.2). Passwords are scrypt hashes (§8.1).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.exc import IntegrityError

from chartwire.api.deps import AppDeps, load_tenant, open_tx, request_id
from chartwire.api.schemas import UserCreate, UserOut
from chartwire.audit import service as audit
from chartwire.auth import passwords
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.core.errors import Conflict
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, aad
from chartwire.crypto.errors import CryptoError
from chartwire.db.models import Tenant, User
from chartwire.db.repo import tenancy as tenancy_repo

router = APIRouter(prefix="/v1", tags=["users"])


def _email_aad(tenant: Tenant, email_hmac: bytes) -> str:
    return aad(tenant.id, "user", email_hmac.hex(), "email")


def _record_key(deps: AppDeps, tenant: Tenant) -> bytes:
    return deps.keycache.get(tenant.id, tenant.kek_ref, tenant.record_key_wrapped)


def _user_out(user: User, email: str | None) -> UserOut:
    return UserOut(
        id=user.id,
        role=user.role,
        email=email,
        display_name=user.display_name,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, request: Request, principal: Principal = Depends(require("admin"))
) -> UserOut:
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        email_hmac = blind_index(deps.settings.kek_master_bytes, tenant.id, body.email)
        email_enc = Envelope.encrypt(
            _record_key(deps, tenant), body.email.encode("utf-8"), _email_aad(tenant, email_hmac)
        )
        try:
            user = await tenancy_repo.create_user(
                s,
                tenant_id=tenant.id,
                role=body.role,
                email_hmac=email_hmac,
                email_enc=email_enc,
                display_name=body.display_name,
                password_hash=passwords.hash(body.password),
            )
        except IntegrityError as exc:
            raise Conflict("같은 이메일의 사용자가 이미 있습니다") from exc
        await audit.record(
            s,
            tenant_id=tenant.id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="user.created",
            resource_type="user",
            resource_id=user.id,
            request_id=request_id(request),
            detail={"role": user.role},
        )
        return _user_out(user, body.email)


@router.get("/users", response_model=list[UserOut])
async def list_users(request: Request, principal: Principal = Depends(require("admin"))) -> list[UserOut]:
    async with open_tx(request, principal) as (deps, s):
        tenant = await load_tenant(s, principal.tenant_id)
        rows = await tenancy_repo.list_users(s, tenant.id)
        key = _record_key(deps, tenant)
        out: list[UserOut] = []
        for user in rows:
            try:
                email: str | None = Envelope.decrypt(
                    key, bytes(user.email_enc), _email_aad(tenant, bytes(user.email_hmac))
                ).decode("utf-8")
            except (CryptoError, UnicodeDecodeError):
                email = None  # seeded placeholders / rotated keys: never fail the listing
            out.append(_user_out(user, email))
        return out
