"""Shared seeding for the notes integration suites (WP-D).

Fixtures from ``tests/conftest.py`` store placeholder key material (``bytes(48)``/``bytes(60)``);
these helpers replace it with a *real* wrapped record key and session DEK so the service can
decrypt, encrypt segments the way the stt-worker does (same AAD), and build a ``HandlerContext``
identical in shape to the worker's. All text is synthetic Korean — no real PHI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.api.routers import notes as notes_router
from chartwire.auth import jwt
from chartwire.auth.deps import jwt_secret
from chartwire.core.clock import Clock, SystemClock
from chartwire.core.config import Settings
from chartwire.crypto.envelope import Envelope, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.crypto.keycache import KeyCache
from chartwire.db.models import Tenant
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.notes.repo_adapter import encrypt_segment_text
from chartwire.outbox.context import HandlerContext

ALL_SCOPES = ["recording", "transcription", "ai_drafting", "search_index"]

# Utterances with extractive cues (S: patient symptom, O: clinician observation, P: clinician plan).
# The clinician's opening question is deliberately *not* chartable (question → excluded).
SCRIPT: list[tuple[str, str]] = [
    ("clinician", "안녕하세요, 지난 2주 동안 어떻게 지내셨어요?"),
    ("patient", "잠드는 데 두 시간쯤 걸려요"),
    ("patient", "입맛이 없어요"),
    ("patient", "에스시탈로프람 10mg 약을 먹고 있어요"),
    ("clinician", "오늘 표정이 좀 어두워 보이시네요"),
    ("clinician", "에스시탈로프람을 15mg으로 올려보겠습니다"),
    ("clinician", "2주 뒤에 뵙겠습니다"),
]
EXPECTED_SECTIONS = {"S": 3, "O": 1, "P": 2}


@dataclass
class Seeded:
    tenant: Tenant
    clinician_id: UUID
    patient_id: UUID
    session_id: UUID
    dek: bytes
    record_key: bytes
    kek: LocalKek
    started_at: datetime
    texts: dict[int, str]


def make_keycache(settings: Settings) -> KeyCache:
    return KeyCache(LocalKek(settings.kek_master_bytes))


def make_ctx(
    engine: AsyncEngine, settings: Settings, redis: Redis | None = None, clock: Clock | None = None
) -> HandlerContext:
    kek = LocalKek(settings.kek_master_bytes)
    return HandlerContext(
        engine=engine,
        redis=redis,
        objectstore=None,
        clock=clock or SystemClock(),
        settings=settings,
        kek=kek,
        keycache=KeyCache(kek),
    )


async def seed(
    factories: Any,
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    settings: Settings,
    *,
    scopes: list[str] | None = None,
    script: list[tuple[str, str]] | list[tuple[int, str, str]] | None = None,
    slug: str = "clinic-a",
    end_state: str = "transcribed",
) -> Seeded:
    """Tenant with a real record key, clinician, patient (+consent), an ended session with a real
    DEK and its final segments encrypted exactly as the stt-worker writes them.

    ``script`` entries are ``(speaker, text)`` (seq = position) or ``(seq, speaker, text)``."""
    kek = LocalKek(settings.kek_master_bytes)
    tenant = await factories.tenant(slug)
    record_key = Envelope.new_dek()
    async with owner_engine.begin() as conn:
        await conn.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id)
            .values(record_key_wrapped=kek.wrap(record_key, tenant.kek_ref))
        )
    tenant.record_key_wrapped = kek.wrap(record_key, tenant.kek_ref)
    clinician = await factories.user(tenant.id, "clinician")
    patient = await factories.patient(tenant.id)
    started_at = datetime.now(tz=UTC) - timedelta(minutes=10)
    sess = await factories.session(tenant.id, patient.id, clinician.id, started_at=started_at)
    dek = Envelope.new_dek()
    wrapped = kek.wrap(dek, tenant.kek_ref)
    texts: dict[int, str] = {}
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        await sessions_repo.update_session(
            s, sess.id, dek_wrapped=wrapped, dek_fingerprint=dek_fingerprint(wrapped)
        )
        if scopes is None or scopes:
            await patients_repo.grant_consent(
                s,
                tenant_id=tenant.id,
                patient_id=patient.id,
                scopes=ALL_SCOPES if scopes is None else scopes,
                granted_by=clinician.id,
            )
        for i, entry in enumerate(script if script is not None else SCRIPT):
            seq, speaker, text = entry if len(entry) == 3 else (i, *entry)
            texts[seq] = text
            await segments_repo.insert_final(
                s,
                tenant_id=tenant.id,
                session_id=sess.id,
                patient_id=patient.id,
                seq=seq,
                speaker=speaker,
                t_start_ms=seq * 3000,
                t_end_ms=seq * 3000 + 2500,
                text_enc=encrypt_segment_text(
                    tenant_id=tenant.id, session_id=sess.id, seq=seq, dek=dek, text=text
                ),
                text_len=len(text),
                confidence=0.9,
                provider="simulator",
                created_at=started_at + timedelta(seconds=3 * seq),
            )
        await sessions_repo.set_state(s, sess.id, "ended", now=started_at + timedelta(minutes=5))
        if end_state != "ended":
            await sessions_repo.set_state(s, sess.id, end_state, now=started_at + timedelta(minutes=6))
    return Seeded(tenant, clinician.id, patient.id, sess.id, dek, record_key, kek, started_at, texts)


async def purge_like(app_engine: AsyncEngine, seeded: Seeded, keycache: KeyCache) -> None:
    """What ``purge_run`` does to the session's PHI (§8.4 steps 4–6), without the purge_jobs row:
    segments deleted, DEK crypto-shredded, tombstone state, key cache invalidated."""
    from sqlalchemy import delete

    from chartwire.db.models import SegmentSearch, TranscriptSegment

    now = datetime.now(tz=UTC)
    async with tenant_tx(app_engine, TenantCtx.service(seeded.tenant.id)) as s:
        await s.execute(delete(SegmentSearch).where(SegmentSearch.session_id == seeded.session_id))
        await s.execute(delete(TranscriptSegment).where(TranscriptSegment.session_id == seeded.session_id))
        await sessions_repo.update_session(
            s, seeded.session_id, dek_wrapped=None, dek_destroyed_at=now, state="purged", purged_at=now
        )
    keycache.invalidate(seeded.session_id)


# --------------------------------------------------------------------------- REST app


def build_app(engine: AsyncEngine, redis: Redis | None, settings: Settings, keycache: KeyCache) -> FastAPI:
    """WP-E's ``create_app`` when it exists (deps injected directly — ASGITransport runs no lifespan),
    otherwise a minimal app with just the notes router and the problem+json handler."""
    deps = SimpleNamespace(
        settings=settings,
        engine=engine,
        redis=redis,
        kek=LocalKek(settings.kek_master_bytes),
        keycache=keycache,
        objectstore=None,
        clock=SystemClock(),
        node_id="test-d",
    )
    try:
        from chartwire.api.app import create_app

        app = create_app(settings)
    except ImportError:
        app = FastAPI(title="chartwire notes (test)")
        app.include_router(notes_router.router)
    notes_router.install_error_handlers(app)
    app.state.deps = deps
    app.dependency_overrides[jwt_secret] = lambda: settings.jwt_secret
    return app


def token(settings: Settings, *, tenant_id: UUID, user_id: UUID | str, role: str) -> str:
    return jwt.issue({"sub": str(user_id), "tid": str(tenant_id), "role": role}, secret=settings.jwt_secret)


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
