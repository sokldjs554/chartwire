"""Engine factories. Runtime processes use the async (asyncpg) engine as ``chartwire_app``;
migrations, the schema dump and fixtures use the sync (psycopg) engine as ``chartwire_owner``."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ASYNC_DRIVER = "postgresql+asyncpg"
SYNC_DRIVER = "postgresql+psycopg"


def _with_driver(url: str | URL, driver: str) -> URL:
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError(f"not a PostgreSQL URL: {parsed.drivername}")
    return parsed.set(drivername=driver)


def as_async_url(url: str | URL) -> URL:
    return _with_driver(url, ASYNC_DRIVER)


def as_sync_url(url: str | URL) -> URL:
    return _with_driver(url, SYNC_DRIVER)


def with_database(url: str | URL, database: str) -> URL:
    """Same server and credentials, different database name (test DBs, ``postgres`` maintenance DB)."""
    return make_url(url).set(database=database)


def make_engine(url: str | URL, *, pool_size: int = 20, max_overflow: int = 10) -> AsyncEngine:
    """Async engine for runtime code. ``pool_reset_on_return='rollback'`` guarantees that a
    transaction-local GUC (``set_config(..., true)``) never survives a connection checkout."""
    return create_async_engine(
        as_async_url(url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_reset_on_return="rollback",
        pool_pre_ping=True,
    )


def make_sync_engine(url: str | URL, *, pool_size: int = 5) -> Engine:
    """Sync engine (psycopg) for Alembic, ``schema-dump`` and the bulk loader."""
    return create_engine(as_sync_url(url), pool_size=pool_size, pool_reset_on_return="rollback")
