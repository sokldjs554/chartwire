"""``notes.service.draft_for_session`` against the real PostgreSQL/Redis (spec §7.2, §9, §8.3).

Extractive draft → ``verified`` with every statement and the raw draft persisted encrypted under the
session DEK and decrypting back; the abstain paths (consent scope missing, provider error, schema
rejected twice); the recorded LLM fixtures through the identical service path; the outbox handler
registered and executed by the real poller runtime; a purged session drafts nothing.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.config import Settings
from chartwire.crypto.envelope import Envelope
from chartwire.db.models import AuditEvent, Note, NoteStatement, ProcessedEvent
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import notes as notes_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.notes import service
from chartwire.notes.extractive import build_draft
from chartwire.notes.providers.base import ProviderError, Stopwatch
from chartwire.notes.providers.recorded import RecordedProvider, iter_fixtures
from chartwire.notes.schema import DraftContext, RawDraft
from chartwire.outbox import writer
from chartwire.outbox.poller import Poller
from chartwire.outbox.registry import REGISTRY
from chartwire.redis import keys
from tests.integration.notes_support import EXPECTED_SECTIONS, make_ctx, purge_like, seed, sha256_hex

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic"


class FailingProvider:
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    async def draft(self, ctx: DraftContext) -> RawDraft:
        self.calls += 1
        raise ProviderError("simulated transport failure")


class BadSchemaProvider:
    """Returns a draft with an ``assessment`` key — exactly what the schema must reject (§0.5)."""

    name = "badschema"

    def __init__(self) -> None:
        self.calls = 0

    async def draft(self, ctx: DraftContext) -> RawDraft:
        self.calls += 1
        watch = Stopwatch()
        body = {"statements": [], "assessment": "주요우울장애 의심"}
        return RawDraft(
            text=json.dumps(body, ensure_ascii=False),
            provider=self.name,
            model="fake",
            prompt_hash=None,
            latency_ms=watch.elapsed_ms,
        )


async def note_rows(engine: AsyncEngine, seeded) -> tuple[Note, list[NoteStatement]]:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant.id)) as s:
        note = await notes_repo.latest_for_session(s, seeded.session_id)
        assert note is not None
        return note, list(await notes_repo.list_statements(s, note.id))


async def session_state(engine: AsyncEngine, seeded) -> str:
    async with tenant_tx(engine, TenantCtx.service(seeded.tenant.id)) as s:
        row = await sessions_repo.get_session(s, seeded.session_id)
        assert row is not None
        return row.state


async def audit_actions(engine: AsyncEngine, tenant_id, action: str) -> list[AuditEvent]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == action)
        return list((await s.scalars(stmt)).all())


# --------------------------------------------------------------------------- happy path


async def test_extractive_draft_is_verified_and_round_trips(
    factories, app_engine, owner_engine, settings, redis
):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    pubsub = redis.pubsub()
    await pubsub.subscribe(keys.sess_events(seeded.session_id))
    ctx = make_ctx(app_engine, settings, redis)

    outcome = await service.draft_for_session(ctx, seeded.session_id, tenant_id=seeded.tenant.id)

    assert outcome.status == "verified" and outcome.abstain_reason is None
    assert outcome.version == 1 and outcome.provider == "extractive"
    assert outcome.coverage == 1.0 and outcome.unsupported_count == 0
    assert outcome.statement_count == sum(EXPECTED_SECTIONS.values())
    assert outcome.published

    note, rows = await note_rows(app_engine, seeded)
    assert note.status == "verified" and note.version == 1 and note.provider == "extractive"
    assert float(note.grounding_coverage) == 1.0 and note.statement_count == len(rows)
    assert note.legal_hold is None and note.signed_content_enc is None

    # raw provider output is stored under the session DEK, bound to this note by AAD
    assert note.raw_draft_enc is not None
    raw = Envelope.decrypt(seeded.dek, note.raw_draft_enc, service.raw_draft_aad(seeded.tenant.id, note.id))
    assert json.loads(raw)["statements"], "raw draft round-trips"

    # statements: same content as the pure extractive core, every quote hashed, offsets exact
    by_seq = seeded.texts
    expected = build_draft([v for v in (await _views(ctx, seeded))])
    assert len(rows) == len(expected.statements)
    per_section = {"S": 0, "O": 0, "P": 0}
    # ``list_statements`` orders by (section, ordinal): 'O' < 'P' < 'S' in the CHAR column
    for row, st in zip(rows, sorted(expected.statements, key=lambda x: x.section), strict=True):
        section = row.section.strip()
        assert section == st.section and row.ordinal == per_section[section]
        per_section[section] += 1
        text = Envelope.decrypt(
            seeded.dek, row.text_enc, service.statement_aad(seeded.tenant.id, note.id, section, row.ordinal)
        )
        assert text.decode() == st.text
        assert row.verdict == "supported" and row.verdict_reason is None and row.method == "exact"
        ev = row.evidence[0]
        assert ev["quote_hash"] == sha256_hex(st.evidence[0].quote)
        assert by_seq[ev["seq"]][ev["start"] : ev["end"]] == st.evidence[0].quote
        assert "quote" not in ev and "text" not in ev, "no transcript text in JSONB"

    # the read model rebuilds the verbatim quotes from the encrypted raw draft
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        view = await service.read_note(s, ctx.keycache, note)
    assert [st.evidence[0].quote for st in view.statements] == [by_seq[r.evidence[0]["seq"]] for r in rows]
    assert view.clinician_id == seeded.clinician_id and view.assessment is None

    assert await session_state(app_engine, seeded) == "drafted"
    drafted = await audit_actions(app_engine, seeded.tenant.id, "note.drafted")
    assert (
        len(drafted) == 1 and drafted[0].detail["status"] == "verified" and drafted[0].actor_role == "service"
    )
    assert len(await audit_actions(app_engine, seeded.tenant.id, "session.drafted")) == 1

    msg = await _next_message(pubsub)
    assert msg == {
        "t": "note.status",
        "note_id": str(note.id),
        "status": "verified",
        "coverage": 1.0,
        "unsupported_count": 0,
    }
    await pubsub.aclose()


async def _views(ctx, seeded):
    from chartwire.notes.repo_adapter import load_segment_views

    async with tenant_tx(ctx.engine, TenantCtx.service(seeded.tenant.id)) as s:
        return await load_segment_views(
            s, session_id=seeded.session_id, dek=seeded.dek, started_at=seeded.started_at
        )


async def _next_message(pubsub, timeout_s: float = 3.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if raw is not None:
            return json.loads(raw["data"])
    raise AssertionError("no note.status message published")


async def test_redraft_creates_next_version_and_keeps_history(factories, app_engine, owner_engine, settings):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    ctx = make_ctx(app_engine, settings)
    first = await service.draft_for_session(ctx, seeded.session_id, tenant_id=seeded.tenant.id)
    second = await service.draft_for_session(ctx, seeded.session_id, tenant_id=seeded.tenant.id)
    assert (first.version, second.version) == (1, 2) and first.note_id != second.note_id
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        count = (
            await s.execute(
                select(func.count()).select_from(Note).where(Note.session_id == seeded.session_id)
            )
        ).scalar_one()
        latest = await notes_repo.latest_for_session(s, seeded.session_id)
    assert count == 2 and latest is not None and latest.id == second.note_id


async def test_tenant_is_discovered_when_not_given(factories, app_engine, owner_engine, settings):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    ctx = make_ctx(app_engine, settings)
    outcome = await service.draft_for_session(ctx, seeded.session_id)
    assert outcome.status == "verified"


# --------------------------------------------------------------------------- abstain paths


async def test_missing_ai_drafting_scope_abstains_without_touching_segments(
    factories, app_engine, owner_engine, settings
):
    seeded = await seed(factories, app_engine, owner_engine, settings, scopes=["recording", "transcription"])
    ctx = make_ctx(app_engine, settings)
    provider = FailingProvider()  # must never be called: the gate comes first

    outcome = await service.draft_for_session(
        ctx, seeded.session_id, tenant_id=seeded.tenant.id, provider=provider
    )

    assert outcome.status == "abstained" and outcome.abstain_reason == "consent_scope_missing"
    assert provider.calls == 0 and outcome.statement_count == 0
    note, rows = await note_rows(app_engine, seeded)
    assert note.status == "abstained" and note.abstain_reason == "consent_scope_missing"
    assert note.raw_draft_enc is None and rows == []
    assert await session_state(app_engine, seeded) == "drafted"
    assert seeded.session_id not in ctx.keycache, "the DEK was never unwrapped"


async def test_revoked_consent_abstains(factories, app_engine, owner_engine, settings):
    from datetime import UTC, datetime

    from chartwire.db.repo import patients as patients_repo

    seeded = await seed(factories, app_engine, owner_engine, settings)
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        consent = await patients_repo.latest_active_consent(s, seeded.patient_id)
        assert consent is not None
        await patients_repo.revoke_consent(
            s, consent.id, revoked_by=None, reason="test", now=datetime.now(tz=UTC)
        )
    outcome = await service.draft_for_session(
        make_ctx(app_engine, settings), seeded.session_id, tenant_id=seeded.tenant.id
    )
    assert (outcome.status, outcome.abstain_reason) == ("abstained", "consent_scope_missing")


async def test_provider_error_abstains_and_never_falls_back(factories, app_engine, owner_engine, settings):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    provider = FailingProvider()
    outcome = await service.draft_for_session(
        make_ctx(app_engine, settings), seeded.session_id, tenant_id=seeded.tenant.id, provider=provider
    )
    assert (outcome.status, outcome.abstain_reason) == ("abstained", "provider_error")
    assert provider.calls == 1
    note, rows = await note_rows(app_engine, seeded)
    assert note.provider == "failing" and rows == [] and note.raw_draft_enc is None


async def test_schema_rejected_twice_abstains_with_schema_reason(
    factories, app_engine, owner_engine, settings
):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    provider = BadSchemaProvider()
    outcome = await service.draft_for_session(
        make_ctx(app_engine, settings), seeded.session_id, tenant_id=seeded.tenant.id, provider=provider
    )
    assert (outcome.status, outcome.abstain_reason) == ("abstained", "schema")
    assert provider.calls == 2, "one retry, then abstain"
    note, rows = await note_rows(app_engine, seeded)
    assert rows == [] and note.model == "fake"
    assert note.raw_draft_enc is not None, "the rejected output is kept (encrypted) for inspection"
    raw = Envelope.decrypt(seeded.dek, note.raw_draft_enc, service.raw_draft_aad(seeded.tenant.id, note.id))
    assert "assessment" in json.loads(raw)


async def test_anthropic_selected_without_model_abstains_provider_error(
    factories, app_engine, owner_engine, settings
):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    cfg = Settings(note_provider="anthropic", anthropic_model=None)
    outcome = await service.draft_for_session(
        make_ctx(app_engine, cfg), seeded.session_id, tenant_id=seeded.tenant.id
    )
    assert (outcome.status, outcome.abstain_reason, outcome.provider) == (
        "abstained",
        "provider_error",
        "anthropic",
    )


async def test_purged_session_is_skipped(factories, app_engine, owner_engine, settings):
    seeded = await seed(factories, app_engine, owner_engine, settings)
    ctx = make_ctx(app_engine, settings)
    await purge_like(app_engine, seeded, ctx.keycache)
    outcome = await service.draft_for_session(ctx, seeded.session_id, tenant_id=seeded.tenant.id)
    assert outcome.skipped and outcome.status == "skipped"
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        assert await notes_repo.latest_for_session(s, seeded.session_id) is None


# --------------------------------------------------------------------------- recorded LLM fixtures


@pytest.mark.parametrize("fixture", iter_fixtures(FIXTURES), ids=lambda f: f.name)
async def test_recorded_fixture_through_the_service(fixture, factories, app_engine, owner_engine, settings):
    """Each hand-made model response drives the real service: same parse → verify → decide → persist path."""
    script = [(seg.seq, seg.speaker, seg.text) for seg in fixture.segments]
    seeded = await seed(factories, app_engine, owner_engine, settings, script=script)
    provider = RecordedProvider(FIXTURES / f"{fixture.name}.json")
    outcome = await service.draft_for_session(
        make_ctx(app_engine, settings), seeded.session_id, tenant_id=seeded.tenant.id, provider=provider
    )
    assert outcome.status == fixture.expected.status.value
    note, rows = await note_rows(app_engine, seeded)
    assert note.model == fixture.model and note.provider == "recorded"
    if fixture.expected.schema_ok:
        assert len(rows) == len(fixture.raw["statements"])
        assert sum(r.verdict == "supported" for r in rows) == fixture.expected.supported
        reasons = [r.verdict_reason for r in rows if r.verdict_reason]
        assert sorted(reasons) == sorted(fixture.expected.reasons)
    else:
        assert rows == [] and note.abstain_reason == "schema"


# --------------------------------------------------------------------------- outbox handler


async def test_handler_registered_and_run_by_the_poller(factories, app_engine, owner_engine, settings, redis):
    import chartwire.worker.handlers.note_draft  # noqa: F401  (import registers)

    spec = REGISTRY.get("session.transcribed")
    assert spec.name == "note_draft" and spec.lease_s == 120

    seeded = await seed(factories, app_engine, owner_engine, settings)
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        event_id = await writer.emit(
            s,
            tenant_id=seeded.tenant.id,
            aggregate_type="session",
            aggregate_id=seeded.session_id,
            event_type="session.transcribed",
            payload={"session_id": str(seeded.session_id), "patient_id": str(seeded.patient_id)},
            idempotency_key=writer.idempotency_key("session.transcribed", seeded.session_id, 1),
        )
    assert event_id is not None
    ctx = make_ctx(app_engine, settings, redis)
    await asyncio.sleep(0.05)  # ``next_attempt_at`` defaults to the emitting tx's now()
    poller = Poller(ctx, worker_id="test-d", registry=REGISTRY, tenants=[seeded.tenant.id])
    stats = await poller.run_once()
    assert stats.processed == 1 and stats.failed == 0

    note, rows = await note_rows(app_engine, seeded)
    assert note.status == "verified" and len(rows) == sum(EXPECTED_SECTIONS.values())
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        processed = (await s.scalars(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id))).all()
        sess = await s.get(SessionModel, seeded.session_id)
    assert [p.handler for p in processed] == ["note_draft"]
    assert sess is not None and sess.state == "drafted"

    # a redelivery of the same event is skipped by ``processed_events`` — no second version
    from chartwire.outbox.context import OutboxEvent as Event
    from chartwire.outbox.runtime import run_handler

    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        from chartwire.db.models import OutboxEvent

        row = (await s.scalars(select(OutboxEvent).where(OutboxEvent.id == event_id))).one()
        event = Event.from_row({c.name: getattr(row, c.name) for c in OutboxEvent.__table__.columns})
    assert await run_handler(ctx, spec, event) == "skipped"
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        assert await notes_repo.next_version(s, seeded.session_id) == 2


# --------------------------------------------------------------------------- §0.9: nothing PHI in logs


async def test_draft_logs_carry_no_transcript_text(
    factories, app_engine, owner_engine, settings, redis, caplog
):
    import logging

    seeded = await seed(factories, app_engine, owner_engine, settings)
    with caplog.at_level(logging.DEBUG):
        outcome = await service.draft_for_session(make_ctx(app_engine, settings, redis), seeded.session_id)
        bad = await service.draft_for_session(
            make_ctx(app_engine, settings),
            seeded.session_id,
            tenant_id=seeded.tenant.id,
            provider=BadSchemaProvider(),
        )
    assert outcome.status == "verified" and bad.abstain_reason == "schema"
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "note drafted" in logged and "draft schema rejected" in logged
    for text in seeded.texts.values():
        assert text not in logged
    assert "주요우울장애" not in logged, "the rejected provider output is not logged either"


def test_retention_is_ten_calendar_years_even_from_a_leap_day():
    from datetime import UTC, datetime

    assert service.retention_until(datetime(2026, 9, 3, 12, 0, tzinfo=UTC)) == datetime(
        2036, 9, 3, 12, 0, tzinfo=UTC
    )
    assert service.retention_until(datetime(2028, 2, 29, 9, 0, tzinfo=UTC)) == datetime(
        2038, 3, 1, 9, 0, tzinfo=UTC
    )
