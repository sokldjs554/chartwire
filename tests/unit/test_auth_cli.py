"""``chartwire token issue``: DB-free with --tenant-id, slug resolution via an injected resolver."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import typer
from typer.testing import CliRunner

from chartwire.auth import cli
from chartwire.auth.deps import JWT_SECRET_ENV
from chartwire.auth.jwt import verify

runner = CliRunner()
SECRET = "cli-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JWT_SECRET_ENV, SECRET)
    monkeypatch.delenv(cli.DATABASE_URL_ENV, raising=False)


def test_issue_with_tenant_id_needs_no_db() -> None:
    tid, uid = uuid4(), uuid4()
    result = runner.invoke(
        cli.app, ["issue", "--tenant-id", str(tid), "--role", "clinician", "--user", str(uid)]
    )
    assert result.exit_code == 0, result.output
    principal = verify(result.output.strip(), secret=SECRET)
    assert (principal.tenant_id, principal.role, principal.user_id) == (tid, "clinician", uid)


def test_issue_without_user_uses_dev_subject_and_ttl() -> None:
    result = runner.invoke(
        cli.app, ["issue", "--tenant-id", str(uuid4()), "--role", "recorder", "--ttl", "1"]
    )
    assert result.exit_code == 0, result.output
    principal = verify(result.output.strip(), secret=SECRET)
    assert principal.sub == "dev:recorder" and principal.user_id is None


def test_issue_rejects_unknown_role_and_missing_tenant() -> None:
    assert runner.invoke(cli.app, ["issue", "--tenant-id", str(uuid4()), "--role", "root"]).exit_code != 0
    result = runner.invoke(cli.app, ["issue", "--role", "admin"])
    assert result.exit_code != 0
    assert "--tenant" in result.output


def test_issue_without_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JWT_SECRET_ENV)
    result = runner.invoke(cli.app, ["issue", "--tenant-id", str(uuid4()), "--role", "admin"])
    assert result.exit_code != 0 and JWT_SECRET_ENV in result.output


def test_resolve_tenant_uses_resolver_for_slug() -> None:
    tid = uuid4()
    seen: list[str] = []

    def resolver(slug: str) -> UUID | None:
        seen.append(slug)
        return tid if slug == "demo" else None

    assert cli.resolve_tenant("demo", None, resolver=resolver) == tid
    with pytest.raises(typer.BadParameter, match="찾을 수 없습니다"):
        cli.resolve_tenant("nope", None, resolver=resolver)
    assert cli.resolve_tenant(None, tid, resolver=resolver) == tid
    assert seen == ["demo", "nope"]


def test_slug_lookup_requires_database_url() -> None:
    with pytest.raises(typer.BadParameter, match=cli.DATABASE_URL_ENV):
        cli.lookup_tenant_id("demo")


def test_sync_dsn_strips_sqlalchemy_driver() -> None:
    assert cli._sync_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert cli._sync_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
