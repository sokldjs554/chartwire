"""Spec §0.9: nothing in the logs may carry transcript text, patient names, phone numbers or tokens.

Runs requests that *do* decrypt and return PHI (segments, patient, search, timeline), a validation
failure whose body contains the marker, and an alert ack, while capturing every stdlib log record
(all fields, not just the message) and every structlog event. The transcript marker, the patient
name, the phone number, the bearer token and the WS ticket must appear in none of them.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import json
import logging
import traceback

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

from chartwire.core.logging import redact_dict
from chartwire.crypto.kek import LocalKek
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.localfs import LocalFs
from tests.integration.api_support import (
    build_app,
    client,
    grant,
    headers,
    make_deps,
    real_patient_dek,
    seed_session,
)

pytestmark = pytest.mark.integration

MARKER = "전사문-마커-9f3a7c"
NAME = "가상환자 로그검사"
PHONE = "010-9876-5432"
SCRIPT = [
    ("patient", f"잠드는 데 두 시간쯤 걸려요 {MARKER}"),
    ("patient", "요즘은 다 사라지고 싶다는 생각이 들어요"),
]


def _record_text(record: logging.LogRecord) -> str:
    """Everything a handler could render: message, args and every ``extra`` attribute."""
    return (
        json.dumps({k: repr(v) for k, v in record.__dict__.items()}, ensure_ascii=False) + record.getMessage()
    )


async def test_request_logs_never_contain_transcript_names_or_tokens(
    app_engine, owner_engine, redis, settings, tmp_path, tenant_a, patient_a, clinician_a, session_a, caplog
):
    deps = make_deps(app_engine, redis, settings, LocalFs(tmp_path))
    await real_patient_dek(
        app_engine, tenant_a, patient_a.id, LocalKek(settings.kek_master_bytes), name=NAME, phone=PHONE
    )
    await grant(app_engine, tenant_a.id, patient_a.id)
    seeded = await seed_session(
        app_engine,
        owner_engine,
        redis,
        deps.objectstore,
        settings,
        tenant=tenant_a,
        patient_id=patient_a.id,
        clinician_id=clinician_a.id,
        session=session_a,
        script=SCRIPT,
    )
    own = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    bearer_token = own["Authorization"].split(" ", 1)[1]

    caplog.set_level(logging.DEBUG)
    with capture_logs() as structured:
        async with client(build_app(deps)) as api:
            segments = await api.get(f"/v1/sessions/{seeded.session.id}/segments", headers=own)
            patient = await api.get(f"/v1/patients/{patient_a.id}", headers=own)
            search = await api.get("/v1/search", params={"q": MARKER[:6], "mode": "text"}, headers=own)
            timeline = await api.get(f"/v1/patients/{patient_a.id}/timeline", headers=own)
            invalid = await api.post(
                "/v1/patients", json={"name": MARKER, "birth_year": "not-a-year"}, headers=own
            )
            ticket = await api.post(
                f"/v1/sessions/{seeded.session.id}/ws-ticket", json={"kind": "watch"}, headers=own
            )
            ack = await api.post(f"/v1/alerts/{seeded.risk_event_ids[0]}/ack", headers=own)
            structlog.get_logger("test").info("request done", text=MARKER, name=NAME, phone=PHONE)

    # the requests really did handle the PHI (so an empty log would not be a vacuous pass)
    assert segments.status_code == 200 and MARKER in segments.text
    assert patient.status_code == 200 and patient.json()["name"] == NAME
    assert search.status_code == 200 and len(search.json()) == 1
    assert timeline.status_code == 200 and MARKER in timeline.text
    assert invalid.status_code == 422 and MARKER not in invalid.text
    assert ticket.status_code == 200 and ack.status_code == 200
    ws_ticket = ticket.json()["ticket"]

    assert len(caplog.records) >= 7, "one access-log line per request"
    secrets = {"transcript": MARKER, "name": NAME, "phone": PHONE, "jwt": bearer_token, "ticket": ws_ticket}
    for record in caplog.records:
        rendered = _record_text(record)
        for label, secret in secrets.items():
            assert secret not in rendered, (
                f"{label} leaked through logger {record.name}: {record.getMessage()}"
            )
    for event in structured:
        rendered = json.dumps(redact_dict(dict(event)), ensure_ascii=False, default=repr)
        for label, secret in secrets.items():
            assert secret not in rendered, f"{label} leaked through structlog event {event.get('event')}"
    access = [r for r in caplog.records if r.getMessage() == "http request"]
    assert access and all(getattr(r, "path", "").startswith("/v1/") for r in access)
    assert all("q=" not in _record_text(r) for r in access), "query strings are never logged"


async def test_db_errors_never_render_bound_parameters(app_engine, tenant_a, patient_a, caplog):
    """A failing statement must not put its bound parameters in the traceback.

    ``segment_search.text`` is the plaintext utterance and ``/v1/search`` binds the clinician's free
    text, and SQLAlchemy renders ``[parameters: (...)]`` into ``str(DBAPIError)``. Those strings reach
    stdout through ``stt/worker.py``'s ``log.exception`` and ``api/app.py``'s ``exc_info=exc``, so the
    engine is built with ``hide_parameters=True``. The success-only coverage above cannot see this.
    """
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SQLAlchemyError) as exc_info:
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
            await s.execute(
                text("INSERT INTO segment_search (segment_id, tenant_id, text) VALUES (:i, :t, :x)"),
                {"i": 1, "t": str(tenant_a.id), "x": MARKER},  # violates NOT NULL on the other columns
            )
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert MARKER not in rendered, "the transcript reached a log-bound exception string"
    assert "hidden due to hide_parameters" in rendered, "SQLAlchemy is still rendering parameters"
    logging.getLogger("test").exception("db failed", exc_info=exc_info.value)
    assert all(MARKER not in _record_text(r) for r in caplog.records)
