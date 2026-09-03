"""The worker process (§7): outbox poller + tickers + a small ops HTTP server, with graceful drain.

``run(settings)`` is ``chartwire serve worker``; ``run_all(settings, host, port)`` is
``chartwire serve all --embedded`` (api + worker + stt-worker as tasks in one process, one pool of 5).

Handler modules are imported here so their ``@registry.handler`` decorators run; modules another
work package has not written yet are skipped with a log line, never an ImportError — the worker
still serves the events whose handlers exist, and unknown event types wait in the queue (retry →
DLQ after ``max_attempts``, replayable once the handler ships).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

import uvicorn

from chartwire.core.config import Settings
from chartwire.ops.drain import DEFAULT_DEADLINE_S, Drainer
from chartwire.ops.health import Health
from chartwire.ops.routes import build_health, chain_signals, standalone_app
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.poller import Poller
from chartwire.outbox.runtime import build_context, close_context, context_from_deps
from chartwire.worker.tickers import Ticker

log = logging.getLogger(__name__)

HANDLER_MODULES: tuple[str, ...] = (
    "note_draft",  # WP-D: session.transcribed
    "purge_run",  # WP-E: consent.revoked, purge.requested
    "purge_verify",  # WP-E: purge.completed
    "alert_sla",  # WP-C: SLA ticker
    "partition_ensure",
    "outbox_prune",
    "session_reaper",
)
ALERT_SLA_INTERVAL_S = 1.0
DEFAULT_HTTP_PORT = 9001
WORKER_PORT_ENV = "CHARTWIRE_WORKER_PORT"


def load_handlers(names: tuple[str, ...] = HANDLER_MODULES) -> dict[str, ModuleType | None]:
    """Import ``chartwire.worker.handlers.<name>`` for each name; ``None`` when the module is absent."""
    loaded: dict[str, ModuleType | None] = {}
    for name in names:
        path = f"chartwire.worker.handlers.{name}"
        try:
            loaded[name] = importlib.import_module(path)
        except ModuleNotFoundError as exc:
            if exc.name and path.startswith(exc.name):
                loaded[name] = None
                log.info("handler module not present; skipped", extra={"module": name})
            else:
                raise
    return loaded


TickFn = Callable[[], Awaitable[object]]


def _tick_fn(module: ModuleType, ctx: HandlerContext) -> TickFn:
    async def tick() -> object:
        return await module.tick(ctx)

    return tick


def _alert_sla_fn(ctx: HandlerContext, module: ModuleType | None) -> TickFn | None:
    """``worker.handlers.alert_sla.tick(ctx)`` if WP-C shipped it, else ``risk.alerts.escalate_due``."""
    if module is not None and hasattr(module, "tick"):
        return _tick_fn(module, ctx)
    try:
        alerts = importlib.import_module("chartwire.risk.alerts")
    except ModuleNotFoundError:
        return None
    escalate = getattr(alerts, "escalate_due", None)
    if escalate is None:
        return None

    async def tick() -> object:
        return await escalate(ctx, ctx.clock.now())

    return tick


def build_tickers(
    ctx: HandlerContext, modules: dict[str, ModuleType | None], drainer: Drainer
) -> list[Ticker]:
    tickers: list[Ticker] = []
    if not modules:  # nothing loaded (tests, ``handler_modules=()``): no tickers either
        return tickers
    sla = _alert_sla_fn(ctx, modules.get("alert_sla"))
    if sla is not None:
        tickers.append(Ticker("alert_sla", ALERT_SLA_INTERVAL_S, sla, drainer=drainer))
    else:
        log.warning("alert_sla ticker unavailable (no handler module, no risk.alerts.escalate_due)")
    for name, at_start in (("partition_ensure", True), ("outbox_prune", False), ("session_reaper", False)):
        module = modules.get(name)
        if module is None:
            continue
        tickers.append(
            Ticker(
                name, float(module.INTERVAL_S), _tick_fn(module, ctx), drainer=drainer, run_at_start=at_start
            )
        )
    return tickers


def worker_id(settings: Settings) -> str:
    return f"{settings.node_id}:{socket.gethostname()}:{os.getpid()}"


async def _serve_http(app: Any, port: int, host: str = "0.0.0.0") -> uvicorn.Server:
    """Start uvicorn without its signal capture (``startup``/``shutdown`` instead of ``serve``)."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    await server.startup()
    return server


async def run(
    settings: Settings,
    *,
    ctx: HandlerContext | None = None,
    drainer: Drainer | None = None,
    health: Health | None = None,
    http_port: int | None = None,
    install_signals: bool = True,
    poller_kwargs: dict[str, Any] | None = None,
    handler_modules: tuple[str, ...] = HANDLER_MODULES,
    ready: asyncio.Event | None = None,
) -> int:
    """Run the worker until SIGTERM/SIGINT (or ``drainer.begin()``); 0 = clean drain, 1 = deadline hit.

    ``http_port`` ``None`` reads ``CHARTWIRE_WORKER_PORT`` (default 9001); pass ``0`` for no HTTP server.
    Embedded callers pass the api's ``drainer``/``health``/``ctx`` and ``install_signals=False``.
    """
    own_ctx = ctx is None
    ctx = ctx or build_context(settings)
    drainer = drainer or Drainer(deadline_s=DEFAULT_DEADLINE_S)
    health = health or build_health(ctx, role="worker")
    drainer.on_begin(health.mark_draining)
    if install_signals:
        chain_signals(drainer)
    modules = load_handlers(handler_modules)
    poller = Poller(ctx, worker_id=worker_id(settings), drainer=drainer, **(poller_kwargs or {}))
    tickers = build_tickers(ctx, modules, drainer)
    if http_port is None:
        http_port = int(os.environ.get(WORKER_PORT_ENV, DEFAULT_HTTP_PORT))
    server = None
    if http_port:
        server = await _serve_http(
            standalone_app(ctx, role="worker", drainer=drainer, health=health), http_port
        )
    log.info(
        "worker started",
        extra={"worker_id": poller.worker_id, "tickers": [t.name for t in tickers], "http_port": http_port},
    )
    tasks = [asyncio.create_task(poller.run(), name="poller")]
    tasks += [asyncio.create_task(t.run(), name=f"ticker:{t.name}") for t in tickers]
    if ready is not None:
        ready.set()
    try:
        await drainer.wait_draining()
        clean = await drainer.wait_drained()
        await asyncio.wait(tasks, timeout=1.0)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            server.should_exit = True
            await server.shutdown()
        drainer.uninstall()
        if own_ctx:
            await close_context(ctx)
    log.info("worker stopped", extra={"clean": clean, "reason": drainer.reason})
    return 0 if clean else 1


def main(settings: Settings) -> int:
    """``chartwire serve worker`` entry point."""
    return asyncio.run(run(settings))


# --- embedded: api + worker + stt-worker in one process (§12.2 render.yaml) --------------------------


async def _run_stt(settings: Settings) -> None:
    try:
        stt_worker = importlib.import_module("chartwire.stt.worker")
    except ModuleNotFoundError:
        log.warning("stt worker module not present; embedded stt-worker skipped")
        return
    entry = stt_worker.main
    if inspect.iscoroutinefunction(entry):
        await entry(settings)
    else:
        await asyncio.to_thread(entry, settings)


async def _wait_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.05)
    if task.done():
        task.result()  # re-raise a startup failure (port in use, bad app)


async def run_all(settings: Settings, *, host: str = "0.0.0.0", port: int = 8000) -> int:
    """``serve all --embedded``: one process, one drainer, one pool (5), ops routes on the api port."""
    from chartwire.api.app import create_app  # WP-E; ImportError is the right failure here

    settings = settings.model_copy(update={"embedded": True})
    drainer = Drainer(deadline_s=DEFAULT_DEADLINE_S)
    app = create_app(settings)
    app.state.drainer = drainer  # ops.routes.on_startup reuses it and chains uvicorn's exit
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    api_task = asyncio.create_task(server.serve(), name="api")
    await _wait_started(server, api_task)
    deps = getattr(app.state, "deps", None)
    ctx = context_from_deps(deps) if deps is not None else build_context(settings)
    health = getattr(app.state, "health", None) or build_health(ctx, role="all")
    worker_task = asyncio.create_task(
        run(settings, ctx=ctx, drainer=drainer, health=health, http_port=0, install_signals=False),
        name="worker",
    )
    stt_task = asyncio.create_task(_run_stt(settings), name="stt-worker")
    try:
        await api_task
    finally:
        drainer.begin("api_exit")  # no-op if SIGTERM already started the drain
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(worker_task, timeout=drainer.deadline_s + 1.0)
        for task in (worker_task, stt_task):
            task.cancel()
        await asyncio.gather(worker_task, stt_task, return_exceptions=True)
        if deps is None:
            await close_context(ctx)
    return 0 if worker_task.done() and not worker_task.cancelled() and worker_task.result() == 0 else 1
