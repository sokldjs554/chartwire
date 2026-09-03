"""``/healthz`` · ``/readyz`` · ``/metrics`` and the drain wiring for any chartwire process (§6.9, §7.3).

``on_startup(app, deps)`` is called from the api lifespan (WP-E ``create_app``) and by the worker's own
small metrics server; it attaches one :class:`Health` and one :class:`Drainer` to ``app.state`` (reusing
instances another module put there first, e.g. ``ws.routes`` or the embedded runner) and routes
SIGTERM/SIGINT to the drainer. Under uvicorn the signal previously belonged to ``Server.handle_exit``;
that handler is invoked again once the drain finishes (or its deadline passes), so the choreography is
SIGTERM → ``/readyz`` 503 + hooks → in-flight work ≤ deadline → uvicorn shutdown → exit.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from chartwire.ops.drain import DEFAULT_SIGNALS, Drainer
from chartwire.ops.health import Health
from chartwire.ops.metrics import bind_db_pool, render

log = logging.getLogger(__name__)

API_DRAIN_DEADLINE_S = 20.0
"""§6.4 step 7: the api flushes ledgers and closes recorders within 20 s (the worker has 25 s)."""

router = APIRouter(tags=["ops"])


def get_health(request: Request) -> Health:
    return request.app.state.health  # type: ignore[no-any-return]


def get_drainer(request: Request) -> Drainer:
    return request.app.state.drainer  # type: ignore[no-any-return]


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> JSONResponse:
    """Liveness: always 200 while the process runs, even during drain (container HEALTHCHECK)."""
    code, body = get_health(request).livez()
    return JSONResponse(body, status_code=code)


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: PostgreSQL + Redis probes; 503 while draining or when a probe fails."""
    code, body = await get_health(request).readyz()
    return JSONResponse(body, status_code=code)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    body, content_type = render()
    return Response(content=body, media_type=content_type)


# --- wiring -------------------------------------------------------------------------------------------


def pg_check(engine: Any) -> Callable[[], Any]:
    async def check() -> bool:
        async with engine.connect() as conn:
            return int((await conn.execute(text("SELECT 1"))).scalar_one()) == 1

    return check


def redis_check(redis: Any) -> Callable[[], Any]:
    async def check() -> bool:
        return bool(await redis.ping())

    return check


def build_health(deps: Any, *, role: str) -> Health:
    """``Health`` with the two dependency probes; ``deps`` needs ``engine``, ``redis``, ``settings``."""
    settings = getattr(deps, "settings", None)
    info = {
        "role": role,
        "node_id": str(getattr(settings, "node_id", "")),
        "dev_secrets": str(bool(getattr(settings, "uses_dev_secrets", False))).lower(),
    }
    health = Health(info=info)
    if getattr(deps, "engine", None) is not None:
        health.add_check("postgres", pg_check(deps.engine))
    if getattr(deps, "redis", None) is not None:
        health.add_check("redis", redis_check(deps.redis))
    return health


def on_startup(
    app: FastAPI,
    deps: Any,
    *,
    role: str = "api",
    deadline_s: float = API_DRAIN_DEADLINE_S,
    install_signals: bool = True,
) -> None:
    """Attach health + drainer to ``app.state`` and route SIGTERM/SIGINT to the drainer."""
    drainer: Drainer | None = getattr(app.state, "drainer", None)
    if drainer is None:
        drainer = Drainer(deadline_s=deadline_s)
        app.state.drainer = drainer
    health: Health | None = getattr(app.state, "health", None)
    if health is None:
        health = build_health(deps, role=role)
        app.state.health = health
    drainer.on_begin(health.mark_draining)
    engine = getattr(deps, "engine", None)
    pool = getattr(engine, "pool", None)
    if pool is not None and hasattr(pool, "checkedout"):
        bind_db_pool(pool.checkedout)
    if install_signals:
        chain_signals(drainer)


async def on_shutdown(app: FastAPI) -> None:
    drainer: Drainer | None = getattr(app.state, "drainer", None)
    if drainer is not None:
        drainer.uninstall()


def chain_signals(drainer: Drainer) -> bool:
    """Route signals to ``drainer.begin`` and re-invoke the previous handler (uvicorn) after the drain.

    Returns ``False`` when handlers cannot be installed (not the main thread, e.g. under TestClient).
    """
    previous = {sig: signal.getsignal(sig) for sig in DEFAULT_SIGNALS}
    try:
        drainer.install()
    except (RuntimeError, ValueError, NotImplementedError):
        log.info("signal handlers not installed (not the main thread)")
        return False

    async def finish() -> None:
        await drainer.wait_drained()
        for sig, handler in previous.items():
            if _chainable(handler):
                handler(sig, None)

    def on_begin() -> None:
        task = asyncio.get_running_loop().create_task(finish(), name="drain-finish")
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    drainer.on_begin(on_begin)
    return True


_BACKGROUND: set[asyncio.Task[None]] = set()


def _chainable(handler: object) -> bool:
    """Only a real previous handler (uvicorn's) is re-invoked — never SIG_DFL/SIG_IGN, and never
    Python's ``default_int_handler``, whose ``KeyboardInterrupt`` would abort the drained process."""
    return callable(handler) and handler is not signal.default_int_handler


def standalone_app(deps: Any, *, role: str, drainer: Drainer, health: Health | None = None) -> FastAPI:
    """Minimal app for processes without a REST API (worker, stt-worker): the three ops routes only."""
    app = FastAPI(title=f"chartwire {role} ops", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.drainer = drainer
    app.state.health = health or build_health(deps, role=role)
    app.include_router(router)
    on_startup(app, deps, role=role, install_signals=False)
    return app
