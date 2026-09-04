"""Live-service scaffolding for the two *executing* eval reports (`purge`, `rls`, spec §11.1).

Both need what the integration suites need — a migrated database, a Redis index, an object store,
a tenant with a real record key, patients with real DEKs and fully populated sessions — so this
module **reuses the WP-A/WP-E fixtures** (``tests.conftest.Factories``,
``tests.integration.api_support``) instead of re-implementing them: one seeding path means the
eval measures the same rows the integration tests assert on. The import is lazy and explained by
:class:`FixturesUnavailable` when the repository's ``tests`` package is not importable (a wheel
install), in which case the eval is skipped and the README row is deleted (§0.2).

Isolation: the database is ``CHARTWIRE_TEST_DB`` (WP-F: ``chartwire_test_f``) and the Redis index
is ``CHARTWIRE_TEST_REDIS_DB`` (6) — never the dev database. All data is synthetic (§0.1).
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.config import Settings
from chartwire.db import cli as dbcli
from chartwire.db.engine import make_engine, with_database
from chartwire.db.models import TENANT_TABLES
from chartwire.objectstore.localfs import LocalFs
from chartwire.redis.client import with_db


class FixturesUnavailable(RuntimeError):
    """The repository fixtures are not importable — the report cannot be produced here."""


def repo_root() -> Path | None:
    """The checkout that holds ``tests/`` — the cwd, or the parent of the installed ``src/``."""
    here = Path(__file__).resolve()
    for candidate in (Path.cwd(), *here.parents):
        if (candidate / "tests" / "integration" / "api_support.py").is_file():
            return candidate
    return None


def fixtures() -> Any:
    """``tests.integration.api_support`` (+ ``tests.conftest``), imported lazily.

    The console script does not put the working directory on ``sys.path``, so the checkout that
    owns ``tests/`` is located first (editable install → the parent of ``src/``).
    """
    return import_fixture("tests.integration.api_support"), import_fixture("tests.conftest")


def import_fixture(dotted: str) -> Any:
    """Import one repository fixture module by name (dynamic so type checking stops at the boundary)."""
    root = repo_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:  # pragma: no cover - only outside a source checkout
        raise FixturesUnavailable(
            f"{dotted} 를 임포트할 수 없습니다 — 리포지터리 소스 체크아웃에서 실행하세요"
        ) from exc


@dataclass
class EvalEnv:
    """Everything a live eval run needs; ``deps`` is what ``create_app`` would build."""

    settings: Settings
    owner_engine: AsyncEngine
    app_engine: AsyncEngine
    redis: Redis
    objectstore: LocalFs
    workdir: Path
    deps: Any
    factories: Any
    tenant: Any = None
    """Set by the caller once it has created the eval tenant (kept here so helpers can find it)."""

    @property
    def db_name(self) -> str:
        return self.settings.test_db


def _urls(settings: Settings) -> tuple[str, str, str]:
    name = settings.test_db
    owner = with_database(settings.database_owner_url, name).render_as_string(hide_password=False)
    app = with_database(settings.database_url, name).render_as_string(hide_password=False)
    return name, owner, app


def bootstrap(settings: Settings) -> tuple[str, str]:
    """Create the eval database if missing and rebuild its schema from base to head."""
    name, owner, app = _urls(settings)
    if settings.superuser_url:
        dbcli.bootstrap_roles(
            settings.superuser_url,
            owner_password=settings.owner_password,
            app_password=settings.app_password,
            databases=[name],
        )
    dbcli.downgrade(owner, "base")
    dbcli.upgrade(owner, "head")
    return owner, app


@asynccontextmanager
async def eval_env(settings: Settings, *, migrate: bool = True) -> AsyncIterator[EvalEnv]:
    """Migrated database + clean tenant tables + Redis index + a temp object store."""
    api_support, conftest = fixtures()
    if migrate:
        owner_url, app_url = bootstrap(settings)
    else:
        _, owner_url, app_url = _urls(settings)
    owner_engine = make_engine(owner_url, pool_size=5)
    app_engine = make_engine(app_url, pool_size=5)
    redis = Redis.from_url(with_db(settings.redis_url, settings.test_redis_db), decode_responses=True)
    tmp = Path(tempfile.mkdtemp(prefix="chartwire-eval-"))
    try:
        async with owner_engine.begin() as conn:
            tables = ", ".join((*TENANT_TABLES, "tenants"))
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await redis.flushdb()  # our own index only (AGENT_ENV: never FLUSHALL)
        objectstore = LocalFs(tmp)
        deps = api_support.make_deps(app_engine, redis, settings, objectstore)
        yield EvalEnv(
            settings=settings,
            owner_engine=owner_engine,
            app_engine=app_engine,
            redis=redis,
            objectstore=objectstore,
            workdir=tmp,
            deps=deps,
            factories=conftest.Factories(owner_engine, app_engine),
        )
    finally:
        await redis.flushdb()
        await redis.aclose()
        await app_engine.dispose()
        await owner_engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
