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


HIDE_PARAMETERS = True
"""Spec §0.9: never log transcript text. A ``DBAPIError`` renders its bound parameters into
``str(exc)`` — and ``segment_search.text`` is the *plaintext* utterance, while the ``/v1/search``
statement is bound with the clinician's free-text query. Those exception strings reach the logs
verbatim (``stt/worker.py`` ``log.exception``, ``api/app.py`` ``exc_info=exc``), so the engine hides
them: SQLAlchemy renders ``[SQL parameters hidden due to hide_parameters=True]`` and keeps the SQL
text and error class, which is all those sites diagnose with."""


def make_engine(url: str | URL, *, pool_size: int = 20, max_overflow: int = 10) -> AsyncEngine:
    """Async engine for runtime code. ``pool_reset_on_return='rollback'`` guarantees that a
    transaction-local GUC (``set_config(..., true)``) never survives a connection checkout."""
    return create_async_engine(
        as_async_url(url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_reset_on_return="rollback",
        pool_pre_ping=True,
        hide_parameters=HIDE_PARAMETERS,
    )


def make_sync_engine(url: str | URL, *, pool_size: int = 5) -> Engine:
    """Sync engine (psycopg) for Alembic, ``schema-dump`` and the bulk loader (which also writes
    plaintext ``segment_search`` rows — hence the same parameter hiding)."""
    return create_engine(
        as_sync_url(url),
        pool_size=pool_size,
        pool_reset_on_return="rollback",
        hide_parameters=HIDE_PARAMETERS,
    )
