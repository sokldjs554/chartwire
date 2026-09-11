"""Pure-ASGI middleware of the api (spec §5, §8.1): request id + access log, security headers,
1 MB body limit, per-principal rate limit and ``Idempotency-Key`` replay.

They are plain ASGI callables (not ``BaseHTTPMiddleware``) so WebSocket scopes pass straight through
and streaming responses are not buffered. Every refusal is an RFC 9457 problem document with the
request id, exactly like the exception handlers in :mod:`chartwire.api.app`.

Principal identification here is deliberately *lightweight*: the bearer token is verified with the
same HS256 code the auth dependency uses, but a missing or bad token is not an error at this layer —
the request continues and the route's dependency answers 401. Anonymous callers are limited by
client address instead.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any, Final

import orjson
from redis.exceptions import RedisError

from chartwire.auth.deps import parse_bearer
from chartwire.auth.jwt import Principal, TokenError, verify
from chartwire.core.clock import Clock
from chartwire.core.config import Settings
from chartwire.core.errors import AppError
from chartwire.core.ids import uuid7
from chartwire.core.logging import bind_context, clear_context
from chartwire.redis import idempotency, ratelimit

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
REQUEST_ID_HEADER: Final = b"x-request-id"
IDEMPOTENCY_HEADER: Final = b"idempotency-key"
MAX_BODY_BYTES: Final = 1_048_576
API_PREFIX: Final = "/v1/"
ANONYMOUS_TENANT: Final = "-"

IDEMPOTENT_PATHS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^/v1/sessions$"),
    re.compile(r"^/v1/patients/[^/]+/consents$"),
    re.compile(r"^/v1/consents/[^/]+/revoke$"),
    re.compile(r"^/v1/purge-jobs$"),
)
"""POST routes honouring ``Idempotency-Key`` (§5: sessions / consents / purge-jobs)."""

CONSOLE_CSP: Final = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'"
)
API_CSP: Final = "default-src 'none'; frame-ancestors 'none'"


# ------------------------------------------------------------------ shared helpers


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


def problem_bytes(exc: AppError, request_id: str | None) -> bytes:
    return orjson.dumps(exc.to_problem(request_id))


async def send_problem(
    send: Send, exc: AppError, request_id: str | None, extra_headers: dict[str, str] | None = None
) -> None:
    body = problem_bytes(exc, request_id)
    headers = [(b"content-type", PROBLEM_MEDIA_TYPE.encode()), (b"content-length", str(len(body)).encode())]
    if request_id:
        headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
    for key, value in (extra_headers or {}).items():
        headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    await send({"type": "http.response.start", "status": exc.status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


def scope_request_id(scope: Scope) -> str | None:
    state = scope.get("state") or {}
    return state.get("request_id") or _header(scope, REQUEST_ID_HEADER)


class _PrincipalResolver:
    """Best-effort bearer verification for middleware (never raises, never logs the token)."""

    def __init__(self, settings: Settings, clock: Callable[[], Clock]) -> None:
        self._secret = settings.jwt_secret
        self._clock = clock

    def resolve(self, scope: Scope) -> Principal | None:
        authorization = _header(scope, b"authorization")
        if not authorization:
            return None
        try:
            return verify(parse_bearer(authorization), secret=self._secret, clock=self._clock())
        except Exception:  # HTTPException from parse_bearer or TokenError: the route will answer 401
            return None


def client_host(scope: Scope) -> str:
    """레이트리밋의 익명 호출자 식별자 — **소켓 주소만** 쓴다.

    ``X-Forwarded-For`` 를 여기서 읽지 않는 이유: 헤더는 호출자가 마음대로 넣을 수 있어서, 그 값으로
    버킷을 나누면 헤더만 바꿔 가며 무제한으로 던질 수 있다. 프록시 뒤에서 원 클라이언트를 복원하는 일은
    uvicorn 의 ``ProxyHeadersMiddleware`` 몫이고(신뢰하는 프록시 목록이 있어야 동작한다),
    그때는 이 함수가 읽는 ``scope["client"]`` 자체가 이미 복원된 주소다
    (:data:`chartwire.core.config.Settings.trusted_proxy_ips`).
    """
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


# ------------------------------------------------------------------ request id + access log


class RequestId:
    """Assign/propagate ``X-Request-Id``, bind it to structlog for the request, echo it back and write
    one access-log line (method, path template-free path, status, duration — never the query string,
    which may carry a free-text search term)."""

    def __init__(self, app: ASGIApp, *, node_id: str = "") -> None:
        self.app = app
        self.node_id = node_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rid = _header(scope, REQUEST_ID_HEADER) or uuid7().hex
        if len(rid) > 128 or not rid.isprintable():
            rid = uuid7().hex
        scope.setdefault("state", {})["request_id"] = rid
        bind_context(request_id=rid, node_id=self.node_id or None)
        started = time.perf_counter()
        status_holder = {"status": 0}

        async def send_with_id(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            if scope["type"] == "http":
                log.info(
                    "http request",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status_holder["status"],
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "request_id": rid,
                    },
                )
            clear_context()


# ------------------------------------------------------------------ security headers


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        csp = CONSOLE_CSP if path.startswith("/console") else API_CSP

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k for k, _ in headers}
                for key, value in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", csp.encode()),
                    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
                    (b"cache-control", b"no-store"),
                ):
                    if key not in present:
                        headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ------------------------------------------------------------------ body limit


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Reject request bodies over ``max_bytes`` (1 MB, §8.1) — by ``Content-Length`` up front, and by
    counting streamed bytes when the length is unknown."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = scope_request_id(scope)
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await send_problem(send, self._error(), rid)
            return
        seen = 0
        started = False

        async def counting_receive() -> MutableMapping[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await send_problem(send, self._error(), rid)

    def _error(self) -> AppError:
        return AppError("CW-4130", 413, f"요청 본문은 {self.max_bytes} 바이트를 넘을 수 없습니다")


# ------------------------------------------------------------------ rate limit


class RateLimit:
    """§5 ``rl:`` keys: 120/min per principal on ``/v1/*``, 30/min for ``ws-ticket``. Redis unavailability
    fails *open* with a warning — a throttle is not a confidentiality control, and refusing every request
    when Redis blinks would turn a cache outage into an api outage (ingest itself fails closed, §5)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        redis: Callable[[], Any],
        clock: Callable[[], Clock],
    ) -> None:
        self.app = app
        self._redis = redis
        self._clock = clock
        self._principals = _PrincipalResolver(settings, clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(API_PREFIX):
            await self.app(scope, receive, send)
            return
        redis = self._redis()
        if redis is None:
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        principal = self._principals.resolve(scope)
        tenant = str(principal.tenant_id) if principal else ANONYMOUS_TENANT
        who = principal.sub if principal else client_host(scope)
        if path.endswith("/ws-ticket"):
            bucket, limit = ratelimit.BUCKET_WS_TICKET, ratelimit.WS_TICKET_PER_MIN
        else:
            bucket, limit = ratelimit.BUCKET_REST, ratelimit.REST_PER_MIN
        try:
            decision = await ratelimit.hit(redis, tenant, who, bucket, limit, now=self._clock().now())
        except (RedisError, OSError):
            log.warning("rate limit store unavailable; allowing request", extra={"bucket": bucket})
            await self.app(scope, receive, send)
            return
        if not decision.allowed:
            exc = AppError("CW-4290", 429, "요청이 너무 많습니다. 잠시 후 다시 시도하세요", retryable=True)
            await send_problem(
                send,
                exc,
                scope_request_id(scope),
                {
                    "Retry-After": str(decision.retry_after_s),
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
            return

        async def send_with_quota(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(decision.limit).encode()))
                headers.append((b"x-ratelimit-remaining", str(decision.remaining).encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_quota)


# ------------------------------------------------------------------ idempotency


class Idempotency:
    """``Idempotency-Key`` on the POST routes of §5. First use runs the handler and stores
    ``(status, content type, body)`` for 24 h; a repeat with the same body replays that response with
    ``Idempotent-Replayed: true``; the same key with a different body is 422; a key whose first request
    is still running is 409. Responses ≥ 500 are not stored (the client may retry)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        redis: Callable[[], Any],
        clock: Callable[[], Clock],
    ) -> None:
        self.app = app
        self._redis = redis
        self._principals = _PrincipalResolver(settings, clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        key = _header(scope, IDEMPOTENCY_HEADER)
        path = str(scope.get("path", ""))
        if key is None or not any(p.match(path) for p in IDEMPOTENT_PATHS):
            await self.app(scope, receive, send)
            return
        rid = scope_request_id(scope)
        redis = self._redis()
        principal = self._principals.resolve(scope)
        if redis is None or principal is None:  # the route answers 401 / 503 itself
            await self.app(scope, receive, send)
            return
        if not idempotency.valid_key(key):
            await send_problem(
                send, AppError("CW-4223", 422, "Idempotency-Key 형식이 올바르지 않습니다"), rid
            )
            return
        body = await _drain_body(receive)
        digest = idempotency.body_hash(body)
        tenant = str(principal.tenant_id)
        existing = await idempotency.begin(redis, tenant, key, digest, sub=principal.sub)
        if existing is not None:
            if existing.body_hash != digest:
                exc = AppError("CW-4222", 422, "같은 Idempotency-Key 로 다른 본문을 보냈습니다")
                await send_problem(send, exc, rid)
                return
            if existing.sub != principal.sub:
                # The key space is per tenant, but a replay never runs the route — so it never runs
                # ``rbac.require`` either. Replaying another principal's stored response would hand,
                # say, a ``staff`` user the admin's purge receipt at 202. Two principals colliding on
                # one key is the same client error as reusing a key for another body.
                exc = AppError("CW-4222", 422, "같은 Idempotency-Key 를 다른 사용자가 이미 사용했습니다")
                await send_problem(send, exc, rid)
                return
            if existing.state == idempotency.PENDING:
                exc = AppError("CW-4095", 409, "같은 Idempotency-Key 의 요청이 처리 중입니다", retryable=True)
                await send_problem(send, exc, rid, {"Retry-After": "1"})
                return
            await _send_stored(send, existing, rid)
            return

        captured: dict[str, Any] = {"status": 0, "content_type": "", "chunks": []}

        async def replay_receive() -> MutableMapping[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def capture_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = int(message["status"])
                for name, value in message.get("headers", []):
                    if name == b"content-type":
                        captured["content_type"] = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                captured["chunks"].append(bytes(message.get("body", b"")))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            await idempotency.release(redis, tenant, key)
            raise
        status = captured["status"]
        if 200 <= status < 500:
            await idempotency.complete(
                redis,
                tenant,
                key,
                request_body_hash=digest,
                sub=principal.sub,
                status=status,
                content_type=captured["content_type"],
                body=b"".join(captured["chunks"]),
            )
        else:
            await idempotency.release(redis, tenant, key)


async def _drain_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(bytes(message.get("body", b"")))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _send_stored(send: Send, record: idempotency.Record, request_id: str | None) -> None:
    headers = [
        (b"content-type", (record.content_type or "application/json").encode("latin-1")),
        (b"content-length", str(len(record.body)).encode()),
        (b"idempotent-replayed", b"true"),
    ]
    if request_id:
        headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
    await send({"type": "http.response.start", "status": record.status, "headers": headers})
    await send({"type": "http.response.body", "body": record.body, "more_body": False})


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


__all__ = [
    "BodyLimit",
    "Idempotency",
    "RateLimit",
    "RequestId",
    "SecurityHeaders",
    "TokenError",
]
