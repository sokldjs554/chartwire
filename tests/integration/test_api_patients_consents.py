"""Patients (blind index, per-patient DEK, tenancy through the API) and the consent lifecycle
(§6.9, §8.3): grant → new version, session create gate ``CW-4031``, revoke in one transaction with
the outbox event + patient-level purge job + ``ctl`` publish to live sessions.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import orjson
import pytest
from sqlalchemy import select

from chartwire.crypto.kek import LocalKek
from chartwire.db.models import AuditEvent, OutboxEvent, Patient, PurgeJob
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.localfs import LocalFs
from chartwire.redis import keys
from tests.integration.api_support import build_app, client, headers, make_deps, real_record_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(app_engine, owner_engine, redis, settings, tmp_path, tenant_a, tenant_b):
    kek = LocalKek(settings.kek_master_bytes)
    await real_record_key(owner_engine, tenant_a, kek)
    await real_record_key(owner_engine, tenant_b, kek)
    app = build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))
    async with client(app) as c:
        yield c


async def _rows(engine, tenant_id, model):
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        return list((await s.scalars(select(model))).all())


async def test_patient_create_encrypts_name_and_blind_index_lookup_is_exact(
    api, app_engine, tenant_a, clinician_a, settings
):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    r = await api.post(
        "/v1/patients",
        json={"name": "가상환자 홍길동", "birth_year": 1988, "sex": "M", "phone": "010-0000-0000"},
        headers=clin,
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert (
        out["pseudonym"].startswith("가상환자-") and out["name"] == "가상환자 홍길동" and out["is_synthetic"]
    )
    assert out["consent_state"] == "none" and "phone" not in out

    rows = await _rows(app_engine, tenant_a.id, Patient)
    row = next(p for p in rows if str(p.id) == out["id"])
    assert row.dek_wrapped is not None and row.name_hmac is not None
    assert "홍길동".encode() not in bytes(row.name_enc), "name is ciphertext at rest"

    exact = await api.get(
        "/v1/patients", params={"name": " 가상환자  홍길동 ".replace("  ", " ")}, headers=clin
    )
    assert [p["id"] for p in exact.json()] == [out["id"]], "NFKC/strip/lower normalisation, exact match"
    partial = await api.get("/v1/patients", params={"name": "홍길동"}, headers=clin)
    assert partial.json() == [], "a blind index cannot do substring search"

    got = await api.get(f"/v1/patients/{out['id']}", headers=clin)
    assert got.status_code == 200 and got.json()["name"] == "가상환자 홍길동"


async def test_tenant_b_cannot_see_tenant_a_patient(
    api, tenant_a, tenant_b, clinician_a, clinician_b, settings
):
    a = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    b = headers(settings, tenant_id=tenant_b.id, role="clinician", user_id=clinician_b.id)
    created = (await api.post("/v1/patients", json={"name": "가상환자 김철수"}, headers=a)).json()
    assert (await api.get(f"/v1/patients/{created['id']}", headers=b)).status_code == 404
    assert (await api.get("/v1/patients", params={"name": "가상환자 김철수"}, headers=b)).json() == []
    assert (await api.get(f"/v1/patients/{created['id']}/consents", headers=b)).status_code == 404
    assert (
        await api.post(f"/v1/patients/{created['id']}/consents", json={"scopes": ["recording"]}, headers=b)
    ).status_code == 404
    assert (await api.post("/v1/sessions", json={"patient_id": created["id"]}, headers=b)).status_code == 404


async def test_consent_versions_and_session_gate(api, app_engine, tenant_a, clinician_a, settings):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    patient = (await api.post("/v1/patients", json={"name": "가상환자 이영희"}, headers=clin)).json()

    blocked = await api.post("/v1/sessions", json={"patient_id": patient["id"]}, headers=clin)
    assert blocked.status_code == 403 and blocked.json()["code"] == "CW-4031", (
        "no consent at all → fail closed"
    )

    v1 = await api.post(
        f"/v1/patients/{patient['id']}/consents",
        json={"scopes": ["transcription", "recording", "recording"]},
        headers=clin,
    )
    assert v1.status_code == 201 and v1.json()["version"] == 1
    assert v1.json()["scopes"] == ["recording", "transcription"], "deduplicated, canonical order"
    v2 = await api.post(
        f"/v1/patients/{patient['id']}/consents", json={"scopes": ["transcription"]}, headers=clin
    )
    assert v2.json()["version"] == 2

    blocked_again = await api.post("/v1/sessions", json={"patient_id": patient["id"]}, headers=clin)
    assert blocked_again.status_code == 403 and blocked_again.json()["code"] == "CW-4031", (
        "latest version governs"
    )

    v3 = await api.post(
        f"/v1/patients/{patient['id']}/consents", json={"scopes": ["recording", "ai_drafting"]}, headers=clin
    )
    session = await api.post("/v1/sessions", json={"patient_id": patient["id"]}, headers=clin)
    assert session.status_code == 201, session.text
    assert session.json()["scopes_snapshot"] == ["recording", "ai_drafting"]

    listed = await api.get(f"/v1/patients/{patient['id']}/consents", headers=clin)
    assert [c["version"] for c in listed.json()] == [3, 2, 1]
    assert (await api.get(f"/v1/patients/{patient['id']}", headers=clin)).json()["consent_state"] == "granted"

    invalid = await api.post(
        f"/v1/patients/{patient['id']}/consents", json={"scopes": ["diagnosis"]}, headers=clin
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "CW-4220"
    assert "diagnosis" not in invalid.text, "validation problems never echo the rejected input"
    assert v3.json()["granted_by"] == str(clinician_a.id)


async def test_revoke_is_one_transaction_and_tells_live_sessions(
    api, app_engine, redis, tenant_a, clinician_a, settings
):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    patient = (await api.post("/v1/patients", json={"name": "가상환자 박민수"}, headers=clin)).json()
    consent = (
        await api.post(
            f"/v1/patients/{patient['id']}/consents",
            json={"scopes": ["recording", "transcription"]},
            headers=clin,
        )
    ).json()
    session = (await api.post("/v1/sessions", json={"patient_id": patient["id"]}, headers=clin)).json()
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        from chartwire.db.repo import sessions as sessions_repo

        await sessions_repo.set_state(s, session["id"], "recording")

    ps = redis.pubsub()
    await ps.subscribe(keys.ctl(session["id"]))
    assert await ps.get_message(timeout=2.0) is not None

    staff = headers(
        settings, tenant_id=tenant_a.id, role="staff", user_id=(await _staff(api, tenant_a, settings))
    )
    r = await api.post(f"/v1/consents/{consent['id']}/revoke", json={"reason": "환자 요청"}, headers=staff)
    assert r.status_code == 202, r.text
    out = r.json()
    assert (
        out["consent_id"] == consent["id"] and out["live_sessions_notified"] == 1 and out["outbox_event_id"]
    )

    ctl = await ps.get_message(ignore_subscribe_messages=True, timeout=2.0)
    assert ctl is not None and orjson.loads(ctl["data"]) == {"t": "consent_revoked"}

    assert (await api.get(f"/v1/patients/{patient['id']}", headers=clin)).json()["consent_state"] == "revoked"
    jobs = await _rows(app_engine, tenant_a.id, PurgeJob)
    assert len(jobs) == 1 and jobs[0].subject_type == "patient" and jobs[0].reason == "consent_revoked"
    assert str(jobs[0].id) == out["purge_job_id"] and jobs[0].state == "queued"
    events = await _rows(app_engine, tenant_a.id, OutboxEvent)
    revoked = [e for e in events if e.event_type == "consent.revoked"]
    assert len(revoked) == 1 and revoked[0].payload == {
        "patient_id": patient["id"],
        "consent_id": consent["id"],
        "purge_job_id": out["purge_job_id"],
    }
    assert revoked[0].idempotency_key == f"consent.revoked:{consent['id']}:1"
    actions = [a.action for a in await _rows(app_engine, tenant_a.id, AuditEvent)]
    assert actions[-2:] == ["consent.revoked", "purge.requested"]

    again = await api.post(f"/v1/consents/{consent['id']}/revoke", headers=staff)
    assert again.status_code == 409 and again.json()["code"] == "CW-4097"
    gated = await api.post("/v1/sessions", json={"patient_id": patient["id"]}, headers=clin)
    assert gated.status_code == 403 and gated.json()["code"] == "CW-4031"
    await ps.aclose()


async def _staff(api, tenant, settings):
    admin = headers(settings, tenant_id=tenant.id, role="admin")
    r = await api.post(
        "/v1/users",
        json={
            "email": "staff@example.test",
            "password": "staff-password-1",
            "role": "staff",
            "display_name": "가상직원",
        },
        headers=admin,
    )
    return r.json()["id"]
