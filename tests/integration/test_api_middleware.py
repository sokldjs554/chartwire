"""Cross-cutting api behaviour (§5, §8.1, §13.3): request ids, security headers, problem+json for
every failure class, ``Idempotency-Key`` replay, the 120/min rate limit, the 1 MB body cap, CORS and
the console page.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from chartwire.db.models import Session as SessionModel
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.localfs import LocalFs
from chartwire.redis import keys, ratelimit
from tests.integration.api_support import build_app, client, grant, headers, make_deps

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(app_engine, redis, settings, tmp_path):
    settings = settings.model_copy(update={"cors_origins": "http://console.example.test"})
    app = build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))
    async with client(app) as c:
        yield c


async def test_request_id_security_headers_and_problem_shapes(api, tenant_a, settings):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician")
    r = await api.get("/v1/me", headers={**clin, "X-Request-Id": "req-abc"})
    assert r.status_code == 200 and r.headers["x-request-id"] == "req-abc"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert (
        r.headers["cache-control"] == "no-store"
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )
    assert r.headers["x-ratelimit-limit"] == str(ratelimit.REST_PER_MIN)

    generated = await api.get("/v1/me", headers=clin)
    assert len(generated.headers["x-request-id"]) == 32, "uuid7 hex when the client sends none"

    missing = await api.get(f"/v1/sessions/{tenant_a.id}", headers=clin)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    problem = missing.json()
    assert problem["type"] == "urn:chartwire:error:CW-4040" and problem["status"] == 404
    assert problem["request_id"] == missing.headers["x-request-id"] and problem["retryable"] is False

    invalid = await api.post("/v1/patients", json={"name": "", "birth_year": 1700}, headers=clin)
    assert invalid.status_code == 422 and invalid.json()["code"] == "CW-4220"
    assert {e["loc"][-1] for e in invalid.json()["errors"]} == {"name", "birth_year"}
    assert "input" not in invalid.text

    unknown = await api.get("/v1/does-not-exist", headers=clin)
    assert unknown.status_code == 404 and unknown.json()["code"] == "CW-4040"


async def test_idempotency_key_replays_the_first_response(
    api, app_engine, tenant_a, patient_a, clinician_a, settings
):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    await grant(app_engine, tenant_a.id, patient_a.id)
    body = {"patient_id": str(patient_a.id), "script_ref": "s02"}
    key = {"Idempotency-Key": "create-1"}
    first = await api.post("/v1/sessions", json=body, headers={**clin, **key})
    assert first.status_code == 201 and "idempotent-replayed" not in first.headers
    second = await api.post("/v1/sessions", json=body, headers={**clin, **key})
    assert second.status_code == 201 and second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        count = (await s.execute(select(func.count()).select_from(SessionModel))).scalar_one()
    assert count == 1, "exactly one session row for two identical requests"

    mismatch = await api.post("/v1/sessions", json={**body, "script_ref": "s03"}, headers={**clin, **key})
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "CW-4222"

    # The keyspace stays per tenant (§5 ``idem:{tenant}:{key}``), but a record is only ever replayed
    # to the principal that produced it: a replay never runs the route and therefore never runs
    # ``rbac.require``, so replaying across principals would hand a role the matrix forbids someone
    # else's response body (an admin's purge receipt, say) with its 202 intact.
    other_principal = headers(settings, tenant_id=tenant_a.id, role="staff")
    shared = await api.post("/v1/sessions", json=body, headers={**other_principal, **key})
    assert shared.status_code == 422 and shared.json()["code"] == "CW-4222"
    assert "idempotent-replayed" not in shared.headers
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        count = (await s.execute(select(func.count()).select_from(SessionModel))).scalar_one()
    assert count == 1, "the rejected cross-principal replay neither replayed nor ran the route"

    failed = await api.post(
        "/v1/sessions", json={"patient_id": str(tenant_a.id)}, headers={**clin, "Idempotency-Key": "create-2"}
    )
    assert failed.status_code == 404
    replayed_failure = await api.post(
        "/v1/sessions", json={"patient_id": str(tenant_a.id)}, headers={**clin, "Idempotency-Key": "create-2"}
    )
    assert (
        replayed_failure.status_code == 404 and replayed_failure.headers.get("idempotent-replayed") == "true"
    )

    bad_key = await api.post("/v1/sessions", json=body, headers={**clin, "Idempotency-Key": "has space"})
    assert bad_key.status_code == 422 and bad_key.json()["code"] == "CW-4223"

    no_key_route = await api.post(
        f"/v1/sessions/{first.json()['id']}/ws-ticket", json={"kind": "watch"}, headers={**clin, **key}
    )
    assert no_key_route.status_code == 200 and "idempotent-replayed" not in no_key_route.headers


async def test_rate_limit_429_per_principal(api, redis, tenant_a, factories, settings):
    user = await factories.user(tenant_a.id, "staff")
    staff = headers(settings, tenant_id=tenant_a.id, role="staff", user_id=user.id)
    statuses = [(await api.get("/v1/me", headers=staff)).status_code for _ in range(ratelimit.REST_PER_MIN)]
    assert set(statuses) == {200}
    blocked = await api.get("/v1/me", headers=staff)
    assert blocked.status_code == 429 and blocked.json()["code"] == "CW-4290" and blocked.json()["retryable"]
    assert 1 <= int(blocked.headers["retry-after"]) <= 60 and blocked.headers["x-ratelimit-remaining"] == "0"
    rl_keys = [k async for k in redis.scan_iter(match=f"rl:{tenant_a.id}:{user.id}:rest:*")]
    assert len(rl_keys) == 1 and 0 < await redis.ttl(rl_keys[0]) <= keys.TTL_RATELIMIT
    other = headers(settings, tenant_id=tenant_a.id, role="staff")
    assert (await api.get("/v1/me", headers=other)).status_code == 200, "another principal is unaffected"


async def test_body_limit_cors_and_console(api, tenant_a, settings):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician")
    huge = await api.post(
        "/v1/patients",
        content=b"{" + b" " * 1_048_600 + b"}",
        headers={**clin, "content-type": "application/json"},
    )
    assert huge.status_code == 413 and huge.json()["code"] == "CW-4130"

    preflight = await api.options(
        "/v1/me", headers={"Origin": "http://console.example.test", "Access-Control-Request-Method": "GET"}
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://console.example.test"
    denied = await api.options(
        "/v1/me", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in denied.headers

    root = await api.get("/")
    assert root.status_code == 302 and root.headers["location"] == "/console", (
        "배포 URL 루트는 콘솔로 안내한다"
    )
    page = await api.get("/console")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    icon = await api.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml"), (
        "the tab icon must not leave a 404 in the browser console"
    )
    catalog = await api.get("/console/scripts.json")
    assert catalog.status_code == 200 and isinstance(catalog.json().get("scripts"), list), (
        "대본 카탈로그는 항상 목록"
    )
    assert (
        "SYNTHETIC" in page.text
        and "script-src 'self' 'unsafe-inline'" in page.headers["content-security-policy"]
    )
    assert (await api.get("/healthz")).status_code == 200
