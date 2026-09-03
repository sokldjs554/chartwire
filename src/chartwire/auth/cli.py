"""``chartwire token issue`` — developer/device token issuer (§8.1, §13.1).

    chartwire token issue --tenant demo --role clinician [--user <uuid>] [--ttl 15]
    chartwire token issue --tenant-id <uuid> --role recorder

The secret comes from ``CHARTWIRE_JWT_SECRET``. With ``--tenant-id`` no database is
needed at all; with ``--tenant <slug>`` the id is looked up in ``tenants`` through a
plain synchronous psycopg connection (``CHARTWIRE_DATABASE_URL``, app role — the
table has no RLS and grants SELECT). The token is written to stdout only and is
never logged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Final
from uuid import UUID

import typer

from chartwire.auth.deps import JWT_SECRET_ENV
from chartwire.auth.jwt import ROLES, issue

DATABASE_URL_ENV: Final = "CHARTWIRE_DATABASE_URL"
TenantResolver = Callable[[str], UUID | None]

app = typer.Typer(help="JWT 발급 도구 (개발/디바이스용)", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """토큰 명령 그룹 (``chartwire token issue …``)."""


def _sync_dsn(url: str) -> str:
    """``postgresql+asyncpg://…`` (SQLAlchemy form) → ``postgresql://…`` (libpq form)."""
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


def lookup_tenant_id(slug: str, *, database_url: str | None = None) -> UUID | None:
    """Resolve a tenant slug via psycopg (imported lazily so the id path stays DB-free)."""
    url = database_url or os.environ.get(DATABASE_URL_ENV, "")
    if not url:
        raise typer.BadParameter(
            f"--tenant 조회에는 {DATABASE_URL_ENV} 가 필요합니다 (또는 --tenant-id 사용)"
        )
    import psycopg

    with psycopg.connect(_sync_dsn(url)) as conn:
        row = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()
    return UUID(str(row[0])) if row else None


def resolve_tenant(
    tenant: str | None, tenant_id: UUID | None, *, resolver: TenantResolver = lookup_tenant_id
) -> UUID:
    if tenant_id is not None:
        return tenant_id
    if not tenant:
        raise typer.BadParameter("--tenant <slug> 또는 --tenant-id <uuid> 중 하나가 필요합니다")
    resolved = resolver(tenant)
    if resolved is None:
        raise typer.BadParameter(f"테넌트를 찾을 수 없습니다: {tenant}")
    return resolved


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise typer.BadParameter(f"알 수 없는 역할: {role} (허용: {', '.join(sorted(ROLES))})")
    return role


@app.command("issue")
def issue_command(
    role: Annotated[
        str,
        typer.Option(
            "--role", callback=_validate_role, help="clinician|staff|admin|auditor|recorder|service"
        ),
    ],
    tenant: Annotated[str | None, typer.Option("--tenant", help="테넌트 slug (DB 조회)")] = None,
    tenant_id: Annotated[UUID | None, typer.Option("--tenant-id", help="테넌트 UUID (DB 불필요)")] = None,
    user: Annotated[UUID | None, typer.Option("--user", help="사용자 UUID (sub); 생략 시 dev:<role>")] = None,
    ttl: Annotated[int, typer.Option("--ttl", min=1, help="유효 시간(분)")] = 15,
) -> None:
    """HS256 JWT 를 발급해 stdout 에 출력합니다."""
    secret = os.environ.get(JWT_SECRET_ENV, "")
    if not secret:
        raise typer.BadParameter(f"{JWT_SECRET_ENV} 환경변수가 설정되어 있지 않습니다")
    tid = resolve_tenant(tenant, tenant_id)
    sub = str(user) if user is not None else f"dev:{role}"
    token = issue({"sub": sub, "tid": tid, "role": role}, timedelta(minutes=ttl), secret=secret)
    typer.echo(token)
