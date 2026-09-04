"""``chartwire seed --demo`` — the idempotent demo tenant (spec §13.1, §10.3 ``demo`` set).

Creates tenant ``demo`` (KEK reference + wrapped record key), one user per role with the shared
password ``demo1234!``, 20 synthetic patients with an active consent covering all four scopes,
the ``s01``–``s20`` demo scripts on disk, and one ``created`` session per script (with a wrapped
session DEK) so the console's session picker has something to start.

Everything is keyed on stable identifiers — tenant slug, user e-mail blind index, patient
pseudonym, session ``script_ref`` — so running the command twice adds nothing. Encryption
matches what the REST routers write (same AADs, same blind index), so a seeded user can log in
and a seeded patient's name decrypts in the console. All data is synthetic (spec §0 rule 1).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from chartwire.auth import passwords
from chartwire.consent.scopes import SCOPE_ORDER
from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, aad, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.db.engine import make_engine
from chartwire.db.models import Consent, Patient, Tenant, User
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.synth import vocab_ko as v
from chartwire.synth.cli import write_scripts
from chartwire.synth.scripts import Script, generate_set

DEMO_SLUG: Final = "demo"
DEMO_NAME: Final = "가상의원 데모"
DEMO_KEK_REF: Final = "local:demo"
DEMO_PASSWORD: Final = "demo1234!"
DEMO_USERS: Final[tuple[tuple[str, str, str], ...]] = (
    ("clinician", "clinician@demo.clinic", "가상임상의"),
    ("staff", "staff@demo.clinic", "가상직원"),
    ("admin", "admin@demo.clinic", "가상관리자"),
    ("auditor", "auditor@demo.clinic", "가상감사자"),
    ("recorder", "recorder@demo.clinic", "가상녹음기"),
)
"""(role, e-mail, display name). The e-mails are console defaults (WP-H); nothing here is real."""
N_PATIENTS: Final = 20
N_SCRIPTS: Final = 20
ALL_SCOPES: Final[list[str]] = list(SCOPE_ORDER)
CONSENT_CHANNEL: Final = "seed"


def default_scripts_dir(settings: Settings | None = None) -> Path:
    """``Settings.stt_scripts_dir`` → ``Settings.scripts_dir`` (default ``var/scripts``) — the same
    directory the stt-worker simulator reads, so ``seed`` and ``serve stt-worker`` always agree."""
    return (settings or Settings()).stt_scripts_path


@dataclass
class SeedResult:
    tenant_id: UUID
    scripts_dir: Path
    skipped: bool = False
    """``--if-empty`` and the tenant already existed: nothing was touched."""
    users_created: int = 0
    patients_created: int = 0
    consents_created: int = 0
    sessions_created: int = 0
    scripts_written: int = 0
    users: dict[str, UUID] = field(default_factory=dict)
    patients: list[UUID] = field(default_factory=list)
    sessions: list[tuple[str, UUID]] = field(default_factory=list)
    """``(script_ref, session_id)`` for every demo session (existing or new)."""

    @property
    def summary(self) -> str:
        if self.skipped:
            return "demo 테넌트가 이미 있어 건너뜁니다 (--if-empty)"
        return (
            f"tenant {DEMO_SLUG}: 사용자 +{self.users_created}, 환자 +{self.patients_created}, "
            f"동의 +{self.consents_created}, 세션 +{self.sessions_created}, "
            f"스크립트 {self.scripts_written}개 → {self.scripts_dir}"
        )


def email_aad(tenant_id: UUID, email_hmac: bytes) -> str:
    """Identical to ``api/routers/users.py`` so the users router can decrypt seeded e-mails."""
    return aad(tenant_id, "user", email_hmac.hex(), "email")


def patient_aad(tenant_id: UUID, pseudonym: str, field_name: str) -> str:
    """Identical to ``api/routers/patients.py`` (``tenant:patient:<pseudonym>:name|phone``)."""
    return aad(tenant_id, "patient", pseudonym, field_name)


async def seed_demo(
    settings: Settings,
    *,
    seed: int = 1,
    if_empty: bool = False,
    scripts_dir: Path | None = None,
    owner_engine: AsyncEngine | None = None,
    app_engine: AsyncEngine | None = None,
) -> SeedResult:
    """Create (or complete) the demo tenant. Engines may be injected by tests; otherwise they are
    built from ``settings`` and disposed here."""
    own_engines = owner_engine is None or app_engine is None
    owner = owner_engine or make_engine(settings.database_owner_url, pool_size=2, max_overflow=0)
    app = app_engine or make_engine(settings.database_url, pool_size=2, max_overflow=0)
    try:
        return await _seed(settings, owner, app, seed=seed, if_empty=if_empty, scripts_dir=scripts_dir)
    finally:
        if own_engines:
            await owner.dispose()
            await app.dispose()


async def _seed(
    settings: Settings,
    owner: AsyncEngine,
    app: AsyncEngine,
    *,
    seed: int,
    if_empty: bool,
    scripts_dir: Path | None,
) -> SeedResult:
    kek = LocalKek(settings.kek_master_bytes)
    master = settings.kek_master_bytes
    target_dir = scripts_dir or default_scripts_dir(settings)
    tenant, created = await _ensure_tenant(owner, kek)
    result = SeedResult(tenant_id=tenant.id, scripts_dir=target_dir)
    if if_empty and not created:
        result.skipped = True
        return result
    record_key = kek.unwrap(bytes(tenant.record_key_wrapped), tenant.kek_ref)
    scripts = generate_set("demo", N_SCRIPTS, seed)
    rng = random.Random(f"seed-demo:{seed}")  # patient identities are a pure function of the seed

    async with tenant_tx(app, TenantCtx.service(tenant.id)) as s:
        await _ensure_users(s, tenant, master, record_key, result)
        patients = await _ensure_patients(s, tenant, master, kek, rng, result)
        await _ensure_consents(s, tenant, patients, result)
        await _ensure_sessions(s, tenant, kek, patients, scripts, result)
    result.scripts_written = len(write_scripts(scripts, target_dir))
    return result


# ------------------------------------------------------------------ tenant (owner: tenants has no RLS)


async def _ensure_tenant(owner: AsyncEngine, kek: LocalKek) -> tuple[Tenant, bool]:
    factory = async_sessionmaker(owner, expire_on_commit=False)
    async with factory() as s, s.begin():
        existing = await tenancy_repo.get_tenant_by_slug(s, DEMO_SLUG)
        if existing is not None:
            return existing, False
        tenant = await tenancy_repo.create_tenant(
            s,
            slug=DEMO_SLUG,
            name=DEMO_NAME,
            kek_ref=DEMO_KEK_REF,
            record_key_wrapped=kek.wrap(Envelope.new_dek(), DEMO_KEK_REF),
            settings={"synthetic": True, "seeded_by": "chartwire seed --demo"},
        )
        return tenant, True


# ------------------------------------------------------------------ users


async def _ensure_users(
    s: AsyncSession, tenant: Tenant, master: bytes, record_key: bytes, result: SeedResult
) -> None:
    for role, email, display_name in DEMO_USERS:
        email_hmac = blind_index(master, tenant.id, email)
        user = await tenancy_repo.find_user_by_email_hmac(s, tenant.id, email_hmac)
        if user is None:
            user = await tenancy_repo.create_user(
                s,
                tenant_id=tenant.id,
                role=role,
                email_hmac=email_hmac,
                email_enc=Envelope.encrypt(record_key, email.encode(), email_aad(tenant.id, email_hmac)),
                display_name=display_name,
                password_hash=passwords.hash(DEMO_PASSWORD),
            )
            result.users_created += 1
        result.users[role] = user.id


# ------------------------------------------------------------------ patients + consents


async def _ensure_patients(
    s: AsyncSession, tenant: Tenant, master: bytes, kek: LocalKek, rng: random.Random, result: SeedResult
) -> list[Patient]:
    rows = (
        await s.scalars(select(Patient).where(Patient.tenant_id == tenant.id).order_by(Patient.pseudonym))
    ).all()
    by_pseudonym: dict[str, Patient] = {p.pseudonym: p for p in rows}
    patients: list[Patient] = []
    for n in range(1, N_PATIENTS + 1):
        pseudonym = v.pseudonym(n)
        # draw the synthetic identity even when the row exists so later patients stay deterministic
        name, phone = v.synth_name(rng), v.synth_phone(rng)
        birth_year, sex = rng.randint(1958, 2006), rng.choice(("F", "M"))
        patient = by_pseudonym.get(pseudonym)
        if patient is None:
            dek = Envelope.new_dek()
            wrapped = kek.wrap(dek, tenant.kek_ref)
            patient = await patients_repo.create_patient(
                s,
                tenant_id=tenant.id,
                pseudonym=pseudonym,
                name_enc=Envelope.encrypt(dek, name.encode(), patient_aad(tenant.id, pseudonym, "name")),
                name_hmac=blind_index(master, tenant.id, name),
                birth_year=birth_year,
                sex=sex,
                phone_enc=Envelope.encrypt(dek, phone.encode(), patient_aad(tenant.id, pseudonym, "phone")),
                dek_wrapped=wrapped,
                dek_fingerprint=dek_fingerprint(wrapped),
            )
            result.patients_created += 1
        patients.append(patient)
        result.patients.append(patient.id)
    return patients


async def _ensure_consents(
    s: AsyncSession, tenant: Tenant, patients: list[Patient], result: SeedResult
) -> None:
    granted_by = result.users.get("clinician")
    for patient in patients:
        active = await patients_repo.latest_active_consent(s, patient.id)
        if active is not None and set(active.scopes) >= set(ALL_SCOPES):
            continue
        await patients_repo.grant_consent(
            s,
            tenant_id=tenant.id,
            patient_id=patient.id,
            scopes=ALL_SCOPES,
            granted_by=granted_by,
            channel=CONSENT_CHANNEL,
            policy_hash=_policy_hash(ALL_SCOPES, CONSENT_CHANNEL),
        )
        result.consents_created += 1


def _policy_hash(scopes: list[str], channel: str) -> bytes | None:
    try:
        from chartwire.consent.service import policy_hash
    except ImportError:  # consent service not built yet: the column is nullable
        return None
    return policy_hash(scopes, channel)


# ------------------------------------------------------------------ sessions


async def _ensure_sessions(
    s: AsyncSession,
    tenant: Tenant,
    kek: LocalKek,
    patients: list[Patient],
    scripts: list[Script],
    result: SeedResult,
) -> None:
    clinician_id = result.users["clinician"]
    existing = {
        row.script_ref: row.id
        for row in (
            await s.scalars(
                select(SessionModel).where(
                    SessionModel.tenant_id == tenant.id, SessionModel.script_ref.is_not(None)
                )
            )
        ).all()
    }
    for i, script in enumerate(scripts):
        ref = script.script_ref
        if ref in existing:
            result.sessions.append((ref, existing[ref]))
            continue
        wrapped = kek.wrap(Envelope.new_dek(), tenant.kek_ref)
        row = await sessions_repo.create_session(
            s,
            id=uuid7(),
            tenant_id=tenant.id,
            patient_id=patients[i % len(patients)].id,
            clinician_id=clinician_id,
            script_ref=ref,
            scopes_snapshot=ALL_SCOPES,
            dek_wrapped=wrapped,
            dek_fingerprint=dek_fingerprint(wrapped),
            chunk_ms=script.chunk_ms,
        )
        result.sessions_created += 1
        result.sessions.append((ref, row.id))


# ------------------------------------------------------------------ inspection helper (tests, CLI --status)


async def demo_counts(app: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    async with tenant_tx(app, TenantCtx.service(tenant_id)) as s:
        counts = {}
        for name, model in (
            ("users", User),
            ("patients", Patient),
            ("consents", Consent),
            ("sessions", SessionModel),
        ):
            stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            counts[name] = int((await s.execute(stmt)).scalar_one())
        return counts
