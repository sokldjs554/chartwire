"""``chartwire db ...`` — role bootstrap, migrations, schema dump, partition maintenance.

The functions are importable (``tests/conftest.py`` and ``make`` targets use them); the
typer app at the bottom only parses arguments.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from chartwire.core.config import get_settings
from chartwire.db.engine import as_sync_url, make_sync_engine, with_database

OWNER_ROLE = "chartwire_owner"
APP_ROLE = "chartwire_app"
DEFAULT_DATABASES: tuple[str, ...] = ("chartwire", "chartwire_test")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MONTHLY_PARTITION = re.compile(r"_y\d{4}m\d{2}")


def libpq_url(url: str) -> str:
    """``postgresql+psycopg://`` → plain ``postgresql://`` for psycopg / pg_dump."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


# ---------------------------------------------------------------- bootstrap


def bootstrap_roles(
    superuser_url: str,
    *,
    owner_password: str,
    app_password: str,
    databases: tuple[str, ...] | list[str] = DEFAULT_DATABASES,
) -> list[str]:
    """Idempotently create the two roles and the databases (spec §4.1). Returns a log of actions."""
    log: list[str] = []
    with psycopg.connect(libpq_url(superuser_url), autocommit=True) as conn:
        for role, password, extra in (
            (OWNER_ROLE, owner_password, "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"),
            (APP_ROLE, app_password, "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT"),
        ):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                sql.SQL(f"{verb} ROLE {role} LOGIN {extra} PASSWORD {{}}").format(sql.Literal(password))
            )
            log.append(f"{verb.lower()} role {role}")
        conn.execute(f"REVOKE {OWNER_ROLE} FROM {APP_ROLE}")  # app must never inherit owner rights
        conn.execute(f"ALTER ROLE {APP_ROLE} SET statement_timeout = '5s'")
        for db in databases:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
                conn.execute(f'CREATE DATABASE "{db}" OWNER {OWNER_ROLE}')
                log.append(f"create database {db}")
            conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {APP_ROLE}')
    for db in databases:
        with psycopg.connect(
            libpq_url(with_database(superuser_url, db).render_as_string(False)), autocommit=True
        ) as conn:
            conn.execute(f"SET ROLE {OWNER_ROLE}")  # trusted extensions owned by the owner, not the superuser
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    return log


# ---------------------------------------------------------------- migrations


def alembic_config(owner_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", as_sync_url(owner_url).render_as_string(hide_password=False))
    return cfg


def upgrade(owner_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(owner_url), revision)


def downgrade(owner_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(owner_url), revision)


def current_revision(owner_url: str) -> str | None:
    engine = make_sync_engine(owner_url)
    try:
        with engine.connect() as conn:
            if not conn.execute(text("SELECT to_regclass('alembic_version')")).scalar():
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


# ---------------------------------------------------------------- schema dump

_VOLATILE_LINE = re.compile(r"^(-- Dumped (from|by)|\\(un)?restrict )")


def normalize_schema_dump(raw: str) -> str:
    """Strip pg_dump version comments, ``\\restrict`` tokens and calendar-dependent monthly
    partitions (0003 creates ``current month -1..+2``; the parent + default partition stay)."""
    blocks = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines() if not _VOLATILE_LINE.match(ln)]
        if not lines or MONTHLY_PARTITION.search(block):
            continue
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).rstrip() + "\n"


def schema_dump(owner_url: str) -> str:
    result = subprocess.run(
        ["pg_dump", "--schema-only", "--no-owner", "--dbname", libpq_url(owner_url)],
        check=True,
        capture_output=True,
        text=True,
    )
    return normalize_schema_dump(result.stdout)


# ---------------------------------------------------------------- partitions


def month_start(offset: int, today: date | None = None) -> date:
    today = today or date.today()
    index = today.year * 12 + (today.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def ensure_partitions(url: str, *, months_ahead: int = 2, months_back: int = 0) -> list[str]:
    """``SELECT ensure_segment_partition(m)`` for the requested month window; returns partition names."""
    engine = make_sync_engine(url)
    try:
        with engine.begin() as conn:
            return [
                conn.execute(text("SELECT ensure_segment_partition(:m)"), {"m": month_start(o)}).scalar_one()
                for o in range(-months_back, months_ahead + 1)
            ]
    finally:
        engine.dispose()


# ---------------------------------------------------------------- typer

app = typer.Typer(help="PostgreSQL 역할·마이그레이션·스키마 관리", no_args_is_help=True)
partitions_app = typer.Typer(help="transcript_segments 월별 파티션 관리", no_args_is_help=True)
app.add_typer(partitions_app, name="partitions")

OwnerUrl = Annotated[str | None, typer.Option("--owner-url", help="기본값: CHARTWIRE_DATABASE_OWNER_URL")]


def _owner(url: str | None) -> str:
    return url or get_settings().database_owner_url


@app.command("bootstrap-roles")
def cmd_bootstrap_roles(
    superuser_url: Annotated[str | None, typer.Option(help="기본값: CHARTWIRE_SUPERUSER_URL")] = None,
    database: Annotated[
        list[str] | None, typer.Option("--database", "-d", help="생성할 DB (반복 가능)")
    ] = None,
) -> None:
    """chartwire_owner / chartwire_app 역할과 데이터베이스를 멱등하게 생성합니다 (superuser 필요)."""
    settings = get_settings()
    su = superuser_url or settings.superuser_url
    if not su:
        raise typer.BadParameter("superuser URL이 필요합니다 (CHARTWIRE_SUPERUSER_URL)")
    for line in bootstrap_roles(
        su,
        owner_password=settings.owner_password,
        app_password=settings.app_password,
        databases=tuple(database) if database else DEFAULT_DATABASES,
    ):
        typer.echo(line)


@app.command("upgrade")
def cmd_upgrade(revision: Annotated[str, typer.Argument()] = "head", owner_url: OwnerUrl = None) -> None:
    """Alembic upgrade (owner 역할로 실행)."""
    upgrade(_owner(owner_url), revision)
    typer.echo(f"revision: {current_revision(_owner(owner_url))}")


@app.command("downgrade")
def cmd_downgrade(revision: Annotated[str, typer.Argument()] = "base", owner_url: OwnerUrl = None) -> None:
    """Alembic downgrade (기본 base)."""
    downgrade(_owner(owner_url), revision)
    typer.echo(f"revision: {current_revision(_owner(owner_url))}")


@app.command("schema-dump")
def cmd_schema_dump(
    out: Annotated[Path, typer.Option(help="출력 파일")] = Path("docs/db/schema.sql"),
    check: Annotated[bool, typer.Option("--check", help="파일과 비교만 하고 다르면 실패")] = False,
    owner_url: OwnerUrl = None,
) -> None:
    """pg_dump --schema-only --no-owner (변동 라인 제거) 를 기록하거나 비교합니다."""
    dump = schema_dump(_owner(owner_url))
    if check:
        if out.read_text() != dump:
            typer.echo(f"schema drift: {out} differs from the live schema", err=True)
            raise typer.Exit(1)
        typer.echo("schema matches")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump)
    typer.echo(f"wrote {out} ({len(dump.splitlines())} lines)")


@partitions_app.command("ensure")
def cmd_partitions_ensure(
    months_ahead: Annotated[int, typer.Option(min=0)] = 2,
    months_back: Annotated[int, typer.Option(min=0)] = 0,
    owner_url: OwnerUrl = None,
) -> None:
    """현재 달부터 --months-ahead 달까지 파티션(및 파티션 인덱스)을 보장합니다."""
    for name in ensure_partitions(_owner(owner_url), months_ahead=months_ahead, months_back=months_back):
        typer.echo(name)
