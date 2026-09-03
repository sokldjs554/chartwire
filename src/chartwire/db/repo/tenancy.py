"""``tenants`` (owner-only writes: seed, fixtures) and ``users``."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import Tenant, User


async def create_tenant(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    kek_ref: str,
    record_key_wrapped: bytes,
    settings: dict | None = None,
) -> Tenant:
    stmt = (
        insert(Tenant)
        .values(
            slug=slug,
            name=name,
            kek_ref=kek_ref,
            record_key_wrapped=record_key_wrapped,
            settings=settings or {},
        )
        .returning(Tenant)
    )
    return (await session.scalars(stmt)).one()


async def get_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return (await session.scalars(select(Tenant).where(Tenant.slug == slug))).one_or_none()


async def list_active_tenant_ids(session: AsyncSession) -> list[UUID]:
    """Used by the outbox poller and every cross-tenant ticker (ADR-0001: iterate tenants)."""
    return list(
        (await session.scalars(select(Tenant.id).where(Tenant.status == "active").order_by(Tenant.id))).all()
    )


async def create_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    role: str,
    email_hmac: bytes,
    email_enc: bytes,
    display_name: str,
    password_hash: str,
) -> User:
    stmt = (
        insert(User)
        .values(
            tenant_id=tenant_id,
            role=role,
            email_hmac=email_hmac,
            email_enc=email_enc,
            display_name=display_name,
            password_hash=password_hash,
        )
        .returning(User)
    )
    return (await session.scalars(stmt)).one()


async def get_user(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.get(User, user_id)


async def find_user_by_email_hmac(session: AsyncSession, tenant_id: UUID, email_hmac: bytes) -> User | None:
    stmt = select(User).where(User.tenant_id == tenant_id, User.email_hmac == email_hmac)
    return (await session.scalars(stmt)).one_or_none()


async def list_users(session: AsyncSession, tenant_id: UUID, *, limit: int = 200) -> Sequence[User]:
    stmt = select(User).where(User.tenant_id == tenant_id).order_by(User.created_at, User.id).limit(limit)
    return (await session.scalars(stmt)).all()
