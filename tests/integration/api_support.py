"""Shared helpers for the WP-E REST / purge suites (not a test module).

Builds the real ``create_app`` with ``AppDeps`` injected (``httpx.ASGITransport`` runs no lifespan),
issues JWTs signed with the test settings, gives fixture tenants a *real* wrapped record key (the
``conftest`` factories store placeholders), and seeds a fully populated session — encrypted
segments, search rows, risk events, an unsigned draft and a signed note, ledger rows with object
files, stt offsets and the Redis hot state — the way the runtime processes write them, so the
purge pipeline has something to destroy and verify. All text is synthetic Korean; no real PHI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.api.app import create_app
from chartwire.api.deps import AppDeps
from chartwire.auth import jwt
from chartwire.core.clock import SystemClock
from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.crypto.envelope import Envelope, aad, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.crypto.keycache import KeyCache
from chartwire.db.models import Note, NoteStatement, Tenant
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import notes as notes_repo
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import search as search_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore.base import chunk_key
from chartwire.objectstore.localfs import LocalFs
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys
from chartwire.ws.watch import segment_aad

ALL_SCOPES = ["recording", "transcription", "ai_drafting", "search_index"]

SCRIPT: list[tuple[str, str]] = [
    ("clinician", "지난 2주 동안 어떻게 지내셨어요?"),
    ("patient", "잠드는 데 두 시간쯤 걸려요"),
    ("patient", "요즘은 다 사라지고 싶다는 생각이 들어요"),
    ("clinician", "에스시탈로프람을 15mg으로 올려보겠습니다"),
]
"""Synthetic utterances; segment 2 carries a lexicon hit so a risk event is realistic."""


# --------------------------------------------------------------------------- app / tokens


def make_deps(engine: AsyncEngine, redis: Redis | None, settings: Settings, objectstore: Any) -> AppDeps:
    kek = LocalKek(settings.kek_master_bytes)
    return AppDeps(
        settings=settings,
        engine=engine,
        redis=redis,
        kek=kek,
        keycache=KeyCache(kek),
        objectstore=objectstore,
        clock=SystemClock(),
        node_id="test-e",
    )


def build_app(deps: AppDeps) -> FastAPI:
    """``create_app`` + injected deps + the ops wiring the lifespan would do (no signal handlers)."""
    app = create_app(deps.settings)
    app.state.deps = deps
    try:
        from chartwire.ops import routes as ops_routes

        ops_routes.on_startup(app, deps, role="api", install_signals=False)
    except ImportError:  # WP-G absent: /healthz etc. simply are not mounted
        pass
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


def token(settings: Settings, *, tenant_id: UUID, role: str, user_id: UUID | str | None = None) -> str:
    sub = str(user_id) if user_id is not None else f"dev:{role}"
    return jwt.issue({"sub": sub, "tid": str(tenant_id), "role": role}, secret=settings.jwt_secret)


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def headers(
    settings: Settings, *, tenant_id: UUID, role: str, user_id: UUID | str | None = None
) -> dict[str, str]:
    return bearer(token(settings, tenant_id=tenant_id, role=role, user_id=user_id))


def handler_ctx(deps: AppDeps, clock: Any | None = None) -> HandlerContext:
    return HandlerContext(
        engine=deps.engine,
        redis=deps.redis,
        objectstore=deps.objectstore,
        clock=clock or deps.clock,
        settings=deps.settings,
        kek=deps.kek,
        keycache=deps.keycache,
    )


# --------------------------------------------------------------------------- key material


async def real_record_key(owner_engine: AsyncEngine, tenant: Tenant, kek: LocalKek) -> bytes:
    """Replace the fixture placeholder with a real wrapped record key; returns the plaintext key."""
    record_key = Envelope.new_dek()
    wrapped = kek.wrap(record_key, tenant.kek_ref)
    async with owner_engine.begin() as conn:
        await conn.execute(update(Tenant).where(Tenant.id == tenant.id).values(record_key_wrapped=wrapped))
    tenant.record_key_wrapped = wrapped
    return record_key


async def real_patient_dek(
    app_engine: AsyncEngine, tenant: Tenant, patient_id: UUID, kek: LocalKek, *, name: str, phone: str | None
) -> bytes:
    """Give a fixture patient a real DEK with an encrypted name/phone (what ``POST /patients`` does)."""
    dek = Envelope.new_dek()
    wrapped = kek.wrap(dek, tenant.kek_ref)
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        patient = await patients_repo.get_patient(s, patient_id)
        assert patient is not None
        values: dict[str, Any] = {
            "dek_wrapped": wrapped,
            "dek_fingerprint": dek_fingerprint(wrapped),
            "name_enc": Envelope.encrypt(
                dek, name.encode(), aad(tenant.id, "patient", patient.pseudonym, "name")
            ),
            "phone_enc": None
            if phone is None
            else Envelope.encrypt(dek, phone.encode(), aad(tenant.id, "patient", patient.pseudonym, "phone")),
        }
        await s.execute(update(type(patient)).where(type(patient).id == patient_id).values(**values))
    return dek


async def real_session_dek(app_engine: AsyncEngine, tenant: Tenant, session_id: UUID, kek: LocalKek) -> bytes:
    dek = Envelope.new_dek()
    wrapped = kek.wrap(dek, tenant.kek_ref)
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        await sessions_repo.update_session(
            s, session_id, dek_wrapped=wrapped, dek_fingerprint=dek_fingerprint(wrapped)
        )
    return dek


async def session_dek_from_db(
    app_engine: AsyncEngine, tenant: Tenant, session_id: UUID, kek: LocalKek
) -> bytes:
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        row = await sessions_repo.get_session(s, session_id)
    assert row is not None and row.dek_wrapped is not None
    return kek.unwrap(bytes(row.dek_wrapped), tenant.kek_ref)


async def grant(
    app_engine: AsyncEngine, tenant_id: UUID, patient_id: UUID, scopes: list[str] | None = None
) -> UUID:
    async with tenant_tx(app_engine, TenantCtx.service(tenant_id)) as s:
        consent = await patients_repo.grant_consent(
            s, tenant_id=tenant_id, patient_id=patient_id, scopes=scopes or ALL_SCOPES, granted_by=None
        )
        return consent.id


# --------------------------------------------------------------------------- full session seed


@dataclass
class SeededSession:
    tenant: Tenant
    patient_id: UUID
    clinician_id: UUID
    session: SessionModel
    dek: bytes
    record_key: bytes
    texts: dict[int, str]
    chunk_keys: list[str] = field(default_factory=list)
    risk_event_ids: list[int] = field(default_factory=list)
    draft_note_id: UUID | None = None
    signed_note_id: UUID | None = None


def encrypt_segment(dek: bytes, tenant_id: UUID, session_id: UUID, seq: int, text: str) -> bytes:
    return Envelope.encrypt(dek, text.encode("utf-8"), segment_aad(tenant_id, session_id, seq))


async def seed_session(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    redis: Redis,
    objectstore: LocalFs,
    settings: Settings,
    *,
    tenant: Tenant,
    patient_id: UUID,
    clinician_id: UUID,
    session: SessionModel,
    script: list[tuple[str, str]] | None = None,
    chunks: int = 5,
    with_notes: bool = True,
    with_redis: bool = True,
) -> SeededSession:
    """Populate every table and store the purge touches, as the runtime would have."""
    kek = LocalKek(settings.kek_master_bytes)
    record_key = await real_record_key(owner_engine, tenant, kek)
    dek = await real_session_dek(app_engine, tenant, session.id, kek)
    started = session.started_at or datetime.now(tz=UTC)
    texts: dict[int, str] = {}
    seeded = SeededSession(tenant, patient_id, clinician_id, session, dek, record_key, texts)

    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        # ledger rows + ciphertext objects
        rows = []
        for seq in range(1, chunks + 1):
            payload = Envelope.encrypt(
                dek, bytes([seq]) * 64, aad(tenant.id, "session", session.id, f"chunk:{seq}")
            )
            key = chunk_key(tenant.id, session.id, seq)
            await objectstore.put(key, payload)
            seeded.chunk_keys.append(key)
            rows.append(
                {
                    "session_id": session.id,
                    "seq": seq,
                    "tenant_id": tenant.id,
                    "byte_len": len(payload),
                    "sha256": hashlib.sha256(payload).digest(),
                    "storage_key": key,
                    "offset_ms": (seq - 1) * 200,
                    "flags": 0,
                    "received_at": started + timedelta(milliseconds=(seq - 1) * 200),
                }
            )
        await sessions_repo.insert_chunks(s, rows)
        await sessions_repo.update_session(s, session.id, ack_seq=chunks)
        # final segments (+ search rows, + one risk event on the lexicon hit)
        for seq, (speaker, text) in enumerate(script or SCRIPT):
            texts[seq] = text
            created_at = started + timedelta(seconds=3 * seq)
            segment = await segments_repo.insert_final(
                s,
                tenant_id=tenant.id,
                session_id=session.id,
                patient_id=patient_id,
                seq=seq,
                speaker=speaker,
                t_start_ms=seq * 3000,
                t_end_ms=seq * 3000 + 2500,
                text_enc=encrypt_segment(dek, tenant.id, session.id, seq, text),
                text_len=len(text),
                confidence=0.9,
                provider="simulator",
                created_at=created_at,
            )
            await search_repo.index_segment(
                s,
                segment_id=segment.id,
                segment_created_at=segment.created_at,
                tenant_id=tenant.id,
                session_id=session.id,
                patient_id=patient_id,
                speaker=speaker,
                text_plain=text,
                terms=["불면"] if "잠" in text else [],
            )
            if "사라지고" in text:
                event = await risk_repo.insert_event(
                    s,
                    tenant_id=tenant.id,
                    session_id=session.id,
                    patient_id=patient_id,
                    segment_id=segment.id,
                    segment_created_at=segment.created_at,
                    segment_seq=seq,
                    category="suicidal_ideation",
                    severity=2,
                    phrase="사라지고 싶",
                    span_start=6,
                    span_end=12,
                    scope={},
                    detector_version="lex-1",
                    detected_at=created_at,
                    sla_deadline_at=created_at + timedelta(seconds=300),
                )
                seeded.risk_event_ids.append(int(event.id))
                await redis.zadd(
                    keys.ALERTS_SLA,
                    {
                        keys.alerts_sla_member(tenant.id, int(event.id)): event.sla_deadline_at.timestamp()
                        * 1000
                    },
                )
        await sessions_repo.upsert_stt_offset(
            s,
            session_id=session.id,
            tenant_id=tenant.id,
            last_chunk_seq=chunks,
            last_segment_seq=len(texts) - 1,
        )
        if with_notes:
            draft = await notes_repo.create_note(
                s,
                id=uuid7(),
                tenant_id=tenant.id,
                session_id=session.id,
                version=1,
                status="verified",
                provider="extractive",
                statement_count=1,
                raw_draft_enc=Envelope.encrypt(
                    dek, b'{"statements":[]}', aad(tenant.id, "session", session.id, "raw_draft")
                ),
            )
            await notes_repo.insert_statements(
                s,
                [
                    {
                        "tenant_id": tenant.id,
                        "note_id": draft.id,
                        "section": "S",
                        "ordinal": 0,
                        "text_enc": Envelope.encrypt(
                            dek, texts[1].encode(), aad(tenant.id, "session", session.id, "stmt:0")
                        ),
                        "evidence": [{"seq": 1, "quote_hash": "x", "start": 0, "end": 5, "method": "exact"}],
                        "verdict": "supported",
                    }
                ],
            )
            seeded.draft_note_id = draft.id
            signed_at = started + timedelta(minutes=30)
            signed = await notes_repo.create_note(
                s,
                id=uuid7(),
                tenant_id=tenant.id,
                session_id=session.id,
                version=2,
                status="signed",
                provider="extractive",
                statement_count=1,
                signed_content_enc=Envelope.encrypt(
                    record_key, b'{"S":["signed statement"]}', aad(tenant.id, "note", session.id, "signed")
                ),
                legal_hold="medical_record",
                retention_until=signed_at + timedelta(days=3653),
                signed_by=clinician_id,
                signed_at=signed_at,
            )
            seeded.signed_note_id = signed.id
        await sessions_repo.set_state(s, session.id, "ended", now=started + timedelta(minutes=5))

    if with_redis:
        await redis.hset(
            keys.sess(session.id), mapping={"epoch": "1", "state": "ended", "ack_seq": str(chunks)}
        )
        await redis.xadd(
            keys.sess_chunks(session.id),
            {
                "seq": "1",
                "key": seeded.chunk_keys[0],
                "len": "1",
                "off": "0",
                "fl": "0",
                "ep": "1",
                "ts": "0",
            },
        )
        await redis.sadd(keys.sess_viewers(session.id), "conn-1")
        await redis.sadd(keys.STT_ACTIVE, str(session.id))
        await redis.set(keys.stt_owner(session.id), "worker-1")
        await redis.hset(keys.STT_LAG, str(session.id), "0")
    return seeded


async def note_rows(app_engine: AsyncEngine, tenant_id: UUID, session_id: UUID) -> list[Note]:
    async with tenant_tx(app_engine, TenantCtx.service(tenant_id)) as s:
        return list(
            (await s.scalars(select(Note).where(Note.session_id == session_id).order_by(Note.version))).all()
        )


async def statement_count(app_engine: AsyncEngine, tenant_id: UUID, note_id: UUID) -> int:
    async with tenant_tx(app_engine, TenantCtx.service(tenant_id)) as s:
        return len((await s.scalars(select(NoteStatement).where(NoteStatement.note_id == note_id))).all())
