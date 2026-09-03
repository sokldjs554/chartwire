"""Handler runtime shared by the worker, the CLI and the tests (§7.1).

* :func:`build_context` / :func:`context_from_deps` assemble the :class:`HandlerContext` every handler
  receives (engine as ``chartwire_app``, Redis, object store, clock, KEK, DEK cache).
* :func:`run_handler` is the *one* place a handler is executed: it opens the handler transaction under
  ``app.tenant_id`` / ``app.role='service'``, inserts the ``processed_events`` row first (a PK conflict
  means another delivery already committed the effects → skip), then runs the handler with that
  transaction bound so ``HandlerContext.tenant_tx`` joins it, and commits everything together.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chartwire.core.clock import SystemClock
from chartwire.core.config import Settings
from chartwire.crypto.kek import LocalKek
from chartwire.crypto.keycache import KeyCache
from chartwire.db.engine import make_engine
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore import from_spec
from chartwire.outbox.context import Clock, HandlerContext, OutboxEvent, bind_tx
from chartwire.outbox.registry import HandlerSpec
from chartwire.redis.client import get_redis

Outcome = Literal["processed", "skipped"]

EMBEDDED_POOL_SIZE = 5
"""``serve all --embedded`` (Render free tier, §12.2): one small pool for the whole process."""


def build_context(
    settings: Settings, *, clock: Clock | None = None, pool_size: int | None = None
) -> HandlerContext:
    """Dependencies for a standalone worker process. ``pool_size`` defaults to 20 (5 when embedded)."""
    if pool_size is None:
        pool_size = EMBEDDED_POOL_SIZE if settings.embedded else 20
    engine = make_engine(
        settings.database_url, pool_size=pool_size, max_overflow=0 if settings.embedded else 10
    )
    kek = LocalKek(settings.kek_master_bytes)
    return HandlerContext(
        engine=engine,
        redis=get_redis(settings.redis_url),
        objectstore=from_spec(settings.objectstore),
        clock=clock or SystemClock(),
        settings=settings,
        kek=kek,
        keycache=KeyCache(kek),
    )


def context_from_deps(deps: Any) -> HandlerContext:
    """Share the api process's ``AppDeps`` (WP-E) when the worker runs embedded in the api."""
    return HandlerContext(
        engine=deps.engine,
        redis=deps.redis,
        objectstore=deps.objectstore,
        clock=deps.clock,
        settings=deps.settings,
        kek=deps.kek,
        keycache=deps.keycache,
    )


async def close_context(ctx: HandlerContext) -> None:
    """Dispose the pool and the Redis client (only for contexts built by :func:`build_context`)."""
    engine: AsyncEngine = ctx.engine
    await engine.dispose()
    if ctx.redis is not None:
        await ctx.redis.aclose()


async def active_tenant_ids(engine: AsyncEngine) -> list[UUID]:
    """``tenants`` carries no RLS and is readable by ``chartwire_app``; no tenant context needed."""
    async with AsyncSession(engine) as session:
        return await tenancy_repo.list_active_tenant_ids(session)


async def run_handler(ctx: HandlerContext, spec: HandlerSpec, event: OutboxEvent) -> Outcome:
    """Execute ``spec.fn`` for ``event`` inside one tenant transaction (§7.1 handler execution).

    Returns ``"skipped"`` when ``processed_events(handler, event_id)`` already exists — a previous
    delivery committed the effects but crashed before ``mark_done``. Any exception propagates after the
    transaction (handler effects *and* the ledger row) has been rolled back, so a retry starts clean.
    A concurrent duplicate delivery blocks on the ledger's primary key until this transaction commits
    or rolls back, which is why no lease heartbeat is needed for correctness.
    """
    async with tenant_tx(ctx.engine, TenantCtx.service(event.tenant_id)) as session:
        fresh = await outbox_repo.mark_processed(
            session, handler=spec.name, event_id=event.id, tenant_id=event.tenant_id
        )
        if not fresh:
            return "skipped"
        with bind_tx(event.tenant_id, session):
            await spec.fn(ctx, event)
    return "processed"
