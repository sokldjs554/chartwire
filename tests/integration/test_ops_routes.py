"""``/healthz`` · ``/readyz`` · ``/metrics`` on a real engine/Redis (§6.9), drain → 503, dependency
failure → 503 with the failing probe named, and the SIGTERM chain (drainer first, previous handler —
uvicorn's ``handle_exit`` in production — after the drain).
"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from chartwire.core.config import Settings
from chartwire.ops import routes
from chartwire.ops.drain import Drainer

pytestmark = pytest.mark.integration


def deps_for(app_engine, redis) -> SimpleNamespace:
    return SimpleNamespace(engine=app_engine, redis=redis, settings=Settings(node_id="test-node"))


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ops")


async def test_health_ready_metrics_and_drain(app_engine, redis):
    drainer = Drainer(deadline_s=5)
    app = routes.standalone_app(deps_for(app_engine, redis), role="worker", drainer=drainer)
    async with client(app) as c:
        live = await c.get("/healthz")
        assert live.status_code == 200
        assert live.json() == {
            "status": "ok",
            "draining": False,
            "role": "worker",
            "node_id": "test-node",
            "dev_secrets": "true",
        }

        ready = await c.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {"postgres": "ok", "redis": "ok"}
        assert ready.json()["status"] == "ready"

        metrics = await c.get("/metrics")
        assert metrics.status_code == 200 and metrics.headers["content-type"].startswith("text/plain")
        body = metrics.text
        for name in ("outbox_pending", "outbox_lag_seconds", "handler_duration_seconds", "db_pool_in_use"):
            assert f"# TYPE {name}" in body
        assert "python_gc_objects" not in body, "private registry only"

        assert drainer.begin("test") is True
        assert (await c.get("/healthz")).status_code == 200, "liveness survives the drain"
        draining = await c.get("/readyz")
        assert draining.status_code == 503 and draining.json()["status"] == "draining"


async def test_readyz_reports_failing_dependency(app_engine):
    bad_redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        app = routes.standalone_app(deps_for(app_engine, bad_redis), role="worker", drainer=Drainer())
        async with client(app) as c:
            ready = await c.get("/readyz")
        assert ready.status_code == 503
        body = ready.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["postgres"] == "ok"
        assert body["checks"]["redis"] in {"error", "fail", "timeout"}
    finally:
        await bad_redis.aclose()


async def test_on_startup_reuses_existing_state_and_binds_pool(app_engine, redis):
    app = FastAPI()
    mine = Drainer(deadline_s=3)
    app.state.drainer = mine
    routes.on_startup(app, deps_for(app_engine, redis), role="api", install_signals=False)
    assert app.state.drainer is mine, "a drainer another module placed first is kept"
    assert app.state.health.check_names == ("postgres", "redis")
    mine.begin("test")
    assert app.state.health.draining
    await routes.on_shutdown(app)


async def test_sigterm_chain_runs_drainer_then_previous_handler():
    """SIGTERM → drainer.begin → (in-flight ≤ deadline) → the handler that was installed before
    (uvicorn's ``Server.handle_exit`` in production) — the api exits only after its drain."""
    seen: list[int] = []
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda sig, frame: seen.append(sig))
    drainer = Drainer(deadline_s=2)
    try:
        assert routes.chain_signals(drainer) is True
        finished = asyncio.Event()

        async def work() -> None:
            async with drainer.track():
                await asyncio.sleep(0.2)
            finished.set()

        task = asyncio.create_task(work())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
        assert drainer.draining and drainer.reason == "SIGTERM" and seen == [], "previous handler waits"
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == [signal.SIGTERM]
        await task
    finally:
        drainer.uninstall()
        signal.signal(signal.SIGTERM, original)
