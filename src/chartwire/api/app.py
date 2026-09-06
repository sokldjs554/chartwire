"""``create_app(settings)`` — the api process (spec §6.9, §8.1, §13.3).

Composition, in order:

* **lifespan** builds :class:`AppDeps` (engine as ``chartwire_app``, Redis, KEK, DEK cache, object
  store, clock, node id) at ``app.state.deps``, subscribes to ``keys:invalidate`` (DEK cache eviction
  after a purge), then starts the WebSocket runtime (WP-B) and the ops/drain wiring (WP-G);
* **routers** — every REST route of §6.9 owned here plus, each guarded by ``ImportError`` so a partial
  tree still boots: ``ws.routes`` (WP-B), ``routers.notes`` (WP-D), ``ops.routes`` (WP-G);
* **middleware** — request id, security headers, CORS allowlist, 1 MB body limit, rate limit,
  ``Idempotency-Key`` (:mod:`chartwire.api.middleware`);
* **errors** — every failure is RFC 9457 ``application/problem+json`` with a ``CW-xxxx`` code and
  the request id (``AppError``, consent gate, auth ``HTTPException``, validation, unexpected).

Tests may run the app without its lifespan by injecting ``app.state.deps`` (``httpx.ASGITransport``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException

from chartwire.api import middleware
from chartwire.api.deps import AppDeps, request_id
from chartwire.api.routers import (
    alerts,
    audit,
    auth,
    consents,
    opsviews,
    patients,
    purge,
    search,
    segments,
    sessions,
    users,
)
from chartwire.auth.deps import clock as auth_clock
from chartwire.auth.deps import jwt_secret
from chartwire.auth.jwt import SYSTEM_CLOCK
from chartwire.consent.gates import ConsentScopeMissing
from chartwire.core.clock import SystemClock
from chartwire.core.config import Settings, get_settings
from chartwire.core.errors import AppError
from chartwire.crypto.errors import CryptoError
from chartwire.crypto.kek import LocalKek
from chartwire.crypto.keycache import KeyCache
from chartwire.db.engine import make_engine
from chartwire.objectstore import from_spec
from chartwire.redis import keys

log = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = middleware.PROBLEM_MEDIA_TYPE
CONSOLE_DIR_ENV = "CHARTWIRE_CONSOLE_DIR"
_STATUS_CODES = {
    400: "CW-4000",
    401: "CW-4010",
    403: "CW-4030",
    404: "CW-4040",
    405: "CW-4050",
    409: "CW-4090",
}

OWN_ROUTERS = (
    auth.router,
    users.router,
    patients.router,
    consents.router,
    sessions.router,
    segments.router,
    search.router,
    alerts.router,
    purge.router,
    audit.router,
    opsviews.router,
)


# ------------------------------------------------------------------ deps


def build_deps(settings: Settings) -> AppDeps:
    kek = LocalKek(settings.kek_master_bytes)
    pool = 5 if settings.embedded else 20
    return AppDeps(
        settings=settings,
        engine=make_engine(
            settings.database_url, pool_size=pool, max_overflow=0 if settings.embedded else 10
        ),
        redis=Redis.from_url(
            settings.redis_url, decode_responses=True, max_connections=settings.redis_max_connections
        ),
        kek=kek,
        keycache=KeyCache(kek),
        objectstore=from_spec(settings.objectstore),
        clock=SystemClock(),
        node_id=settings.node_id,
    )


async def close_deps(deps: AppDeps) -> None:
    await deps.engine.dispose()
    with contextlib.suppress(RedisError, OSError):
        await deps.redis.aclose()


async def _keys_invalidate_loop(deps: AppDeps) -> None:
    """``keys:invalidate`` subscriber: evict a purged session/patient DEK from this node's cache."""
    pubsub = deps.redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(keys.KEYS_INVALIDATE)
        while True:
            try:
                message = await pubsub.get_message(timeout=1.0)
            except (RedisError, OSError):
                await asyncio.sleep(1.0)
                continue
            if message and message.get("type") == "message":
                deps.keycache.invalidate(str(message["data"]))
    finally:
        with contextlib.suppress(RedisError, OSError):
            await pubsub.aclose()


def _optional(module: str) -> Any | None:
    try:
        return import_module(module)
    except ImportError as exc:
        log.info("optional module not available", extra={"module_name": module, "reason": type(exc).__name__})
        return None


# ------------------------------------------------------------------ errors


def problem_response(
    exc: AppError, rid: str | None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        exc.to_problem(rid),
        status_code=exc.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=None if headers is None else dict(headers),
    )


def _http_exception_problem(exc: HTTPException) -> AppError:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or _STATUS_CODES.get(exc.status_code, "CW-4000"))
        text = str(detail.get("detail") or detail.get("reason") or "요청을 처리할 수 없습니다")
    else:
        code = _STATUS_CODES.get(exc.status_code, "CW-4000" if exc.status_code < 500 else "CW-5000")
        text = str(detail) if detail else "요청을 처리할 수 없습니다"
    return AppError(code, exc.status_code, text)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        return problem_response(exc, request_id(request))

    @app.exception_handler(ConsentScopeMissing)
    async def _consent(request: Request, exc: ConsentScopeMissing) -> Response:
        return problem_response(AppError(exc.code, exc.status, exc.detail), request_id(request))

    @app.exception_handler(
        HTTPException
    )  # Starlette's base class: covers FastAPI's and the router's own 404/405
    async def _http(request: Request, exc: HTTPException) -> Response:
        return problem_response(_http_exception_problem(exc), request_id(request), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": err.get("msg"),
                "type": err.get("type"),
            }
            for err in exc.errors()
        ]  # never ``input``: a rejected value may be free text
        problem = AppError("CW-4220", 422, "요청 형식이 올바르지 않습니다").to_problem(request_id(request))
        problem["errors"] = errors
        return JSONResponse(problem, status_code=422, media_type=PROBLEM_MEDIA_TYPE)

    @app.exception_handler(CryptoError)
    async def _crypto(request: Request, exc: CryptoError) -> Response:
        # wrong KEK master / foreign key material: the reason is a token like ``invalid_tag``, never key bytes
        log.error("key material unusable", extra={"error_type": type(exc).__name__})
        return problem_response(AppError("CW-5001", 500, "키 자료를 열 수 없습니다"), request_id(request))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.error("unhandled error", extra={"error_type": type(exc).__name__}, exc_info=exc)
        return problem_response(
            AppError("CW-5000", 500, "내부 오류가 발생했습니다", retryable=True), request_id(request)
        )


# ------------------------------------------------------------------ console


def console_path(settings: Settings | None = None) -> Path | None:
    configured = settings.console_dir if settings is not None else os.environ.get(CONSOLE_DIR_ENV)
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[3] / "console")
    candidates.append(Path("/app/console"))
    for directory in candidates:
        index = directory / "index.html"
        if index.is_file():
            return index
    return None


# ------------------------------------------------------------------ factory


_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#2456c9"/>'
    '<path d="M6 17h4l3-7 3 13 3-9 2 3h5" fill="none" stroke="#fff" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    ws_routes = _optional("chartwire.ws.routes")
    notes_routes = _optional("chartwire.api.routers.notes")
    ops_routes = _optional("chartwire.ops.routes")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        deps = build_deps(settings)
        app.state.deps = deps
        invalidator = asyncio.get_running_loop().create_task(
            _keys_invalidate_loop(deps), name="keys-invalidate"
        )
        try:
            if ws_routes is not None:
                await ws_routes.on_startup(app, deps)
            if ops_routes is not None:
                ops_routes.on_startup(app, deps, role="api")
            log.info("api started", extra={"node_id": deps.node_id, "dev_secrets": settings.uses_dev_secrets})
            yield
        finally:
            if ws_routes is not None:
                await ws_routes.on_shutdown(app)
            if ops_routes is not None:
                await ops_routes.on_shutdown(app)
            invalidator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await invalidator
            await close_deps(deps)
            app.state.deps = None

    app = FastAPI(
        title="chartwire",
        version="0.1.0",
        description="SOAPY-class 음성차팅 제품의 밑바닥 백엔드 층 — 모든 데이터는 합성(SYNTHETIC)입니다",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.deps = None

    for router in OWN_ROUTERS:
        app.include_router(router)
    if notes_routes is not None:
        app.include_router(notes_routes.router)
    if ws_routes is not None:
        app.include_router(ws_routes.router)
    if ops_routes is not None:
        app.include_router(ops_routes.router)

    index = console_path(settings)

    @app.get("/console", include_in_schema=False)
    @app.get("/console/index.html", include_in_schema=False)
    async def console() -> Response:
        if index is None:
            raise AppError("CW-4040", 404, "console/index.html 이 없습니다")
        return FileResponse(index, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """탭 아이콘 — 브라우저가 항상 요청하므로 404 로 콘솔 오류를 남기지 않는다 (인라인 SVG, 외부 자원 없음)."""
        return Response(
            _FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"}
        )

    @app.get("/console/scripts.json", include_in_schema=False)
    async def console_scripts() -> Response:
        # 데모 콘솔의 세션 선택기가 script_ref 옆에 대본 템플릿(초진 우울·강박·알코올 …)과 예상 경보 수를
        # 보여 주기 위한 카탈로그. `seed --demo` 가 쓴 index.json 에서 메타데이터만 내보내고 발화 텍스트는
        # 내보내지 않는다. /console 과 같은 탐색용 경로라 인증이 없다 — 합성 대본의 목차일 뿐이다.
        path = Path(settings.scripts_dir) / "index.json"
        if not path.is_file():
            return JSONResponse({"scripts": []})
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return JSONResponse({"scripts": []})
        keep = ("script_ref", "template", "n_utterances", "total_ms", "n_expected_alerts", "risk_kinds")
        scripts = [{k: s.get(k) for k in keep} for s in index.get("scripts", []) if isinstance(s, dict)]
        return JSONResponse({"scripts": scripts})

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        # 배포 URL 을 그대로 열면 콘솔로 보낸다 — 루트에서 CW-4040 problem+json 을 받는 혼란을 막는 안내용 302.
        # 콘솔의 정식 위치는 여전히 /console (spec §13.3). /console·/docs 와 같은 탐색용 경로라 API 가 아니며,
        # RBAC 매트릭스(§6.9) 밖에 있다 — tests/integration/test_rbac_matrix.py 가 같은 이유로 건너뛴다.
        return RedirectResponse("/console", status_code=302)

    install_error_handlers(app)

    def _deps() -> AppDeps | None:
        return cast(AppDeps | None, app.state.deps)

    def _redis() -> Any:
        deps = _deps()
        return deps.redis if deps is not None else None

    def _clock() -> Any:
        deps = _deps()
        return deps.clock if deps is not None else SYSTEM_CLOCK

    app.dependency_overrides[jwt_secret] = lambda: settings.jwt_secret
    app.dependency_overrides[auth_clock] = _clock

    # innermost first: add_middleware wraps the previous stack
    app.add_middleware(middleware.Idempotency, settings=settings, redis=_redis, clock=_clock)
    app.add_middleware(middleware.RateLimit, settings=settings, redis=_redis, clock=_clock)
    app.add_middleware(middleware.BodyLimit)
    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
            expose_headers=["X-Request-Id", "Retry-After", "X-RateLimit-Remaining", "Idempotent-Replayed"],
            max_age=600,
        )
    app.add_middleware(middleware.SecurityHeaders)
    app.add_middleware(middleware.RequestId, node_id=settings.node_id)
    return app
