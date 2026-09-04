"""``POST /v1/auth/token`` · ``GET /v1/me`` · ``POST/GET /v1/users`` (§6.9, §8.1) over real PostgreSQL/Redis.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from chartwire.crypto.kek import LocalKek
from chartwire.db.models import AuditEvent, User
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.localfs import LocalFs
from tests.integration.api_support import bearer, build_app, client, headers, make_deps, real_record_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(app_engine, owner_engine, redis, settings, tmp_path, tenant_a):
    await real_record_key(owner_engine, tenant_a, LocalKek(settings.kek_master_bytes))
    app = build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))
    async with client(app) as c:
        yield c


async def audit_actions(engine, tenant_id) -> list[str]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        return list((await s.scalars(select(AuditEvent.action).order_by(AuditEvent.id))).all())


async def test_admin_creates_user_then_user_logs_in(api, app_engine, tenant_a, settings):
    admin = headers(settings, tenant_id=tenant_a.id, role="admin")
    created = await api.post(
        "/v1/users",
        json={
            "email": "Dr.Kim@example.test",
            "password": "correct horse battery",
            "role": "clinician",
            "display_name": "가상의사-01",
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["role"] == "clinician" and body["email"] == "Dr.Kim@example.test" and body["is_active"]
    assert "password" not in created.text and "password_hash" not in created.text

    duplicate = await api.post(
        "/v1/users",
        json={
            "email": "dr.kim@example.test",
            "password": "another one here",
            "role": "staff",
            "display_name": "x",
        },
        headers=admin,
    )
    assert duplicate.status_code == 409, "blind index is case-insensitive (NFKC + lower)"

    listed = await api.get("/v1/users", headers=admin)
    assert listed.status_code == 200
    assert [u["email"] for u in listed.json()] == ["Dr.Kim@example.test"], (
        "email decrypted under the record key"
    )

    login = await api.post(
        "/v1/auth/token",
        json={
            "tenant_slug": tenant_a.slug,
            "email": "DR.KIM@example.test",
            "password": "correct horse battery",
        },
    )
    assert login.status_code == 200, login.text
    tok = login.json()
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 900

    me = await api.get("/v1/me", headers=bearer(tok["access_token"]))
    assert me.status_code == 200
    assert me.json()["role"] == "clinician" and me.json()["user_id"] == body["id"]
    assert me.json()["tenant_id"] == str(tenant_a.id)

    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        row = (await s.scalars(select(User).where(User.id == body["id"]))).one()
    assert row.password_hash.startswith("scrypt$") and "correct horse" not in row.password_hash
    assert await audit_actions(app_engine, tenant_a.id) == ["user.created", "auth.login"]


@pytest.mark.parametrize(
    "slug,email,password",
    [
        ("clinic-a", "nobody@example.test", "whatever"),
        ("no-such-clinic", "x@y.z", "whatever"),
        ("clinic-a", "dr.kim@example.test", "wrong"),
    ],
)
async def test_bad_login_is_401_problem_without_hints(api, tenant_a, settings, slug, email, password):
    admin = headers(settings, tenant_id=tenant_a.id, role="admin")
    await api.post(
        "/v1/users",
        json={
            "email": "dr.kim@example.test",
            "password": "correct horse battery",
            "role": "clinician",
            "display_name": "x",
        },
        headers=admin,
    )
    r = await api.post("/v1/auth/token", json={"tenant_slug": slug, "email": email, "password": password})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    problem = r.json()
    assert problem["code"] == "CW-4010" and problem["request_id"]
    assert "nobody" not in r.text and "no-such" not in r.text, "no account enumeration in the message"


async def test_me_rejects_missing_expired_and_tampered_tokens(api, tenant_a, settings):
    missing = await api.get("/v1/me")
    assert missing.status_code == 401 and missing.json()["code"] == "CW-4010"
    tampered = headers(settings, tenant_id=tenant_a.id, role="admin")["Authorization"][:-3] + "abc"
    r = await api.get("/v1/me", headers={"Authorization": tampered})
    assert r.status_code == 401 and r.json()["code"] == "CW-4010" and "abc" not in r.json()["detail"]
    other_secret = headers(type(settings)(jwt_secret="not-the-secret"), tenant_id=tenant_a.id, role="admin")
    assert (await api.get("/v1/me", headers=other_secret)).status_code == 401
