"""Purge end to end (§8.4, §7.2) on real PostgreSQL/Redis and a LocalFs object store.

A session seeded with ledger rows + ciphertext objects, encrypted segments, search rows, a risk
event, an unsigned draft, a *signed* note, stt offsets and the Redis hot state is purged through
the real path: ``POST /v1/purge-jobs`` (outbox ``purge.requested``) → ``Poller.run_once`` runs
``purge_run`` → ``purge.completed`` → second pass runs ``purge_verify``. Then the receipt, the
``verify-decrypt`` demonstration, what survives (signed note, tombstone, audit), idempotent re-runs,
the patient-level variant driven by a consent revoke, a verification failure landing in the DLQ,
and the CLI functions.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from chartwire.audit.service import assert_no_phi
from chartwire.core.clock import FakeClock
from chartwire.crypto.envelope import Envelope
from chartwire.crypto.errors import DecryptError, DekDestroyedError
from chartwire.db.models import (
    AudioChunk,
    AuditEvent,
    DeadLetter,
    Note,
    OutboxEvent,
    Patient,
    PurgeJob,
    RiskEvent,
    SegmentSearch,
    SttOffset,
    TranscriptSegment,
)
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import purge as purge_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.base import session_prefix
from chartwire.objectstore.localfs import LocalFs
from chartwire.outbox.poller import Poller
from chartwire.purge import cli as purge_cli
from chartwire.purge import pipeline, receipt, verify
from chartwire.redis import keys
from chartwire.worker.handlers import purge_run, purge_verify  # noqa: F401  (registers the handlers)
from chartwire.ws.watch import segment_aad
from tests.integration.api_support import (
    SCRIPT,
    build_app,
    client,
    grant,
    handler_ctx,
    headers,
    make_deps,
    note_rows,
    real_patient_dek,
    seed_session,
    statement_count,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def deps(app_engine, redis, settings, tmp_path):
    return make_deps(app_engine, redis, settings, LocalFs(tmp_path))


@pytest.fixture
async def clock(app_engine):
    """Anchored a few seconds after the database clock so freshly emitted outbox rows are due."""
    async with app_engine.connect() as conn:
        now = (await conn.execute(select(func.now()))).scalar_one()
    return FakeClock(now + timedelta(seconds=5))


@pytest.fixture
async def seeded(
    app_engine, owner_engine, redis, deps, settings, tenant_a, patient_a, clinician_a, session_a
):
    await real_patient_dek(
        app_engine, tenant_a, patient_a.id, deps.kek, name="가상환자 파기검사", phone="010-1111-2222"
    )
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


async def run_pass(deps, clock, tenant_id, *, advance_s: float = 30) -> None:
    clock.advance(advance_s)
    poller = Poller(handler_ctx(deps, clock), worker_id="test-e", tenants=[tenant_id], clock=clock)
    await poller.run_once()


async def count(engine, tenant_id, model, **where) -> int:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return int((await s.execute(stmt)).scalar_one())


async def load_job(engine, tenant_id, job_id) -> PurgeJob:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        job = await purge_repo.get_job(s, job_id)
    assert job is not None
    return job


# --------------------------------------------------------------------------- session-level, via the outbox


async def test_session_purge_end_to_end_with_receipt_and_verify(
    deps, clock, seeded, app_engine, redis, tenant_a, clinician_a, settings
):
    sid = seeded.session.id
    prefix = session_prefix(tenant_a.id, sid)
    assert len(await deps.objectstore.list(prefix)) == 5
    admin = headers(settings, tenant_id=tenant_a.id, role="admin")
    auditor = headers(settings, tenant_id=tenant_a.id, role="auditor")
    async with client(build_app(deps)) as api:
        requested = await api.post(
            "/v1/purge-jobs", json={"subject_type": "session", "subject_id": str(sid)}, headers=admin
        )
        assert requested.status_code == 202, requested.text
        job_id = requested.json()["id"]
        assert (
            requested.json()["state"] == "queued"
            and requested.json()["artifact"] == "파기 영수증 (purge receipt)"
        )
        assert await count(app_engine, tenant_a.id, OutboxEvent, event_type="purge.requested") == 1

        # the DEK still unwraps before the purge (so the failure afterwards is meaningful)
        alive = await api.post(f"/v1/purge-jobs/{job_id}/verify-decrypt", headers=auditor)
        assert alive.json()["unwrap"] == "succeeded" and alive.json()["job_state"] == "queued"

        ps = redis.pubsub()
        await ps.subscribe(keys.ctl(sid), keys.KEYS_INVALIDATE)
        for _ in range(2):
            assert await ps.get_message(timeout=2.0) is not None
        deps.keycache.get(
            sid, tenant_a.kek_ref, (await load_job_session(app_engine, tenant_a.id, sid)).dek_wrapped
        )
        assert sid in deps.keycache

        await run_pass(deps, clock, tenant_a.id)  # purge_run
        job = await load_job(app_engine, tenant_a.id, job_id)
        assert job.state == "completed" and job.completed_at is not None
        assert [s["step"] for s in job.steps] == ["capture", "redis", "objectstore", "rows", "crypto_shred"]
        counts = job.counts
        assert (
            counts["objects"] == 5
            and counts["audio_chunks"] == 5
            and counts["transcript_segments"] == len(SCRIPT)
        )
        assert (
            counts["segment_search"] == len(SCRIPT)
            and counts["risk_events"] == 1
            and counts["stt_offsets"] == 1
        )
        assert counts["notes"] == 1 and counts["note_statements"] == 1, "the unsigned draft only"
        assert (
            counts["session_dek_destroyed"] == 1 and counts["redis_keys"] == 4 and counts["stt_active"] == 1
        )
        assert len(job.dek_fingerprints) == 1 and len(job.dek_fingerprints[0]) == 64
        assert job.sample_ciphertext is not None and job.receipt_hash is not None
        assert bytes(job.receipt_hash) == receipt.receipt_hash(
            list(job.steps), dict(job.counts), list(job.dek_fingerprints)
        )

        published = [await ps.get_message(ignore_subscribe_messages=True, timeout=2.0) for _ in range(2)]
        by_channel = {m["channel"]: m["data"] for m in published if m}
        assert json.loads(by_channel[keys.ctl(sid)]) == {"t": "purge"}
        assert by_channel[keys.KEYS_INVALIDATE] == str(sid)
        assert sid not in deps.keycache, "the api's DEK cache dropped the key immediately"

        # nothing PHI-bearing is left; the tombstone and the signed record remain
        for model in (AudioChunk, TranscriptSegment, SegmentSearch, RiskEvent, SttOffset):
            assert await count(app_engine, tenant_a.id, model, session_id=sid) == 0, model.__name__
        assert await deps.objectstore.list(prefix) == []
        assert [k async for k in redis.scan_iter(match=keys.sess_pattern(sid))] == []
        assert (
            not await redis.sismember(keys.STT_ACTIVE, str(sid))
            and await redis.get(keys.stt_owner(sid)) is None
        )
        assert await redis.hget(keys.STT_LAG, str(sid)) is None
        notes = await note_rows(app_engine, tenant_a.id, sid)
        assert [n.id for n in notes] == [seeded.signed_note_id] and notes[0].legal_hold == "medical_record"
        assert await statement_count(app_engine, tenant_a.id, seeded.draft_note_id) == 0
        assert (
            Envelope.decrypt(
                seeded.record_key, bytes(notes[0].signed_content_enc), f"{tenant_a.id}:note:{sid}:signed"
            )
            == b'{"S":["signed statement"]}'
        )
        tomb = await load_job_session(app_engine, tenant_a.id, sid)
        assert (
            tomb.state == "purged"
            and tomb.dek_wrapped is None
            and tomb.dek_destroyed_at is not None
            and tomb.purged_at
        )

        await run_pass(deps, clock, tenant_a.id)  # purge_verify
        job = await load_job(app_engine, tenant_a.id, job_id)
        assert job.state == "verified", job.verify_result
        assert job.verify_result["ok"] is True and job.verify_result["failed"] == []
        assert set(job.verify_result["checks"]) == {
            f"{c}:{sid}" for c in ("rows", "objectstore", "redis", "dek_null", "unwrap", "decrypt_sample")
        }

        shown = await api.get(f"/v1/purge-jobs/{job_id}", headers=auditor)
        assert shown.status_code == 200
        out = shown.json()
        assert (
            out["state"] == "verified"
            and out["receipt_hash_valid"] is True
            and out["receipt_hash"] == bytes(job.receipt_hash).hex()
        )
        assert out["verify_result"]["ok"] and len(out["steps"]) == 5

        demo = await api.post(f"/v1/purge-jobs/{job_id}/verify-decrypt", headers=admin)
        assert demo.status_code == 200
        assert demo.json() == {
            "decrypt_attempted": True,
            "unwrap": "failed:dek_destroyed",
            "decrypt_sample": "failed:invalid_tag",
            "job_state": "verified",
        }

        # the tombstone cannot be resurrected, even by the service role
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
            from sqlalchemy import update
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                await s.execute(
                    update(SessionModel).where(SessionModel.id == sid).values(dek_wrapped=b"\x00" * 60)
                )
        with pytest.raises(DekDestroyedError):
            deps.keycache.get(sid, tenant_a.kek_ref, None)
        with pytest.raises(DecryptError):
            Envelope.decrypt(
                deps.kek.derive(tenant_a.kek_ref),
                bytes(job.sample_ciphertext),
                segment_aad(tenant_a.id, sid, 0),
            )

        actions = await audit_actions(app_engine, tenant_a.id)
        assert actions.count("purge.requested") == 1 and actions.count("purge.step") == 5
        assert "purge.verified" in actions and "purge.verify_decrypt" in actions
        assert "session.purged" in actions and "purge.completed" in actions
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
            details = (
                await s.scalars(select(AuditEvent.detail).where(AuditEvent.action == "purge.completed"))
            ).all()
        assert details and details[0]["receipt_hash"] == out["receipt_hash"]
        for detail in details:
            assert_no_phi(detail)  # ids, counts and hashes only (raises on text/quote/name/phone keys)
        assert await count(app_engine, tenant_a.id, OutboxEvent, status="done") == 2
        await ps.aclose()


async def load_job_session(engine, tenant_id, sid) -> SessionModel:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        row = await s.get(SessionModel, sid)
    assert row is not None
    return row


async def audit_actions(engine, tenant_id) -> list[str]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        return list((await s.scalars(select(AuditEvent.action).order_by(AuditEvent.id))).all())


async def test_rerun_of_a_completed_job_and_redelivery_are_no_ops(deps, clock, seeded, app_engine, tenant_a):
    ctx = handler_ctx(deps, clock)
    async with ctx.tenant_tx(tenant_a.id) as s:
        job = await pipeline.create_job(
            s,
            tenant_id=tenant_a.id,
            subject_type="session",
            subject_id=seeded.session.id,
            reason="admin",
            requested_by=None,
        )
    first = await pipeline.run(ctx, job.id, tenant_id=tenant_a.id)
    assert first.state == "completed"
    steps_before = list(first.steps)
    second = await pipeline.run(ctx, job.id, tenant_id=tenant_a.id)
    assert (
        second.state == "completed"
        and list(second.steps) == steps_before
        and second.receipt_hash == first.receipt_hash
    )
    verified = await verify.run(ctx, job.id, tenant_id=tenant_a.id)
    assert verified.state == "verified"
    assert (await verify.run(ctx, job.id, tenant_id=tenant_a.id)).state == "verified"
    # a second job for the same (already purged) session records "skipped" instead of failing
    async with ctx.tenant_tx(tenant_a.id) as s:
        again = await pipeline.create_job(
            s,
            tenant_id=tenant_a.id,
            subject_type="session",
            subject_id=seeded.session.id,
            reason="admin",
            requested_by=None,
        )
    done = await pipeline.run(ctx, again.id, tenant_id=tenant_a.id)
    assert [st["step"] for st in done.steps] == ["skipped"] and done.steps[0]["counts"] == {
        "already_purged": 1
    }
    assert await count(app_engine, tenant_a.id, OutboxEvent, event_type="purge.completed") == 2


# --------------------------------------------------------------------------- patient-level, via consent revoke


async def test_consent_revoke_drives_patient_level_purge(
    deps, clock, seeded, app_engine, redis, factories, tenant_a, patient_a, clinician_a, settings
):
    other_session = await factories.session(tenant_a.id, patient_a.id, clinician_a.id)
    clin = headers(settings, tenant_id=tenant_a.id, role="clinician", user_id=clinician_a.id)
    async with client(build_app(deps)) as api:
        consents = (await api.get(f"/v1/patients/{patient_a.id}/consents", headers=clin)).json()
        assert (await api.get(f"/v1/patients/{patient_a.id}", headers=clin)).json()[
            "name"
        ] == "가상환자 파기검사"
        revoked = await api.post(f"/v1/consents/{consents[0]['id']}/revoke", headers=clin)
        assert revoked.status_code == 202 and revoked.json()["live_sessions_notified"] == 1
        job_id = revoked.json()["purge_job_id"]

        await run_pass(deps, clock, tenant_a.id)  # consent.revoked → purge_run (patient level)
        job = await load_job(app_engine, tenant_a.id, job_id)
        assert job.state == "completed" and job.subject_type == "patient"
        subjects = [s["subject_id"] for s in job.steps]
        assert str(seeded.session.id) in subjects and str(other_session.id) in subjects
        assert job.steps[-1]["step"] == "patient_shred" and job.steps[-1]["counts"] == {
            "patient_identifiers": 1,
            "patient_dek_destroyed": 1,
        }
        assert len(job.dek_fingerprints) == 3, "two session DEKs + the patient DEK"

        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
            patient = await s.get(Patient, patient_a.id)
        assert patient is not None and patient.name_enc is None and patient.phone_enc is None
        assert patient.dek_wrapped is None and patient.consent_state == "purged" and patient.purged_at
        shown = await api.get(f"/v1/patients/{patient_a.id}", headers=clin)
        assert shown.json()["name"] is None and shown.json()["consent_state"] == "purged"
        blocked = await api.post(
            f"/v1/patients/{patient_a.id}/consents", json={"scopes": ["recording"]}, headers=clin
        )
        assert blocked.status_code == 409 and blocked.json()["code"] == "CW-4096"
        for sid in (seeded.session.id, other_session.id):
            assert (await load_job_session(app_engine, tenant_a.id, sid)).state == "purged"
            assert (await api.get(f"/v1/sessions/{sid}/segments", headers=clin)).status_code == 410

        await run_pass(deps, clock, tenant_a.id)  # purge.completed → purge_verify
        job = await load_job(app_engine, tenant_a.id, job_id)
        assert job.state == "verified" and job.verify_result["checks"]["patient_shredded"] is True
        assert await count(app_engine, tenant_a.id, Note, legal_hold="medical_record") == 1
        assert await count(app_engine, tenant_a.id, DeadLetter) == 0


# --------------------------------------------------------------------------- failure path


async def test_verification_failure_is_durable_and_reaches_the_dlq(deps, clock, seeded, app_engine, tenant_a):
    ctx = handler_ctx(deps, clock)
    async with ctx.tenant_tx(tenant_a.id) as s:
        job = await pipeline.create_job(
            s,
            tenant_id=tenant_a.id,
            subject_type="session",
            subject_id=seeded.session.id,
            reason="admin",
            requested_by=None,
        )
    await pipeline.run(ctx, job.id, tenant_id=tenant_a.id)
    # simulate residue the pipeline did not remove: an object reappears under the session prefix
    await deps.objectstore.put(f"{session_prefix(tenant_a.id, seeded.session.id)}00000099.bin", b"residue")
    with pytest.raises(verify.PurgeVerifyFailed) as exc:
        await verify.run(ctx, job.id, tenant_id=tenant_a.id)
    assert exc.value.failed == [f"objectstore:{seeded.session.id}"]
    failed = await load_job(app_engine, tenant_a.id, job.id)
    assert failed.state == "failed" and failed.verify_result["ok"] is False
    assert failed.verify_result["detail"][f"objectstore:{seeded.session.id}"] == {"objects": 1}

    # under the poller the raise goes through retries into the DLQ (max_attempts=8, backoff ≤ 300 s)
    for _ in range(8):
        await run_pass(deps, clock, tenant_a.id, advance_s=400)
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        dead = (await s.scalars(select(DeadLetter))).all()
        event = (
            await s.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "purge.completed"))
        ).one()
    assert len(dead) == 1 and dead[0].event_type == "purge.completed" and event.status == "dead"
    assert "PurgeVerifyFailed" in (dead[0].last_error or "") and "objectstore" in (dead[0].last_error or "")
    assert (await load_job(app_engine, tenant_a.id, job.id)).state == "failed"

    # operator removes the residue and replays: verification now passes
    await deps.objectstore.delete_prefix(session_prefix(tenant_a.id, seeded.session.id))
    admin = headers(settings=deps.settings, tenant_id=tenant_a.id, role="admin")
    async with client(build_app(deps)) as api:
        replayed = await api.post(f"/v1/ops/dead-letters/{event.id}/replay", headers=admin)
        assert replayed.status_code == 200 and replayed.json()["replayed"] is True
        listed = await api.get("/v1/ops/dead-letters", headers=admin)
        assert listed.json()[0]["replayed_at"] is not None
    await run_pass(deps, clock, tenant_a.id, advance_s=400)
    assert (await load_job(app_engine, tenant_a.id, job.id)).state == "verified"
    async with client(build_app(deps)) as api:
        stats = await api.get("/v1/ops/outbox", headers=admin)
        assert stats.json()["dead"] == 0 and stats.json()["done"] >= 1
        parts = await api.get("/v1/ops/partitions", headers=admin)
        assert any(p["is_default"] for p in parts.json()) and all(
            p["name"].startswith("transcript_segments") for p in parts.json()
        )


# --------------------------------------------------------------------------- CLI


async def test_cli_run_receipt_verify_and_audit_list(deps, seeded, migrated_db, tenant_a, settings, capsys):
    cli_settings = settings.model_copy(
        update={
            "database_url": migrated_db.app,
            "redis_url": migrated_db.redis,
            "objectstore": f"localfs:{deps.objectstore.root}",
        }
    )
    out = await purge_cli.run_purge(
        cli_settings,
        tenant_id=tenant_a.id,
        subject_type="session",
        subject_id=seeded.session.id,
        do_verify=True,
    )
    assert out["state"] == "verified" and out["receipt_hash_valid"] is True and out["counts"]["objects"] == 5
    again = await purge_cli.load_receipt(cli_settings, job_id=out["id"], tenant=None)
    assert again["receipt_hash"] == out["receipt_hash"] and again["state"] == "verified"
    verified = await purge_cli.run_verify(cli_settings, job_id=out["id"], tenant=tenant_a.id)
    assert verified["state"] == "verified"
    from chartwire.audit import cli as audit_cli

    rows = await audit_cli.list_events(
        cli_settings, tenant_id=tenant_a.id, action="purge.completed", limit=10, before=None
    )
    assert len(rows) == 1 and rows[0]["resource_id"] == str(out["id"]) and rows[0]["actor_role"] == "service"
    import typer

    with pytest.raises(typer.BadParameter):
        await purge_cli.load_receipt(cli_settings, job_id=seeded.session.id, tenant=None)
