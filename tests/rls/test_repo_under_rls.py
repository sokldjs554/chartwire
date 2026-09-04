"""Repository contracts (§4.5) exercised as ``chartwire_app`` under a tenant context."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from chartwire.core.clock import FakeClock
from chartwire.core.ids import uuid7
from chartwire.db.repo import notes as notes_repo
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import purge as purge_repo
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.outbox import writer

pytestmark = pytest.mark.integration


async def _segment(session, row, seq: int, at: datetime):
    return await segments_repo.insert_final(
        session,
        tenant_id=row.tenant_id,
        session_id=row.id,
        patient_id=row.patient_id,
        seq=seq,
        speaker="patient",
        t_start_ms=seq * 1000,
        t_end_ms=seq * 1000 + 900,
        text_enc=bytes([seq]) * 16,
        text_len=10,
        confidence=0.8,
        provider="simulator",
        created_at=at,
    )


# ------------------------------------------------------------------ segments


async def test_insert_final_is_idempotent_and_timeline_keyset_pages(app_engine, session_a, ctx_a):
    start = session_a.started_at
    async with tenant_tx(app_engine, ctx_a) as session:
        first = await _segment(session, session_a, 1, start + timedelta(seconds=1))
        again = await _segment(session, session_a, 1, start + timedelta(seconds=1))
        second = await _segment(session, session_a, 2, start + timedelta(seconds=2))
        assert first == again and first.id != second.id
        page1 = await segments_repo.timeline(
            session,
            tenant_id=ctx_a.tenant_id,
            patient_id=session_a.patient_id,
            since=start - timedelta(days=1),
            limit=1,
        )
        page2 = await segments_repo.timeline(
            session,
            tenant_id=ctx_a.tenant_id,
            patient_id=session_a.patient_id,
            since=start - timedelta(days=1),
            before=(page1[0].created_at, page1[0].id),
            limit=1,
        )
        by_key = await segments_repo.by_keys(session, [(first.created_at, first.id)])
    assert [p.seq for p in page1] == [2] and [p.seq for p in page2] == [1]
    assert by_key == [first]


# ------------------------------------------------------------------ outbox


async def _tx_clock(session) -> FakeClock:
    """``next_attempt_at`` defaults to the database ``now()`` (= transaction start); a fake clock
    that starts earlier would never see rows emitted in this transaction as due."""
    return FakeClock((await session.execute(select(func.now()))).scalar_one())


async def test_outbox_claim_retry_dead_letter_and_replay(app_engine, tenant_a):
    ctx = TenantCtx.service(tenant_a.id)
    aggregate = uuid7()
    async with tenant_tx(app_engine, ctx) as session:
        fake_clock = await _tx_clock(session)
        now = fake_clock.now()
        eid = await writer.emit(
            session,
            tenant_id=tenant_a.id,
            aggregate_type="session",
            aggregate_id=aggregate,
            event_type="session.transcribed",
            payload={"session_id": str(aggregate)},
            idempotency_key=writer.idempotency_key("session.transcribed", aggregate, 1),
        )
        dup = await writer.emit(
            session,
            tenant_id=tenant_a.id,
            aggregate_type="session",
            aggregate_id=aggregate,
            event_type="session.transcribed",
            payload={},
            idempotency_key=writer.idempotency_key("session.transcribed", aggregate, 1),
        )
    assert eid is not None and dup is None

    async with tenant_tx(app_engine, ctx) as session:
        claimed = await outbox_repo.claim_batch(session, "w1", now=now)
        assert [e.id for e in claimed] == [eid] and claimed[0].status == "in_flight"
        assert await outbox_repo.claim_batch(session, "w2", now=now) == []  # in_flight is not claimable
        assert await outbox_repo.mark_processed(
            session, handler="note_draft", event_id=eid, tenant_id=tenant_a.id
        )
        assert not await outbox_repo.mark_processed(
            session, handler="note_draft", event_id=eid, tenant_id=tenant_a.id
        )

    rng = random.Random(1)
    for attempt in range(1, outbox_repo.DEFAULT_MAX_ATTEMPTS):
        async with tenant_tx(app_engine, ctx) as session:
            status = await outbox_repo.mark_failed(session, eid, error="boom", now=now, rng=rng)
            assert status == "pending"
            claimable = await outbox_repo.claim_batch(session, "w1", now=now)
            assert claimable == []  # backoff keeps it out of reach until next_attempt_at
        fake_clock.advance(outbox_repo.MAX_BACKOFF_S * 1.3)
        now = fake_clock.now()
        async with tenant_tx(app_engine, ctx) as session:
            assert [e.attempts for e in await outbox_repo.claim_batch(session, "w1", now=now)] == [attempt]
    async with tenant_tx(app_engine, ctx) as session:
        assert await outbox_repo.mark_failed(session, eid, error="boom", now=now) == "dead"
        dead = await outbox_repo.list_dead_letters(session)
        stats = await outbox_repo.stats(session, now=now)
    assert [d.outbox_event_id for d in dead] == [eid] and dead[0].attempts == outbox_repo.DEFAULT_MAX_ATTEMPTS
    assert stats["dead"] == 1 and stats["pending"] == 0

    async with tenant_tx(app_engine, ctx) as session:
        replayed = await outbox_repo.replay_dead(session, eid, now=now)
        assert replayed is not None and replayed.attempts == 0 and replayed.status == "pending"
        (event,) = await outbox_repo.claim_batch(session, "w1", now=now)
        await outbox_repo.mark_done(session, event.id, now=now)
        assert (await outbox_repo.stats(session, now=now))["done"] == 1
        assert await outbox_repo.prune_done(session, now=now + timedelta(hours=25)) == 1


async def test_outbox_lease_reclaim_and_tenant_scoping(app_engine, tenant_a, tenant_b):
    for tenant in (tenant_a, tenant_b):
        async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as session:
            now = (await _tx_clock(session)).now()
            await outbox_repo.insert_event(
                session,
                tenant_id=tenant.id,
                aggregate_type="purge_job",
                aggregate_id=uuid7(),
                event_type="purge.requested",
                payload={},
                idempotency_key=f"purge.requested:{tenant.slug}",
            )
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        claimed = await outbox_repo.claim_batch(session, "w1", lease_s=60, now=now)
        assert len(claimed) == 1 and claimed[0].tenant_id == tenant_a.id  # per-tenant polling (ADR-0001)
        assert await outbox_repo.reclaim_stuck(session, now=now + timedelta(seconds=30)) == 0
        assert await outbox_repo.reclaim_stuck(session, now=now + timedelta(seconds=61)) == 1
        assert (await outbox_repo.stats(session, now=now))["pending"] == 1


# ------------------------------------------------------------------ consents / patients


async def test_consent_versions_increment_and_revoke_updates_patient_state(
    app_engine, patient_a, ctx_a, fake_clock
):
    async with tenant_tx(app_engine, ctx_a) as session:
        v1 = await patients_repo.grant_consent(
            session,
            tenant_id=ctx_a.tenant_id,
            patient_id=patient_a.id,
            scopes=["recording"],
            granted_by=ctx_a.user_id,
        )
        v2 = await patients_repo.grant_consent(
            session,
            tenant_id=ctx_a.tenant_id,
            patient_id=patient_a.id,
            scopes=["recording", "transcription"],
            granted_by=ctx_a.user_id,
        )
        latest = await patients_repo.latest_active_consent(session, patient_a.id)
        patient = await patients_repo.get_patient(session, patient_a.id)
        assert (v1.version, v2.version) == (1, 2) and latest is not None and latest.id == v2.id
        assert patient is not None and patient.consent_state == "granted"
        revoked = await patients_repo.revoke_consent(
            session, v2.id, revoked_by=ctx_a.user_id, reason="환자 요청", now=fake_clock.now()
        )
        assert revoked is not None and revoked.revoked_at == fake_clock.now()
        await session.refresh(patient)
        assert patient.consent_state == "revoked"
        # revoking the latest version withdraws consent; the superseded v1 does not resurrect (§8.3,
        # the row form of gates.active_scopes — any other reading would fail open)
        assert await patients_repo.latest_active_consent(session, patient_a.id) is None
        found = await patients_repo.find_patients_by_name_hmac(session, ctx_a.tenant_id, patient_a.name_hmac)
        assert [p.id for p in found] == [patient_a.id]


# ------------------------------------------------------------------ risk


async def test_risk_open_list_ack_and_single_escalation(app_engine, session_a, ctx_a, fake_clock):
    now = fake_clock.now()
    async with tenant_tx(app_engine, ctx_a) as session:
        seg = await _segment(session, session_a, 1, session_a.started_at + timedelta(seconds=1))
        events = [
            await risk_repo.insert_event(
                session,
                tenant_id=ctx_a.tenant_id,
                session_id=session_a.id,
                patient_id=session_a.patient_id,
                segment_id=seg.id,
                segment_created_at=seg.created_at,
                segment_seq=seg.seq,
                category="suicidal_ideation",
                severity=sev,
                phrase="죽고 싶다",
                span_start=0,
                span_end=5,
                scope={"negated": False},
                detector_version="lex-1",
                detected_at=now,
                sla_deadline_at=now + timedelta(seconds=60 if sev == 3 else 300),
            )
            for sev in (2, 3)
        ]
        open_ids = [e.id for e in await risk_repo.list_open(session, ctx_a.tenant_id)]
        assert open_ids == [events[1].id, events[0].id]  # severity 3 has the earlier deadline
        acked = await risk_repo.acknowledge(session, events[1].id, by=ctx_a.user_id, now=now)
        assert acked is not None and acked.acknowledged_by == ctx_a.user_id
        assert [e.id for e in await risk_repo.list_open(session, ctx_a.tenant_id)] == [events[0].id]
        assert await risk_repo.escalate(session, events[0].id, now=now) is not None
        assert await risk_repo.escalate(session, events[0].id, now=now) is None  # one step only
        assert (
            await risk_repo.escalate(session, events[1].id, now=now) is None
        )  # acknowledged: never escalates


# ------------------------------------------------------------------ notes


async def test_note_statements_assessment_and_sign(app_engine, session_a, ctx_a, fake_clock):
    now = fake_clock.now()
    async with tenant_tx(app_engine, ctx_a) as session:
        version = await notes_repo.next_version(session, session_a.id)
        note = await notes_repo.create_note(
            session,
            id=uuid7(),
            tenant_id=ctx_a.tenant_id,
            session_id=session_a.id,
            version=version,
            status="needs_review",
            provider="extractive",
            grounding_coverage=1.0,
            statement_count=1,
        )
        (stmt,) = await notes_repo.insert_statements(
            session,
            [
                {
                    "tenant_id": ctx_a.tenant_id,
                    "note_id": note.id,
                    "section": "S",
                    "ordinal": 1,
                    "text_enc": b"\x01",
                    "evidence": [{"seq": 1, "quote_hash": "ab", "start": 0, "end": 3, "method": "exact"}],
                    "verdict": "supported",
                }
            ],
        )
        decided = await notes_repo.set_decision(session, stmt.id, decision="edit", edited_text_enc=b"\x02")
        assert decided is not None and decided.clinician_decision == "edit"
        await notes_repo.upsert_assessment(
            session,
            note_id=note.id,
            tenant_id=ctx_a.tenant_id,
            text_enc=b"\x03",
            author_id=ctx_a.user_id,
            now=now,
        )
        updated = await notes_repo.upsert_assessment(
            session,
            note_id=note.id,
            tenant_id=ctx_a.tenant_id,
            text_enc=b"\x04",
            author_id=ctx_a.user_id,
            now=now,
        )
        assert updated.text_enc == b"\x04"
        signed = await notes_repo.sign_note(
            session,
            note.id,
            signed_by=ctx_a.user_id,
            signed_at=now,
            signed_content_enc=b"\x05",
            retention_until=now + timedelta(days=3650),
        )
        assert signed is not None and signed.legal_hold == "medical_record" and signed.status == "signed"
        assert version == 1 and await notes_repo.next_version(session, session_a.id) == 2
        latest = await notes_repo.latest_for_session(session, session_a.id)
        assert latest is not None and latest.id == note.id


# ------------------------------------------------------------------ purge


async def test_purge_deletes_everything_but_signed_notes_and_dek_cannot_resurrect(
    app_engine, session_a, ctx_a, fake_clock
):
    now = fake_clock.now()
    svc = TenantCtx.service(ctx_a.tenant_id)
    async with tenant_tx(app_engine, ctx_a) as session:
        seg = await _segment(session, session_a, 1, session_a.started_at + timedelta(seconds=1))
        await sessions_repo.insert_chunks(
            session,
            [
                {
                    "session_id": session_a.id,
                    "seq": 1,
                    "tenant_id": ctx_a.tenant_id,
                    "byte_len": 10,
                    "sha256": bytes(32),
                    "storage_key": "k",
                    "offset_ms": 0,
                    "received_at": now,
                }
            ],
        )
        await sessions_repo.upsert_stt_offset(
            session, session_id=session_a.id, tenant_id=ctx_a.tenant_id, last_chunk_seq=1, last_segment_seq=1
        )
        draft = await notes_repo.create_note(
            session,
            id=uuid7(),
            tenant_id=ctx_a.tenant_id,
            session_id=session_a.id,
            version=1,
            status="verified",
            provider="extractive",
        )
        signed = await notes_repo.create_note(
            session,
            id=uuid7(),
            tenant_id=ctx_a.tenant_id,
            session_id=session_a.id,
            version=2,
            status="signed",
            provider="extractive",
            legal_hold="medical_record",
            signed_content_enc=b"\x09",
        )
        await notes_repo.insert_statements(
            session,
            [
                {
                    "tenant_id": ctx_a.tenant_id,
                    "note_id": n.id,
                    "section": "S",
                    "ordinal": 1,
                    "text_enc": b"x",
                    "evidence": [],
                    "verdict": "supported",
                }
                for n in (draft, signed)
            ],
        )
        job = await purge_repo.create_job(
            session,
            id=uuid7(),
            tenant_id=ctx_a.tenant_id,
            subject_type="session",
            subject_id=session_a.id,
            reason="consent_revoked",
            requested_by=None,
        )
    async with tenant_tx(app_engine, svc) as session:
        assert await purge_repo.sample_ciphertext(session, session_a.id) == seg.text_enc
        await purge_repo.append_step(session, job.id, {"step": "running", "at": now.isoformat()})
        counts = await purge_repo.delete_session_data(session, session_a.id)
        purged = await purge_repo.destroy_session_dek(session, session_a.id, now=now)
        await purge_repo.append_step(session, job.id, {"step": "deleted", "counts": counts})
        assert purged is not None and purged.dek_wrapped is None and purged.state == "purged"
    assert counts == {
        "segment_search": 0,
        "transcript_segments": 1,
        "risk_events": 0,
        "note_statements": 1,
        "note_assessments": 0,
        "notes": 1,
        "stt_offsets": 1,
        "audio_chunks": 1,
    }
    async with tenant_tx(app_engine, svc) as session:
        assert all(v == 0 for v in (await purge_repo.count_session_data(session, session_a.id)).values())
        survivor = await notes_repo.get_note(session, signed.id)
        assert survivor is not None and survivor.signed_content_enc == b"\x09"
        job_row = await purge_repo.get_job(session, job.id)
        assert job_row is not None and [s["step"] for s in job_row.steps] == ["running", "deleted"]
        assert await purge_repo.sessions_of_patient(session, session_a.patient_id) == [session_a.id]
    with pytest.raises(DBAPIError) as exc:
        async with tenant_tx(app_engine, svc) as session:
            await session.execute(
                text("UPDATE sessions SET dek_wrapped = '\\x00' WHERE id = :s"), {"s": session_a.id}
            )
    assert "DEK destroyed" in str(exc.value.orig)


async def test_ledger_helpers(app_engine, session_a, ctx_a, fake_clock):
    now = fake_clock.now()
    rows = [
        {
            "session_id": session_a.id,
            "seq": s,
            "tenant_id": ctx_a.tenant_id,
            "byte_len": 4,
            "sha256": bytes(32),
            "storage_key": f"k{s}",
            "offset_ms": s * 200,
            "received_at": now,
        }
        for s in (1, 2, 4)
    ]
    async with tenant_tx(app_engine, ctx_a) as session:
        assert await sessions_repo.insert_chunks(session, rows) == 3
        assert await sessions_repo.insert_chunks(session, rows[:1]) == 0  # duplicate on resume
        assert await sessions_repo.ledgered_seqs(session, session_a.id, 1, 10) == [1, 2, 4]
        assert await sessions_repo.max_ledger_seq(session, session_a.id) == 4
        assert [c.seq for c in await sessions_repo.chunks_between(session, session_a.id, 2, 4)] == [2, 4]
        assert await sessions_repo.bump_epoch(session, session_a.id) == 1
        listed = await sessions_repo.list_sessions(session, tenant_id=ctx_a.tenant_id, state="recording")
        assert [s.id for s in listed] == [session_a.id]
