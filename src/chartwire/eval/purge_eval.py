"""``purge.json`` — the purge / crypto-shred eval (spec §11.1, §8.4).

Two entry points:

* :func:`run` **executes** the measurement — it seeds ``patients`` × ``sessions_per_patient``
  synthetic sessions (audio-chunk objects on a temp ``LocalFs``, encrypted segments, search rows,
  risk events, an unsigned draft and one *signed* note per patient), runs the real purge pipeline
  and ``purge.verify`` for every session **and** every patient, then tallies what is left, and
  writes ``var/eval/purge.json``;
* :func:`collect` validates that file and re-emits it under the common report header.

Missing input → no report → the README rows are deleted (never an "expected" number, §0.2).

Shape of ``var/eval/purge.json``::

    {"sessions_purged": 50, "patients_purged": 10, "residual_rows": 0, "residual_objects": 0,
     "residual_keys": 0, "unwrap_failure_pct": 100.0, "decrypt_failure_pct": 100.0,
     "receipts_verified_pct": 100.0, "signed_notes_surviving": 10, ...}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select

from chartwire.core.config import Settings
from chartwire.crypto.envelope import Envelope
from chartwire.crypto.errors import DecryptError
from chartwire.db.models import Note
from chartwire.db.repo import purge as purge_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.eval.harness_env import EvalEnv, eval_env, fixtures
from chartwire.objectstore.base import session_prefix
from chartwire.purge import pipeline, receipt, verify
from chartwire.redis import keys

TENANT_SLUG: Final = "purge-eval"

DEFAULT_INPUT: Final = Path("var/eval/purge.json")
REQUIRED: Final[tuple[str, ...]] = (
    "residual_rows",
    "residual_objects",
    "residual_keys",
    "unwrap_failure_pct",
    "decrypt_failure_pct",
    "receipts_verified_pct",
)


class MissingMeasurement(FileNotFoundError):
    """The producing run has not happened; the report must not be written."""


def collect(source: Path = DEFAULT_INPUT, *, required: tuple[str, ...] = REQUIRED) -> dict[str, Any]:
    if not source.is_file():
        raise MissingMeasurement(f"{source} 가 없습니다 — 파기 파이프라인 측정이 아직 실행되지 않았습니다")
    body = json.loads(source.read_text(encoding="utf-8"))
    missing = [k for k in required if k not in body]
    if missing:
        raise ValueError(f"{source}: 필수 필드가 없습니다: {missing}")
    return {"source": str(source), **body}


# --------------------------------------------------------------------------- the executing run

SCRIPT: Final[list[tuple[str, str]]] = [
    ("clinician", "지난 2주 동안 어떻게 지내셨어요?"),
    ("patient", "잠드는 데 두 시간쯤 걸려요"),
    ("patient", "요즘은 다 사라지고 싶다는 생각이 들어요"),
    ("patient", "에스시탈로프람 10mg 먹고 있어요"),
    ("clinician", "에스시탈로프람을 15mg으로 올려보겠습니다"),
]
"""Synthetic utterances (§0.1). Segment 2 carries a lexicon hit so a risk event is realistic."""


@dataclass
class _Seeded:
    session_id: UUID
    signed_note_id: UUID
    record_key: bytes
    """The tenant record key *at signing time* — ``seed_session`` rotates it per session, so a
    signed note is only decryptable with the key its own run produced (§0.6: the signed record is
    independent of the session DEK, which is what this eval destroys)."""


async def _purge_and_verify(env: EvalEnv, tenant_id: UUID, subject_type: str, subject_id: UUID) -> Any:
    """``create_job`` → ``pipeline.run`` → ``verify.run`` for one subject; returns the verified job."""
    api_support, _ = fixtures()
    ctx = api_support.handler_ctx(env.deps)
    async with tenant_tx(env.app_engine, TenantCtx.service(tenant_id)) as s:
        job = await pipeline.create_job(
            s,
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            reason="admin",  # DDL CHECK: consent_revoked | admin | retention
            requested_by=None,
        )
    await pipeline.run(ctx, job.id, tenant_id=tenant_id)
    return await verify.run(ctx, job.id, tenant_id=tenant_id)


async def _residuals(env: EvalEnv, tenant_id: UUID, session_ids: list[UUID]) -> dict[str, int]:
    """An independent sweep after every job: rows, objects and Redis keys that must not exist."""
    rows = objects = redis_keys = 0
    async with tenant_tx(env.app_engine, TenantCtx.service(tenant_id)) as s:
        for sid in session_ids:
            rows += sum((await purge_repo.count_session_data(s, sid)).values())
    for sid in session_ids:
        objects += len(await env.objectstore.list(session_prefix(tenant_id, sid)))
        redis_keys += len([k async for k in env.redis.scan_iter(match=keys.sess_pattern(sid), count=200)])
        redis_keys += int(bool(await env.redis.sismember(keys.STT_ACTIVE, str(sid))))
        redis_keys += int((await env.redis.get(keys.stt_owner(sid))) is not None)
        redis_keys += int((await env.redis.hget(keys.STT_LAG, str(sid))) is not None)
    return {"residual_rows": rows, "residual_objects": objects, "residual_keys": redis_keys}


def _receipt_valid(job: Any) -> bool:
    """``verified`` **and** the receipt hash still matches its steps/counts/fingerprints (§8.4)."""
    if job.state != "verified" or job.receipt_hash is None:
        return False
    expected = receipt.receipt_hash(list(job.steps), dict(job.counts), list(job.dek_fingerprints))
    return bytes(job.receipt_hash) == expected


async def _signed_notes_alive(env: EvalEnv, tenant_id: UUID, seeded: list[_Seeded]) -> int:
    """Signed notes still present, on legal hold, and still decryptable under their record key."""
    by_id = {s.signed_note_id: s for s in seeded}
    async with tenant_tx(env.app_engine, TenantCtx.service(tenant_id)) as s:
        rows = list((await s.scalars(select(Note).where(Note.id.in_(list(by_id))))).all())
    alive = 0
    for note in rows:
        if note.legal_hold != "medical_record" or note.signed_content_enc is None:
            continue
        entry = by_id[note.id]
        try:
            Envelope.decrypt(
                entry.record_key,
                bytes(note.signed_content_enc),
                f"{tenant_id}:note:{entry.session_id}:signed",
            )
        except DecryptError:
            continue
        alive += 1
    return alive


def _pct(hits: int, total: int) -> float:
    return round(100.0 * hits / total, 2) if total else 0.0


async def _seed(
    env: EvalEnv, settings: Settings, patients: int, per_patient: int
) -> dict[UUID, list[_Seeded]]:
    api_support, _ = fixtures()
    tenant = await env.factories.tenant(TENANT_SLUG)
    clinician = await env.factories.user(tenant.id, "clinician")
    env.tenant = tenant
    out: dict[UUID, list[_Seeded]] = {}
    for _ in range(patients):
        patient = await env.factories.patient(tenant.id)
        await api_support.real_patient_dek(
            env.app_engine, tenant, patient.id, env.deps.kek, name="가상환자 파기평가", phone="010-0000-0000"
        )
        await api_support.grant(env.app_engine, tenant.id, patient.id)
        out[patient.id] = []
        for _ in range(per_patient):
            session = await env.factories.session(tenant.id, patient.id, clinician.id)
            seeded = await api_support.seed_session(
                env.app_engine,
                env.owner_engine,
                env.redis,
                env.objectstore,
                settings,
                tenant=tenant,
                patient_id=patient.id,
                clinician_id=clinician.id,
                session=session,
                script=SCRIPT,
            )
            assert seeded.signed_note_id is not None
            out[patient.id].append(_Seeded(session.id, seeded.signed_note_id, seeded.record_key))
    return out


async def run(
    settings: Settings | None = None,
    *,
    patients: int = 10,
    sessions_per_patient: int = 5,
    out: Path = DEFAULT_INPUT,
    migrate: bool = True,
) -> dict[str, Any]:
    """Seed → purge → verify → tally; writes ``out`` (``var/eval/purge.json``) and returns its body."""
    settings = settings or Settings()
    started = time.monotonic()
    async with eval_env(settings, migrate=migrate) as env:
        by_patient = await _seed(env, settings, patients, sessions_per_patient)
        tenant_id = env.tenant.id
        all_seeded = [s for group in by_patient.values() for s in group]
        session_ids = [s.session_id for s in all_seeded]
        unwrap_ok = decrypt_ok = receipts_ok = jobs = 0
        for sid in session_ids:
            job = await _purge_and_verify(env, tenant_id, "session", sid)
            jobs += 1
            checks = job.verify_result["checks"]
            unwrap_ok += bool(checks.get(f"unwrap:{sid}"))
            decrypt_ok += bool(checks.get(f"decrypt_sample:{sid}"))
            receipts_ok += _receipt_valid(job)
        for patient_id in by_patient:
            job = await _purge_and_verify(env, tenant_id, "patient", patient_id)
            jobs += 1
            receipts_ok += _receipt_valid(job)
        body: dict[str, Any] = {
            "sessions_purged": len(session_ids),
            "patients_purged": len(by_patient),
            "jobs": jobs,
            **await _residuals(env, tenant_id, session_ids),
            "unwrap_failure_pct": _pct(unwrap_ok, len(session_ids)),
            "decrypt_failure_pct": _pct(decrypt_ok, len(session_ids)),
            "receipts_verified_pct": _pct(receipts_ok, jobs),
            "signed_notes_surviving": await _signed_notes_alive(env, tenant_id, all_seeded),
            "signed_notes_expected": len(all_seeded),
            "duration_s": round(time.monotonic() - started, 2),
            "db": env.db_name,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
    write_measurement(out, body)
    return body


def write_measurement(out: Path, body: dict[str, Any]) -> None:
    """The measurement file the aggregator later re-emits with the §11.1 header."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
