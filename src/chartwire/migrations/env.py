"""Alembic runtime: sync psycopg as ``chartwire_owner``, one transaction per migration."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from chartwire.db.engine import as_sync_url
from chartwire.db.models import Base

config = context.config
target_metadata = Base.metadata


def _owner_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("CHARTWIRE_DATABASE_OWNER_URL")
    if not url:
        raise RuntimeError("set CHARTWIRE_DATABASE_OWNER_URL (or sqlalchemy.url) for migrations")
    return as_sync_url(url).render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_owner_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _owner_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
