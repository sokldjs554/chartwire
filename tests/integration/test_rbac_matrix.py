"""Every registered REST route × every role (spec §8.1): the HTTP status must match ``rbac.MATRIX``.

The route list comes from the real ``create_app`` (including the notes and ops routers of other work
packages), so a route added without a matrix row — or a matrix row whose route disappeared — fails
here. Disallowed roles get exactly ``403 CW-4030`` (never 404/422: the role gate runs before any
lookup or body validation, so an intruder cannot even probe for ids); allowed roles get anything
except 401/403; anonymous callers reach only the public routes.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from chartwire.auth import rbac
from chartwire.crypto.kek import LocalKek
from chartwire.objectstore.localfs import LocalFs
from tests.integration.api_support import build_app, client, headers, make_deps, real_record_key

pytestmark = pytest.mark.integration

SKIP_PREFIXES = ("/console", "/docs", "/openapi")
SKIP_PATHS = ("/",)  # 루트 → /console 302: 탐색용, API 가 아니다


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Flatten ``app.routes`` (FastAPI ≥ 0.141 keeps included routers as ``_IncludedRouter``)."""

    def walk(routes: Any) -> Any:
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                yield from walk(route.routes)

    return [r for r in walk(app.routes) if not r.path.startswith(SKIP_PREFIXES) and r.path not in SKIP_PATHS]


def concrete_path(route: APIRoute) -> str:
    """Substitute path parameters by their annotation: ``int`` → 1, anything else → a random uuid."""
    params = inspect.signature(route.endpoint).parameters
    path = route.path
    for name in route.param_convertors:
        annotation = params[name].annotation if name in params else None
        value = "1" if annotation is int else str(uuid4())
        path = path.replace("{" + name + "}", value)
    return path


@pytest.fixture
async def rbac_app(app_engine, owner_engine, redis, settings, tmp_path, tenant_a):
    await real_record_key(owner_engine, tenant_a, LocalKek(settings.kek_master_bytes))
    return build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))


def test_matrix_and_routes_agree(rbac_app):
    registered = {rbac.route_key(m, r.path) for r in api_routes(rbac_app) for m in r.methods if m != "HEAD"}
    missing_from_matrix = registered - set(rbac.MATRIX)
    missing_route = set(rbac.MATRIX) - registered
    assert not missing_from_matrix, f"routes without an access decision: {sorted(missing_from_matrix)}"
    assert not missing_route, f"matrix rows without a route: {sorted(missing_route)}"


async def test_every_route_times_every_role(rbac_app, factories, tenant_a, settings):
    users = {role: await factories.user(tenant_a.id, role) for role in sorted(rbac.USER_ROLES)}
    checked = 0
    async with client(rbac_app) as c:
        for route in api_routes(rbac_app):
            for method in route.methods - {"HEAD"}:
                allowed = rbac.MATRIX[rbac.route_key(method, route.path)]
                path = concrete_path(route)
                body = {} if method in ("POST", "PUT") else None
                for role, user in users.items():
                    auth = headers(settings, tenant_id=tenant_a.id, role=role, user_id=user.id)
                    r = await c.request(method, path, json=body, headers=auth)
                    checked += 1
                    if not allowed or role in allowed:
                        assert r.status_code not in (401, 403), (method, path, role, r.status_code, r.text)
                    else:
                        assert r.status_code == 403, (method, path, role, r.status_code, r.text)
                        assert r.headers["content-type"].startswith("application/problem+json")
                        assert r.json()["code"] == "CW-4030"
                anonymous = await c.request(method, path, json=body)
                checked += 1
                if allowed:
                    assert anonymous.status_code == 401, (method, path, anonymous.status_code)
                    assert anonymous.headers.get("www-authenticate") == "Bearer"
                    assert anonymous.json()["code"] == "CW-4010"
                else:
                    assert anonymous.status_code != 401, (method, path)
    assert checked >= len(rbac.MATRIX) * (len(rbac.USER_ROLES) + 1)


async def test_service_role_token_is_not_a_user(rbac_app, tenant_a, settings):
    """``service`` is a worker-only role: a token carrying it is refused by every role gate."""
    async with client(rbac_app) as c:
        r = await c.get("/v1/sessions", headers=headers(settings, tenant_id=tenant_a.id, role="service"))
    assert r.status_code == 403 and r.json()["code"] == "CW-4030"
