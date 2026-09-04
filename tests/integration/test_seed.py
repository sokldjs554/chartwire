"""``chartwire seed --demo``: complete demo tenant, encryption compatible with the REST routers,
idempotent on re-run, ``--if-empty`` no-op (spec §13.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from chartwire.auth import passwords
from chartwire.consent.gates import active_scopes
from chartwire.core.config import Settings
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.db.models import Consent, Patient, Tenant, User
from chartwire.db.models import Session as SessionModel
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.synth import seed as seed_mod
from chartwire.synth import seed_cli
from chartwire.synth.scripts import Script
from tests.conftest import DbUrls

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_settings(settings: Settings, migrated_db: DbUrls) -> Settings:
    return settings.model_copy(
        update={"database_url": migrated_db.app, "database_owner_url": migrated_db.owner}
    )


async def test_seed_demo_is_complete_and_idempotent(
    clean_db: AsyncEngine, app_engine: AsyncEngine, seed_settings: Settings, tmp_path: Path
) -> None:
    scripts_dir = tmp_path / "scripts"
    first = await seed_mod.seed_demo(
        seed_settings, scripts_dir=scripts_dir, owner_engine=clean_db, app_engine=app_engine
    )
    assert not first.skipped
    assert (first.users_created, first.patients_created, first.consents_created, first.sessions_created) == (
        5,
        20,
        20,
        20,
    )
    assert (
        first.scripts_written == 20
        and (scripts_dir / "s01.json").is_file()
        and (scripts_dir / "index.json").is_file()
    )
    Script.model_validate_json((scripts_dir / "s20.json").read_text(encoding="utf-8"))

    kek = LocalKek(seed_settings.kek_master_bytes)
    async with tenant_tx(app_engine, TenantCtx.service(first.tenant_id)) as s:
        tenant = (await s.scalars(select(Tenant).where(Tenant.slug == "demo"))).one()
        record_key = kek.unwrap(bytes(tenant.record_key_wrapped), tenant.kek_ref)
        users = (await s.scalars(select(User).where(User.tenant_id == tenant.id))).all()
        assert sorted(u.role for u in users) == ["admin", "auditor", "clinician", "recorder", "staff"]
        for role, email, _ in seed_mod.DEMO_USERS:
            user = next(u for u in users if u.role == role)
            hmac = blind_index(seed_settings.kek_master_bytes, tenant.id, email)
            assert bytes(user.email_hmac) == hmac  # the auth router's login lookup
            assert passwords.verify(seed_mod.DEMO_PASSWORD, user.password_hash)
            decrypted = Envelope.decrypt(
                record_key, bytes(user.email_enc), seed_mod.email_aad(tenant.id, hmac)
            )
            assert decrypted.decode() == email

        patients = (await s.scalars(select(Patient).where(Patient.tenant_id == tenant.id))).all()
        assert len(patients) == 20 and all(
            p.is_synthetic and p.pseudonym.startswith("가상환자-") for p in patients
        )
        assert all(p.consent_state == "granted" for p in patients)
        p1 = next(p for p in patients if p.pseudonym == "가상환자-0001")
        dek = kek.unwrap(bytes(p1.dek_wrapped), tenant.kek_ref)
        assert bytes(p1.dek_fingerprint) == dek_fingerprint(bytes(p1.dek_wrapped))
        name = Envelope.decrypt(
            dek, bytes(p1.name_enc), seed_mod.patient_aad(tenant.id, p1.pseudonym, "name")
        )
        assert bytes(p1.name_hmac) == blind_index(seed_settings.kek_master_bytes, tenant.id, name.decode())
        consents = (await s.scalars(select(Consent).where(Consent.patient_id == p1.id))).all()
        assert active_scopes(consents) == {"recording", "transcription", "ai_drafting", "search_index"}

        sessions = (await s.scalars(select(SessionModel).where(SessionModel.tenant_id == tenant.id))).all()
        assert sorted(sess.script_ref for sess in sessions) == [f"s{i:02d}" for i in range(1, 21)]
        assert {sess.state for sess in sessions} == {"created"}
        for sess in sessions:
            assert sess.clinician_id == first.users["clinician"]
            assert set(sess.scopes_snapshot) == set(seed_mod.ALL_SCOPES)
            assert len(kek.unwrap(bytes(sess.dek_wrapped), tenant.kek_ref)) == 32

    # second run: nothing added, scripts rewritten byte-identical
    before = (scripts_dir / "s07.json").read_bytes()
    second = await seed_mod.seed_demo(
        seed_settings, scripts_dir=scripts_dir, owner_engine=clean_db, app_engine=app_engine
    )
    assert not second.skipped
    assert (
        second.users_created,
        second.patients_created,
        second.consents_created,
        second.sessions_created,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert second.tenant_id == first.tenant_id and second.sessions == first.sessions
    assert (scripts_dir / "s07.json").read_bytes() == before
    assert await seed_mod.demo_counts(app_engine, first.tenant_id) == {
        "users": 5,
        "patients": 20,
        "consents": 20,
        "sessions": 20,
    }

    # --if-empty: tenant exists → untouched, even with a different seed
    third = await seed_mod.seed_demo(
        seed_settings,
        seed=9,
        if_empty=True,
        scripts_dir=scripts_dir,
        owner_engine=clean_db,
        app_engine=app_engine,
    )
    assert third.skipped and third.scripts_written == 0
    assert (scripts_dir / "s07.json").read_bytes() == before


async def test_seed_completes_a_partially_seeded_tenant(
    clean_db: AsyncEngine, app_engine: AsyncEngine, seed_settings: Settings, tmp_path: Path
) -> None:
    """A tenant that exists but lacks users/patients (e.g. created by hand) is completed, not duplicated."""
    kek = LocalKek(seed_settings.kek_master_bytes)
    tenant, created = await seed_mod._ensure_tenant(clean_db, kek)
    assert created
    result = await seed_mod.seed_demo(
        seed_settings, scripts_dir=tmp_path, owner_engine=clean_db, app_engine=app_engine
    )
    assert result.tenant_id == tenant.id and not result.skipped
    assert result.users_created == 5 and result.patients_created == 20 and result.sessions_created == 20


def test_cli_requires_demo_flag_and_runs_end_to_end(
    migrated_db: DbUrls, clean_db: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTWIRE_DATABASE_URL", migrated_db.app)
    monkeypatch.setenv("CHARTWIRE_DATABASE_OWNER_URL", migrated_db.owner)
    runner = CliRunner()
    assert runner.invoke(seed_cli.app, []).exit_code != 0
    out = runner.invoke(seed_cli.app, ["--demo", "--scripts-dir", str(tmp_path)])
    assert out.exit_code == 0, out.output
    assert "세션 +20" in out.output and "demo1234!" in out.output
    again = runner.invoke(seed_cli.app, ["--demo", "--if-empty", "--scripts-dir", str(tmp_path)])
    assert again.exit_code == 0 and "건너뜁니다" in again.output
