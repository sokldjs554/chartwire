"""Sessions, segments, timeline, search, alerts (§6.9) over real PostgreSQL/Redis: DEK generation,
the "clinician (own)" row rule, WS tickets, REST ``end``, decrypted keyset reads, the ≥3-char text
search rule, alert ack effects, and tenancy through the API.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import orjson
import pytest
from sqlalchemy import select

from chartwire.crypto.kek import LocalKek
from chartwire.db.models import AuditEvent
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.localfs import LocalFs
from chartwire.redis import keys, tickets
from tests.integration.api_support import SCRIPT, build_app, client, grant, headers, make_deps, seed_session

pytestmark = pytest.mark.integration


@pytest.fixture
async def deps(app_engine, redis, settings, tmp_path):
    return make_deps(app_engine, redis, settings, LocalFs(tmp_path))


@pytest.fixture
async def api(deps):
    async with client(build_app(deps)) as c:
        yield c


@pytest.fixture
async def seeded(
    app_engine, owner_engine, redis, deps, settings, tenant_a, patient_a, clinician_a, session_a
):
    await grant(app_engine, tenant_a.id, patient_a.id)
    return await seed_session(
        app_engine,
        owner_engine,
        redis,
        deps.objectstore,
        settings,
        tenant=tenant_a,
        patient_id=patient_a.id,
        clinician_id=clinician_a.id,
        session=session_a,
    )


async def test_create_session_generates_wrapped_dek_and_audits(
    api, app_engine, tenant_a, patient_a, clinician_a, settings
):
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    await grant(app_engine, tenant_a.id, patient_a.id, ["recording", "search_index"])
    r = await api.post(
        "/v1/sessions", json={"patient_id": str(patient_a.id), "script_ref": "s01"}, headers=clin
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["state"] == "created" and out["clinician_id"] == str(clinician_a.id) and out["epoch"] == 0
    assert out["scopes_snapshot"] == ["recording", "search_index"] and out["ack_seq"] == 0
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        row = await sessions_repo.get_session(s, out["id"])
        assert row is not None and row.dek_wrapped is not None and row.dek_fingerprint is not None
        dek = LocalKek(settings.kek_master_bytes).unwrap(bytes(row.dek_wrapped), tenant_a.kek_ref)
        assert len(dek) == 32
        audit = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "session.created"))).one()
    assert audit.actor_id == clinician_a.id and audit.detail["scopes"] == ["recording", "search_index"]

    dev = headers(settings, tenant_id=tenant_a.id, role="clinician")
    r2 = await api.post("/v1/sessions", json={"patient_id": str(patient_a.id)}, headers=dev)
    assert r2.status_code == 403 and r2.json()["code"] == "CW-4032", "a dev token owns no rows"
    staff = headers(settings, tenant_id=tenant_a.id, role="staff")
    r3 = await api.post(
        "/v1/sessions",
        json={"patient_id": str(patient_a.id), "clinician_id": str(patient_a.id)},
        headers=staff,
    )
    assert r3.status_code == 422 and r3.json()["code"] == "CW-4224"


async def test_own_rule_list_and_tenancy(
    api, app_engine, factories, tenant_a, tenant_b, clinician_a, clinician_b, session_a, session_b, settings
):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    other = await factories.user(tenant_a.id, "clinician")
    other_h = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=other.id)
    staff = headers(settings, tenant_id=tenant_a.id, role="staff")
    foreign = headers(settings, tenant_id=tenant_b.id, role="clinician", user_id=clinician_b.id)

    assert (await api.get(f"/v1/sessions/{session_a.id}", headers=own)).status_code == 200
    denied = await api.get(f"/v1/sessions/{session_a.id}", headers=other_h)
    assert denied.status_code == 403 and denied.json()["code"] == "CW-4030"
    assert (await api.get(f"/v1/sessions/{session_a.id}", headers=staff)).status_code == 200
    assert (await api.get(f"/v1/sessions/{session_a.id}", headers=foreign)).status_code == 404
    assert (await api.get(f"/v1/sessions/{session_a.id}/segments", headers=foreign)).status_code == 404
    assert (await api.post(f"/v1/sessions/{session_a.id}/end", headers=foreign)).status_code == 404

    assert [s["id"] for s in (await api.get("/v1/sessions", headers=own)).json()["items"]] == [
        str(session_a.id)
    ]
    assert (await api.get("/v1/sessions", headers=other_h)).json()["items"] == []
    assert [s["id"] for s in (await api.get("/v1/sessions", headers=foreign)).json()["items"]] == [
        str(session_b.id)
    ]

    page = await api.get("/v1/sessions", params={"limit": 1, "state": "recording"}, headers=staff)
    assert len(page.json()["items"]) == 1 and page.json()["next_before"] is not None
    nxt = await api.get(
        "/v1/sessions", params={"limit": 1, "before": page.json()["next_before"]}, headers=staff
    )
    assert nxt.json()["items"] == [] and nxt.json()["next_before"] is None


async def test_ws_ticket_roles_and_one_time_payload(
    api, redis, factories, tenant_a, clinician_a, session_a, settings
):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    r = await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "ingest"}, headers=own)
    assert r.status_code == 200 and r.json()["expires_in"] == keys.TTL_TICKET
    payload = await tickets.consume(redis, r.json()["ticket"])
    assert payload is not None and payload.kind == "ingest" and payload.session_id == session_a.id
    assert (
        payload.user_id == clinician_a.id and payload.role == "clinician" and payload.tenant_id == tenant_a.id
    )
    assert await tickets.consume(redis, r.json()["ticket"]) is None, "GETDEL: one use"

    staff = headers(settings, tenant_id=tenant_a.id, role="staff")
    assert (
        await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "ingest"}, headers=staff)
    ).status_code == 403
    assert (
        await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "watch"}, headers=staff)
    ).status_code == 200
    recorder = await factories.user(tenant_a.id, "recorder")
    rec = headers(settings, tenant_id=tenant_a.id, role="recorder", user_id=recorder.id)
    assert (
        await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "watch"}, headers=rec)
    ).status_code == 403
    assert (
        await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "ingest"}, headers=rec)
    ).status_code == 200
    other = await factories.user(tenant_a.id, "clinician")
    other_h = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=other.id)
    assert (
        await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "watch"}, headers=other_h)
    ).status_code == 403

    # the ws-ticket bucket is 30/min per principal (§5), separate from the 120/min REST bucket
    statuses = [
        (
            await api.post(f"/v1/sessions/{session_a.id}/ws-ticket", json={"kind": "watch"}, headers=staff)
        ).status_code
        for _ in range(31)
    ]
    assert statuses.count(429) >= 1 and statuses[-1] == 429
    assert (await api.get(f"/v1/sessions/{session_a.id}", headers=staff)).status_code == 200, (
        "REST bucket untouched"
    )


async def test_end_sets_final_seq_and_end_marker(
    api, app_engine, redis, tenant_a, clinician_a, session_a, settings
):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        await sessions_repo.update_session(s, session_a.id, ack_seq=42, epoch=3)
    ps = redis.pubsub()
    await ps.subscribe(keys.sess_events(session_a.id))
    assert await ps.get_message(timeout=2.0) is not None

    r = await api.post(f"/v1/sessions/{session_a.id}/end", headers=own)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ended" and r.json()["final_seq"] == 42 and r.json()["ended_at"]
    entries = await redis.xrange(keys.sess_chunks(session_a.id))
    assert entries[-1][1] == {"end": "1", "ep": "3"}
    assert (await redis.hgetall(keys.sess(session_a.id)))["state"] == "ended"
    assert await redis.ttl(keys.sess_chunks(session_a.id)) > 0
    evt = await ps.get_message(ignore_subscribe_messages=True, timeout=2.0)
    assert evt is not None and orjson.loads(evt["data"]) == {"t": "session.state", "state": "ended"}
    again = await api.post(f"/v1/sessions/{session_a.id}/end", headers=own)
    assert again.status_code == 409 and again.json()["code"] == "CW-4099"
    await ps.aclose()


async def test_segments_timeline_search_decrypted(api, seeded, tenant_a, clinician_a, settings):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    sid = seeded.session.id
    all_rows = (await api.get(f"/v1/sessions/{sid}/segments", headers=own)).json()
    assert [(r["seq"], r["speaker"], r["text"]) for r in all_rows] == [
        (i, sp, tx) for i, (sp, tx) in enumerate(SCRIPT)
    ]
    page = (
        await api.get(f"/v1/sessions/{sid}/segments", params={"after_seq": 1, "limit": 2}, headers=own)
    ).json()
    assert [r["seq"] for r in page] == [2, 3]

    timeline = (
        await api.get(f"/v1/patients/{seeded.patient_id}/timeline", params={"limit": 3}, headers=own)
    ).json()
    assert [r["seq"] for r in timeline["items"]] == [3, 2, 1] and timeline["next_before"]
    rest = (
        await api.get(
            f"/v1/patients/{seeded.patient_id}/timeline",
            params={"before": timeline["next_before"]},
            headers=own,
        )
    ).json()
    assert [r["seq"] for r in rest["items"]] == [0] and rest["next_before"] is None
    assert rest["items"][0]["text"] == SCRIPT[0][1] and rest["items"][0]["session_id"] == str(sid)

    short = await api.get("/v1/search", params={"q": "잠드", "mode": "text"}, headers=own)
    assert short.status_code == 422 and short.json()["code"] == "CW-4220"
    text = (await api.get("/v1/search", params={"q": "두 시간", "mode": "text"}, headers=own)).json()
    assert len(text) == 1 and text[0]["seq"] == 1 and "두 시간" in text[0]["snippet"]
    term = (
        await api.get(
            "/v1/search",
            params={"q": "불면", "mode": "term", "patient_id": str(seeded.patient_id)},
            headers=own,
        )
    ).json()
    assert [h["seq"] for h in term] == [1]
    other_patient = (
        await api.get("/v1/search", params={"q": "불면", "mode": "term", "patient_id": str(sid)}, headers=own)
    ).json()
    assert other_patient == []


async def test_alerts_list_and_ack(
    api, seeded, redis, app_engine, factories, tenant_a, clinician_a, settings
):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    other = await factories.user(tenant_a.id, "clinician")
    other_h = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=other.id)
    [event_id] = seeded.risk_event_ids
    listed = (await api.get("/v1/alerts", headers=own)).json()
    assert [a["id"] for a in listed] == [event_id] and listed[0]["category"] == "suicidal_ideation"
    assert "phrase" not in listed[0] and listed[0]["span"] == [6, 12]
    assert (await api.get("/v1/alerts", headers=other_h)).json() == [], "clinicians see their own sessions"
    assert (
        len(
            (
                await api.get("/v1/alerts", headers=headers(settings, tenant_id=tenant_a.id, role="staff"))
            ).json()
        )
        == 1
    )

    ps = redis.pubsub()
    await ps.subscribe(keys.sess_events(seeded.session.id))
    assert await ps.get_message(timeout=2.0) is not None
    acked = await api.post(f"/v1/alerts/{event_id}/ack", headers=own)
    assert acked.status_code == 200 and acked.json()["acknowledged_by"] == str(clinician_a.id)
    assert await redis.zcard(keys.ALERTS_SLA) == 0
    msg = await ps.get_message(ignore_subscribe_messages=True, timeout=2.0)
    assert msg is not None and orjson.loads(msg["data"]) == {
        "t": "risk.ack",
        "risk_event_id": event_id,
        "by": str(clinician_a.id),
    }
    assert (await api.get("/v1/alerts", headers=own)).json() == []
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        rows = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "alert.acked"))).all()
    assert len(rows) == 1 and rows[0].detail["via"] == "rest"
    missing = await api.post("/v1/alerts/999999/ack", headers=own)
    assert missing.status_code == 404
    await ps.aclose()


async def test_purged_session_segments_are_410(api, seeded, app_engine, tenant_a, clinician_a, settings):
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        from chartwire.db.repo import purge as purge_repo

        await purge_repo.destroy_session_dek(s, seeded.session.id, now=seeded.session.started_at)
    r = await api.get(f"/v1/sessions/{seeded.session.id}/segments", headers=own)
    assert r.status_code == 410 and r.json()["code"] == "CW-4100"
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        row = await s.get(SessionModel, seeded.session.id)
    assert row is not None and row.state == "purged"
