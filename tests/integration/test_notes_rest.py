"""Notes REST (spec §6.9) over the real PostgreSQL/Redis, in-process ASGI (no port).

Covers: owner reads (+ ``note.read`` audit), the "own session" rule, RBAC refusals, auditor
metadata-only responses, manual draft enqueue through the outbox, the decision → assessment → sign
flow with every refusal (no assessment, unsupported statement pending, edit without text, already
signed), and the ADR-0004 property: after a purge-like deletion of the session's segments and DEK,
the signed note is still fully readable because it was sealed under the tenant record key.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from chartwire.crypto.envelope import Envelope
from chartwire.crypto.errors import DecryptError, DekDestroyedError
from chartwire.db.models import AuditEvent, Note, NoteAssessment, NoteStatement, OutboxEvent
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import notes as notes_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.notes import service
from chartwire.notes.providers.recorded import RecordedProvider
from chartwire.outbox.poller import Poller
from chartwire.outbox.registry import REGISTRY
from tests.integration.notes_support import (
    EXPECTED_SECTIONS,
    bearer,
    build_app,
    make_ctx,
    make_keycache,
    purge_like,
    seed,
    token,
)

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic"
NEEDS_REVIEW_FIXTURE = FIXTURES / "03_numeric_mismatch.json"


class Env:
    """Seeded tenant + drafted note + app client + tokens for every role that matters."""

    def __init__(self, seeded, note_id, client, settings, factories):
        self.seeded = seeded
        self.note_id = note_id
        self.client = client
        self.settings = settings
        self.factories = factories

    def headers(self, role: str = "clinician", user_id=None) -> dict[str, str]:
        uid = user_id or (self.seeded.clinician_id if role == "clinician" else f"dev:{role}")
        return bearer(token(self.settings, tenant_id=self.seeded.tenant.id, user_id=uid, role=role))

    async def other_clinician(self):
        return await self.factories.user(self.seeded.tenant.id, "clinician")

    async def auditor(self):
        return await self.factories.user(self.seeded.tenant.id, "auditor")


@pytest.fixture
async def keycache(settings):
    return make_keycache(settings)


@pytest.fixture
async def env(factories, app_engine, owner_engine, settings, redis, keycache):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    ctx = make_ctx(app_engine, settings, redis)
    outcome = await service.draft_for_session(ctx, seeded.session_id, tenant_id=seeded.tenant.id)
    assert outcome.status == "verified"
    app = build_app(app_engine, redis, settings, keycache)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notes") as client:
        yield Env(seeded, outcome.note_id, client, settings, factories)


@pytest.fixture
async def review_env(factories, app_engine, owner_engine, settings, redis, keycache):
    """A ``needs_review`` note: fixture 03 has one ``numeric_mismatch`` statement among supported ones."""
    provider = RecordedProvider(NEEDS_REVIEW_FIXTURE)
    script = [(s.seq, s.speaker, s.text) for s in provider.fixture.segments]
    seeded = await seed(factories, app_engine, owner_engine, settings, script=script)
    ctx = make_ctx(app_engine, settings, redis)
    outcome = await service.draft_for_session(
        ctx, seeded.session_id, tenant_id=seeded.tenant.id, provider=provider
    )
    assert outcome.status == "needs_review" and outcome.unsupported_count == 1
    app = build_app(app_engine, redis, settings, keycache)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notes") as client:
        yield Env(seeded, outcome.note_id, client, settings, factories)


async def audit_rows(engine, tenant_id, action: str) -> list[AuditEvent]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        return list((await s.scalars(select(AuditEvent).where(AuditEvent.action == action))).all())


# --------------------------------------------------------------------------- reads


async def test_owner_reads_latest_and_by_id_with_audit(env, app_engine):
    r = await env.client.get(f"/v1/sessions/{env.seeded.session_id}/notes/latest", headers=env.headers())
    assert r.status_code == 200, r.text
    note = r.json()
    assert note["id"] == str(env.note_id) and note["status"] == "verified" and note["coverage"] == 1.0
    assert note["version"] == 1 and note["assessment"] is None and note["legal_hold"] is None
    sections = {sec: sum(st["section"] == sec for st in note["statements"]) for sec in "SOP"}
    assert sections == EXPECTED_SECTIONS
    for st in note["statements"]:
        assert st["text"] and st["verdict"] == "supported" and st["decision"] is None
        ev = st["evidence"][0]
        assert (
            ev["quote"] == env.seeded.texts[ev["seq"]] and ev["start"] == 0 and ev["end"] == len(ev["quote"])
        )

    r2 = await env.client.get(f"/v1/notes/{env.note_id}", headers={**env.headers(), "x-request-id": "req-1"})
    assert r2.status_code == 200 and r2.json() == note

    reads = await audit_rows(app_engine, env.seeded.tenant.id, "note.read")
    assert len(reads) == 2 and {r.actor_id for r in reads} == {env.seeded.clinician_id}
    assert any(r.request_id == "req-1" for r in reads)
    assert all(r.detail["redacted"] is False for r in reads)


async def test_missing_note_is_problem_json(env):
    r = await env.client.get(f"/v1/sessions/{env.seeded.patient_id}/notes/latest", headers=env.headers())
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == service.NOTE_NOT_FOUND


async def test_other_clinician_and_wrong_roles_are_refused(env):
    other = await env.other_clinician()
    r = await env.client.get(f"/v1/notes/{env.note_id}", headers=env.headers("clinician", other.id))
    assert r.status_code == 403 and r.json()["code"] == "CW-4030"
    for role in ("staff", "admin", "recorder"):
        r = await env.client.get(f"/v1/notes/{env.note_id}", headers=env.headers(role))
        assert r.status_code == 403, role
    r = await env.client.get(f"/v1/notes/{env.note_id}")
    assert r.status_code == 401


async def test_auditor_gets_metadata_only(env, app_engine):
    auditor = await env.auditor()
    r = await env.client.get(f"/v1/notes/{env.note_id}", headers=env.headers("auditor", auditor.id))
    assert r.status_code == 200, r.text
    note = r.json()
    assert note["status"] == "verified" and len(note["statements"]) == sum(EXPECTED_SECTIONS.values())
    body = r.text
    for text in env.seeded.texts.values():
        assert text not in body, "transcript text must not reach an auditor"
    for st in note["statements"]:
        assert st["text"] is None and st["edited_text"] is None
        assert all(e["quote"] is None and e["seq"] is not None for e in st["evidence"])
    reads = await audit_rows(app_engine, env.seeded.tenant.id, "note.read")
    assert reads[-1].actor_role == "auditor" and reads[-1].detail["redacted"] is True
    # auditor may read but never review
    r = await env.client.post(
        f"/v1/notes/{env.note_id}/statements/1/decision",
        json={"decision": "accept"},
        headers=env.headers("auditor", auditor.id),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- manual draft


async def test_manual_draft_request_enqueues_and_the_worker_drafts_a_new_version(
    env, app_engine, settings, redis
):
    import chartwire.worker.handlers.note_draft  # noqa: F401

    r = await env.client.post(f"/v1/sessions/{env.seeded.session_id}/notes/draft", headers=env.headers())
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] is True and body["event_id"]
    async with tenant_tx(app_engine, TenantCtx.service(env.seeded.tenant.id)) as s:
        row = (await s.scalars(select(OutboxEvent).where(OutboxEvent.id == body["event_id"]))).one()
    assert row.event_type == "session.transcribed" and row.status == "pending"
    assert row.payload["session_id"] == str(env.seeded.session_id) and row.payload["manual"] is True

    other = await env.other_clinician()
    r = await env.client.post(
        f"/v1/sessions/{env.seeded.session_id}/notes/draft", headers=env.headers("clinician", other.id)
    )
    assert r.status_code == 403

    stats = await Poller(
        make_ctx(app_engine, settings, redis),
        worker_id="d",
        registry=REGISTRY,
        tenants=[env.seeded.tenant.id],
    ).run_once()
    assert stats.processed == 1
    r = await env.client.get(f"/v1/sessions/{env.seeded.session_id}/notes/latest", headers=env.headers())
    assert r.json()["version"] == 2
    assert len(await audit_rows(app_engine, env.seeded.tenant.id, "note.draft_requested")) == 1


# --------------------------------------------------------------------------- review → sign


async def test_decision_assessment_sign_flow_with_refusals(review_env, app_engine, keycache):
    env = review_env
    note_url = f"/v1/notes/{env.note_id}"
    r = await env.client.get(note_url, headers=env.headers())
    note = r.json()
    assert note["status"] == "needs_review"
    unsupported = [st for st in note["statements"] if st["verdict"] == "unsupported"]
    supported = [st for st in note["statements"] if st["verdict"] == "supported"]
    assert len(unsupported) == 1 and unsupported[0]["verdict_reason"] == "numeric_mismatch"

    # 1. sign without an assessment → 409
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_ASSESSMENT_MISSING

    # 2. assessment (the only write path into note_assessments; encrypted under the record key)
    r = await env.client.put(
        f"{note_url}/assessment",
        json={"text": "  임상적 평가는 임상의가 직접 작성한다.  "},
        headers=env.headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["assessment"]["text"] == "임상적 평가는 임상의가 직접 작성한다."
    assert r.json()["assessment"]["author_id"] == str(env.seeded.clinician_id)
    async with tenant_tx(app_engine, TenantCtx.service(env.seeded.tenant.id)) as s:
        row = await s.get(NoteAssessment, env.note_id)
    assert row is not None
    plain = Envelope.decrypt(
        env.seeded.record_key, row.text_enc, service.assessment_aad(env.seeded.tenant.id, env.note_id)
    )
    assert plain.decode() == "임상적 평가는 임상의가 직접 작성한다."
    r = await env.client.put(f"{note_url}/assessment", json={"text": "   "}, headers=env.headers())
    assert r.status_code == 422

    # 3. sign with the unsupported statement undecided → 409
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_UNSUPPORTED_PENDING

    # 4. accepting an unsupported statement does not unblock signing
    sid = unsupported[0]["id"]
    r = await env.client.post(
        f"{note_url}/statements/{sid}/decision", json={"decision": "accept"}, headers=env.headers()
    )
    assert r.status_code == 200
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_UNSUPPORTED_PENDING

    # 5. edit without text → 422; extra keys → 422; unknown statement → 404
    r = await env.client.post(
        f"{note_url}/statements/{sid}/decision", json={"decision": "edit"}, headers=env.headers()
    )
    assert r.status_code == 422 and r.json()["code"] == service.NOTE_EDIT_TEXT_REQUIRED
    r = await env.client.post(
        f"{note_url}/statements/{sid}/decision",
        json={"decision": "accept", "verdict": "supported"},
        headers=env.headers(),
    )
    assert r.status_code == 422
    r = await env.client.post(
        f"{note_url}/statements/999999/decision", json={"decision": "reject"}, headers=env.headers()
    )
    assert r.status_code == 404

    # 6. reject the unsupported one, edit one supported one, accept another
    r = await env.client.post(
        f"{note_url}/statements/{sid}/decision", json={"decision": "reject"}, headers=env.headers()
    )
    assert r.status_code == 200
    edited_id = supported[0]["id"]
    r = await env.client.post(
        f"{note_url}/statements/{edited_id}/decision",
        json={"decision": "edit", "edited_text": "잠들기까지 약 2시간이 걸린다고 보고함"},
        headers=env.headers(),
    )
    assert r.status_code == 200
    edited = next(st for st in r.json()["statements"] if st["id"] == edited_id)
    assert edited["decision"] == "edit" and edited["edited_text"] == "잠들기까지 약 2시간이 걸린다고 보고함"
    async with tenant_tx(app_engine, TenantCtx.service(env.seeded.tenant.id)) as s:
        st_row = await s.get(NoteStatement, edited_id)
    assert st_row is not None and st_row.edited_text_enc is not None
    assert (
        Envelope.decrypt(
            env.seeded.dek,
            st_row.edited_text_enc,
            service.statement_aad(
                env.seeded.tenant.id, env.note_id, st_row.section.strip(), st_row.ordinal, edited=True
            ),
        ).decode()
        == "잠들기까지 약 2시간이 걸린다고 보고함"
    )
    if len(supported) > 1:
        r = await env.client.post(
            f"{note_url}/statements/{supported[1]['id']}/decision",
            json={"decision": "accept"},
            headers=env.headers(),
        )
        assert r.status_code == 200
    decisions = await audit_rows(app_engine, env.seeded.tenant.id, "note.decision")
    assert len(decisions) >= 4 and {d.detail["decision"] for d in decisions} == {"accept", "reject", "edit"}

    # 7. sign → medical record
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 200, r.text
    signed = r.json()
    assert signed["status"] == "signed" and signed["legal_hold"] == "medical_record"
    assert signed["signed_by"] == str(env.seeded.clinician_id)
    signed_at = datetime.fromisoformat(signed["signed_at"])
    assert datetime.fromisoformat(signed["retention_until"]) == signed_at.replace(year=signed_at.year + 10)
    assert all(st["decision"] != "reject" for st in signed["statements"]), (
        "rejected statements are not in the record"
    )
    assert len(signed["statements"]) == len(note["statements"]) - 1
    assert (
        next(st for st in signed["statements"] if st["id"] == edited_id)["text"]
        == "잠들기까지 약 2시간이 걸린다고 보고함"
    )
    assert signed["assessment"]["text"] == "임상적 평가는 임상의가 직접 작성한다."
    async with tenant_tx(app_engine, TenantCtx.service(env.seeded.tenant.id)) as s:
        sess = await s.get(SessionModel, env.seeded.session_id)
        note_row = await s.get(Note, env.note_id)
    assert sess is not None and sess.state == "signed" and sess.signed_at is not None
    assert (
        note_row is not None
        and note_row.signed_content_enc is not None
        and note_row.retention_until is not None
    )
    doc = json.loads(
        Envelope.decrypt(
            env.seeded.record_key,
            note_row.signed_content_enc,
            service.signed_content_aad(env.seeded.tenant.id, env.note_id),
        )
    )
    assert doc["schema"] == service.SIGNED_SCHEMA and doc["clinician_id"] == str(env.seeded.clinician_id)
    assert doc["session_id"] == str(env.seeded.session_id) and doc["assessment"]["text"]
    assert all(e["quote"] for sec in doc["sections"].values() for st in sec for e in st["evidence"]), (
        "verbatim quotes embedded"
    )
    assert len(await audit_rows(app_engine, env.seeded.tenant.id, "note.signed")) == 1
    assert len(await audit_rows(app_engine, env.seeded.tenant.id, "session.signed")) == 1

    # 8. a signed note is immutable through the API
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_ALREADY_SIGNED
    r = await env.client.put(f"{note_url}/assessment", json={"text": "변경 시도"}, headers=env.headers())
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_ALREADY_SIGNED
    r = await env.client.post(
        f"{note_url}/statements/{edited_id}/decision", json={"decision": "reject"}, headers=env.headers()
    )
    assert r.status_code == 409 and r.json()["code"] == service.NOTE_ALREADY_SIGNED


async def test_abstained_note_cannot_be_reviewed_or_signed(
    factories, app_engine, owner_engine, settings, redis, keycache
):
    seeded = await seed(factories, app_engine, owner_engine, settings, scopes=["recording", "transcription"])
    outcome = await service.draft_for_session(
        make_ctx(app_engine, settings), seeded.session_id, tenant_id=seeded.tenant.id
    )
    assert outcome.abstain_reason == "consent_scope_missing"
    app = build_app(app_engine, redis, settings, keycache)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notes") as client:
        headers = bearer(
            token(settings, tenant_id=seeded.tenant.id, user_id=seeded.clinician_id, role="clinician")
        )
        r = await client.get(f"/v1/notes/{outcome.note_id}", headers=headers)
        assert (
            r.status_code == 200
            and r.json()["abstain_reason"] == "consent_scope_missing"
            and r.json()["statements"] == []
        )
        r = await client.post(f"/v1/notes/{outcome.note_id}/sign", headers=headers)
        assert r.status_code == 409 and r.json()["code"] == service.NOTE_NOT_REVIEWABLE
        r = await client.put(
            f"/v1/notes/{outcome.note_id}/assessment", json={"text": "평가"}, headers=headers
        )
        assert r.status_code == 409 and r.json()["code"] == service.NOTE_NOT_REVIEWABLE


# --------------------------------------------------------------------------- ADR-0004: survival


async def test_signed_note_survives_purge_of_segments_and_session_dek(env, app_engine, keycache):
    note_url = f"/v1/notes/{env.note_id}"
    await env.client.put(f"{note_url}/assessment", json={"text": "서명 전 평가"}, headers=env.headers())
    r = await env.client.post(f"{note_url}/sign", headers=env.headers())
    assert r.status_code == 200, r.text
    before = r.json()

    await purge_like(app_engine, env.seeded, keycache)

    # the session DEK is gone: statement ciphertext can no longer be opened by anyone
    async with tenant_tx(app_engine, TenantCtx.service(env.seeded.tenant.id)) as s:
        sess = await s.get(SessionModel, env.seeded.session_id)
        rows = list(await notes_repo.list_statements(s, env.note_id))
        note_row = await s.get(Note, env.note_id)
    assert sess is not None and sess.dek_wrapped is None and sess.state == "purged" and rows
    with pytest.raises(DekDestroyedError):
        keycache.get(env.seeded.session_id, env.seeded.tenant.kek_ref, sess.dek_wrapped)

    # …but the signed record reads back in full through the API (record key, self-contained content)
    r = await env.client.get(note_url, headers=env.headers())
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["status"] == "signed" and after["legal_hold"] == "medical_record"
    assert after["statements"] == before["statements"] and after["assessment"] == before["assessment"]
    assert all(e["quote"] for st in after["statements"] for e in st["evidence"])
    r = await env.client.get(f"/v1/sessions/{env.seeded.session_id}/notes/latest", headers=env.headers())
    assert r.status_code == 200 and r.json()["id"] == str(env.note_id)

    # and directly: the blob is under the tenant record key, never the session DEK
    assert note_row is not None and note_row.signed_content_enc is not None
    doc = json.loads(
        Envelope.decrypt(
            env.seeded.record_key,
            note_row.signed_content_enc,
            service.signed_content_aad(env.seeded.tenant.id, env.note_id),
        )
    )
    assert doc["schema"] == service.SIGNED_SCHEMA
    with pytest.raises(DecryptError):
        Envelope.decrypt(
            env.seeded.dek,
            note_row.signed_content_enc,
            service.signed_content_aad(env.seeded.tenant.id, env.note_id),
        )


async def test_unsigned_draft_of_a_purged_session_is_not_readable(env, app_engine, keycache):
    await purge_like(app_engine, env.seeded, keycache)
    r = await env.client.get(f"/v1/notes/{env.note_id}", headers=env.headers())
    assert r.status_code == 404
    r = await env.client.post(f"/v1/sessions/{env.seeded.session_id}/notes/draft", headers=env.headers())
    assert r.status_code == 409
