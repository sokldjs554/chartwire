"""Shared fixtures (spec §4.4).

Isolation: every work package runs against its own database (``CHARTWIRE_TEST_DB``) and Redis
index (``CHARTWIRE_TEST_REDIS_DB``). The session-scoped ``migrated_db`` fixture creates the
database (superuser URL, idempotent), then runs ``downgrade base`` + ``upgrade head`` as the
owner so every session starts from a schema built purely by the migrations.

Engines are function-scoped and disposed after each test: pytest-asyncio gives each test its
own event loop and asyncpg connections are bound to the loop that created them.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from chartwire.core.clock import FakeClock
from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.db import cli as dbcli
from chartwire.db.engine import make_engine, with_database
from chartwire.db.models import TENANT_TABLES, Patient, Tenant, User
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.redis.client import with_db

# --------------------------------------------------------------------------- urls


@dataclass(frozen=True)
class DbUrls:
    name: str
    owner: str
    app: str
    superuser: str | None
    redis: str


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def db_urls(settings: Settings) -> DbUrls:
    name = settings.test_db
    su = settings.superuser_url
    return DbUrls(
        name=name,
        owner=with_database(settings.database_owner_url, name).render_as_string(hide_password=False),
        app=with_database(settings.database_url, name).render_as_string(hide_password=False),
        superuser=with_database(su, name).render_as_string(hide_password=False) if su else None,
        redis=with_db(settings.redis_url, settings.test_redis_db),
    )


@pytest.fixture(scope="session")
def migrated_db(settings: Settings, db_urls: DbUrls) -> DbUrls:
    """Create the test database if missing and rebuild the schema from base to head."""
    if settings.superuser_url:
        dbcli.bootstrap_roles(
            settings.superuser_url,
            owner_password=settings.owner_password,
            app_password=settings.app_password,
            databases=[db_urls.name],
        )
    if os.environ.get("CHARTWIRE_TEST_KEEP_SCHEMA") != "1":
        dbcli.downgrade(db_urls.owner, "base")
    dbcli.upgrade(db_urls.owner, "head")
    return db_urls


# --------------------------------------------------------------------------- engines


async def _engine(url: str, **kw: int) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(url, **kw)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def owner_engine(migrated_db: DbUrls) -> AsyncIterator[AsyncEngine]:
    async for engine in _engine(migrated_db.owner, pool_size=5):
        yield engine


@pytest.fixture
async def app_engine(migrated_db: DbUrls) -> AsyncIterator[AsyncEngine]:
    """``chartwire_app`` (NOBYPASSRLS) — what every runtime process uses."""
    async for engine in _engine(migrated_db.app, pool_size=5):
        yield engine


@pytest.fixture
async def su_engine(migrated_db: DbUrls) -> AsyncIterator[AsyncEngine]:
    """Superuser: only ``test_superuser_leaks.py`` and the perf study may use it."""
    if migrated_db.superuser is None:
        pytest.skip("CHARTWIRE_SUPERUSER_URL not set")
    async for engine in _engine(migrated_db.superuser, pool_size=2):
        yield engine


@pytest.fixture
async def clean_db(owner_engine: AsyncEngine) -> AsyncEngine:
    """Truncate every tenant table (+ ``tenants``) as owner. TRUNCATE ignores RLS."""
    tables = ", ".join((*TENANT_TABLES, "tenants"))
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return owner_engine


# --------------------------------------------------------------------------- factories


class Factories:
    """Synthetic tenants/users/patients/sessions. Names are fixed placeholders — no real PHI."""

    def __init__(self, owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
        self._owner = async_sessionmaker(owner_engine, expire_on_commit=False)
        self._app = app_engine
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    async def tenant(self, slug: str) -> Tenant:
        async with self._owner() as session, session.begin():
            return await tenancy_repo.create_tenant(
                session,
                slug=slug,
                name=f"가상의원-{slug}",
                kek_ref=f"local:{slug}",
                record_key_wrapped=bytes(48),
            )

    async def user(self, tenant_id: UUID, role: str = "clinician", display_name: str | None = None) -> User:
        n = self._next()
        async with tenant_tx(self._app, TenantCtx.service(tenant_id)) as session:
            return await tenancy_repo.create_user(
                session,
                tenant_id=tenant_id,
                role=role,
                email_hmac=hashlib.sha256(f"{tenant_id}:{role}:{n}".encode()).digest(),
                email_enc=bytes(16),
                display_name=display_name or f"가상{role}-{n:03d}",
                password_hash="scrypt$synthetic",
            )

    async def patient(self, tenant_id: UUID) -> Patient:
        n = self._next()
        async with tenant_tx(self._app, TenantCtx.service(tenant_id)) as session:
            return await patients_repo.create_patient(
                session,
                tenant_id=tenant_id,
                pseudonym=f"가상환자-{n:04d}",
                name_enc=bytes(32),
                name_hmac=hashlib.sha256(f"name:{tenant_id}:{n}".encode()).digest(),
                birth_year=1990,
                sex="F",
                phone_enc=None,
                dek_wrapped=bytes(60),
                dek_fingerprint=hashlib.sha256(bytes(60)).digest(),
            )

    async def session(
        self,
        tenant_id: UUID,
        patient_id: UUID,
        clinician_id: UUID,
        *,
        started_at: datetime | None = None,
    ) -> SessionModel:
        started_at = started_at or datetime.now(tz=UTC)
        async with tenant_tx(self._app, TenantCtx.service(tenant_id)) as session:
            created = await sessions_repo.create_session(
                session,
                id=uuid7(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                clinician_id=clinician_id,
                script_ref="s01",
                scopes_snapshot=["recording", "transcription", "ai_drafting", "search_index"],
                dek_wrapped=bytes(60),
                dek_fingerprint=hashlib.sha256(bytes(60)).digest(),
            )
            updated = await sessions_repo.set_state(session, created.id, "recording", now=started_at)
            assert updated is not None
            return updated


@pytest.fixture
async def factories(clean_db: AsyncEngine, app_engine: AsyncEngine) -> Factories:
    return Factories(clean_db, app_engine)


@pytest.fixture
async def tenant_a(factories: Factories) -> Tenant:
    return await factories.tenant("clinic-a")


@pytest.fixture
async def tenant_b(factories: Factories) -> Tenant:
    return await factories.tenant("clinic-b")


@pytest.fixture
async def clinician_a(factories: Factories, tenant_a: Tenant) -> User:
    return await factories.user(tenant_a.id, "clinician")


@pytest.fixture
async def clinician_b(factories: Factories, tenant_b: Tenant) -> User:
    return await factories.user(tenant_b.id, "clinician")


@pytest.fixture
async def patient_a(factories: Factories, tenant_a: Tenant) -> Patient:
    return await factories.patient(tenant_a.id)


@pytest.fixture
async def patient_b(factories: Factories, tenant_b: Tenant) -> Patient:
    return await factories.patient(tenant_b.id)


@pytest.fixture
async def session_a(
    factories: Factories, tenant_a: Tenant, patient_a: Patient, clinician_a: User
) -> SessionModel:
    return await factories.session(tenant_a.id, patient_a.id, clinician_a.id)


@pytest.fixture
async def session_b(
    factories: Factories, tenant_b: Tenant, patient_b: Patient, clinician_b: User
) -> SessionModel:
    return await factories.session(tenant_b.id, patient_b.id, clinician_b.id)


@pytest.fixture
def ctx_a(tenant_a: Tenant, clinician_a: User) -> TenantCtx:
    return TenantCtx(tenant_id=tenant_a.id, user_id=clinician_a.id, role="clinician")


@pytest.fixture
def ctx_b(tenant_b: Tenant, clinician_b: User) -> TenantCtx:
    return TenantCtx(tenant_id=tenant_b.id, user_id=clinician_b.id, role="clinician")


# --------------------------------------------------------------------------- redis / clock


@pytest.fixture
async def redis(db_urls: DbUrls) -> AsyncIterator[Redis]:
    """Client on this work package's Redis index; flushed (FLUSHDB, never FLUSHALL) before and after."""
    client = Redis.from_url(db_urls.redis, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 1, 9, 0, tzinfo=UTC))


# --------------------------------------------------------------------------- helpers


def owner_session(engine: AsyncEngine) -> AsyncSession:
    """Plain owner session (no tenant context) for tests that need to bypass nothing but write ``tenants``."""
    return async_sessionmaker(engine, expire_on_commit=False)()
