"""Note lifecycle: draft → clinician review → sign (spec §9, §6.9, §7.2, §8.4, ADR-0004).

Two key scopes meet here and must never be confused:

* the **session DEK** protects everything that is purged with the consultation — ``raw_draft_enc``,
  ``note_statements.text_enc`` / ``edited_text_enc``;
* the **tenant record key** (``tenants.record_key_wrapped``, never destroyed) protects what the law
  keeps for ten years — ``note_assessments.text_enc`` and ``notes.signed_content_enc``.

A signed note is therefore *self-contained*: the S/O/P statements the clinician accepted, their
verbatim evidence quotes, the assessment, ids and timestamps are serialised into one document and
encrypted under the record key at signing time, while the session DEK still exists. After a purge the
statements' ciphertext is unreadable and the signed document is the only readable copy — which is
exactly what :func:`read_note` returns for a signed note.

The draft path never falls back between providers (provenance): a provider error is recorded as
``abstained(provider_error)``. Transcript text is never logged; only ids, counts and reasons are.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.audit import service as audit
from chartwire.consent.gates import ConsentScopeMissing, active_scopes, require_scope
from chartwire.core.errors import AppError, NotFound
from chartwire.core.ids import uuid7
from chartwire.crypto.envelope import Envelope, aad
from chartwire.crypto.errors import DecryptError, DekDestroyedError
from chartwire.crypto.keycache import KeyCache
from chartwire.db.models import Note, NoteAssessment, NoteStatement, Tenant
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import notes as notes_repo
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.notes import policy
from chartwire.notes.extractive import ExtractiveProvider
from chartwire.notes.providers.base import ProviderError
from chartwire.notes.repo_adapter import load_segment_views
from chartwire.notes.schema import (
    DraftContext,
    DraftSchemaError,
    NoteDraftOut,
    NoteProvider,
    NoteStatus,
    RawDraft,
    SegmentView,
    VerifiedDraft,
    parse_draft,
)
from chartwire.notes.verifier import VERIFIER_VERSION, verify
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys

try:  # WP-E's DB-backed consent helper (contract); the pure gate is the fallback
    from chartwire.consent.service import active_scopes_for_patient as _active_scopes_for_patient
except ImportError:  # pragma: no cover - depends on the build order
    _active_scopes_for_patient = None

try:  # WP-G's single metrics registry; optional so the notes package imports on its own
    from chartwire.ops import metrics as _metrics
except ImportError:  # pragma: no cover
    _metrics = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

AI_DRAFTING_SCOPE = "ai_drafting"
RETENTION_YEARS = 10
"""의료법: a signed chart is kept ten years from signing (ADR-0004)."""
SIGNED_SCHEMA = "chartwire.signed_note.v1"
Decision = Literal["accept", "edit", "reject"]
DRAFTABLE_STATES: frozenset[str] = frozenset({"ended", "transcribed", "drafted"})
"""Session states in which drafting is meaningful; ``drafted`` is set only from one of these."""

# --- error codes (problem+json ``code``; §3.1 CW-4xxx) -------------------------------------------
NOTE_NOT_FOUND = "CW-4041"
NOTE_ALREADY_SIGNED = "CW-4091"
NOTE_ASSESSMENT_MISSING = "CW-4092"
NOTE_UNSUPPORTED_PENDING = "CW-4093"
NOTE_NOT_REVIEWABLE = "CW-4094"
NOTE_EDIT_TEXT_REQUIRED = "CW-4221"


class NoteError(AppError):
    """A note-state refusal (409/422) carrying a stable code."""


# ================================================================== views (what routers render)


@dataclass(frozen=True, slots=True)
class EvidenceView:
    seq: int
    quote: str
    start: int | None
    end: int | None
    method: str | None


@dataclass(frozen=True, slots=True)
class StatementView:
    id: int
    section: str
    ordinal: int
    text: str
    evidence: list[EvidenceView]
    verdict: str
    verdict_reason: str | None
    decision: str | None
    edited_text: str | None


@dataclass(frozen=True, slots=True)
class AssessmentView:
    text: str
    author_id: UUID
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NoteView:
    id: UUID
    session_id: UUID
    patient_id: UUID
    clinician_id: UUID
    version: int
    status: str
    provider: str
    model: str | None
    coverage: float | None
    statement_count: int
    unsupported_count: int
    abstain_reason: str | None
    statements: list[StatementView]
    assessment: AssessmentView | None
    legal_hold: str | None
    retention_until: datetime | None
    signed_at: datetime | None
    signed_by: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NoteOutcome:
    """What :func:`draft_for_session` did. ``status == "skipped"`` means no note row was written."""

    session_id: UUID
    note_id: UUID | None
    version: int
    status: str
    abstain_reason: str | None
    provider: str
    coverage: float | None
    statement_count: int
    unsupported_count: int
    published: bool = False

    @property
    def skipped(self) -> bool:
        return self.note_id is None


# ================================================================== keys and AAD


def record_key(keycache: KeyCache, tenant: Tenant) -> bytes:
    """The tenant record key (never destroyed). Cached under ``record:{tenant_id}``."""
    return keycache.get(f"record:{tenant.id}", tenant.kek_ref, tenant.record_key_wrapped)


def session_dek(keycache: KeyCache, tenant: Tenant, sess: SessionModel) -> bytes:
    """The session DEK; raises :class:`DekDestroyedError` for a purged session."""
    return keycache.get(sess.id, tenant.kek_ref, sess.dek_wrapped)


def statement_aad(tenant_id: UUID, note_id: UUID, section: str, ordinal: int, *, edited: bool = False) -> str:
    suffix = ":edited" if edited else ""
    return aad(tenant_id, "note", note_id, f"statement:{section}:{ordinal}{suffix}")


def raw_draft_aad(tenant_id: UUID, note_id: UUID) -> str:
    return aad(tenant_id, "note", note_id, "raw_draft")


def signed_content_aad(tenant_id: UUID, note_id: UUID) -> str:
    return aad(tenant_id, "note", note_id, "signed_content")


def assessment_aad(tenant_id: UUID, note_id: UUID) -> str:
    return aad(tenant_id, "note", note_id, "assessment")


def quote_hash(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _prompt_hash_bytes(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return hashlib.sha256(value.encode("utf-8")).digest()


# ================================================================== provider selection


def provider_from_settings(settings: Any) -> NoteProvider:
    """``extractive`` (default) or ``anthropic`` — the latter only when a model is configured.

    Raises :class:`ProviderError` (→ ``abstained(provider_error)``) instead of silently using the
    extractive baseline when the configured LLM provider cannot be built: provenance over convenience.
    """
    name = getattr(settings, "note_provider", "extractive")
    if name == "extractive":
        return ExtractiveProvider()
    if name == "anthropic":
        model = getattr(settings, "anthropic_model", None)
        if not model:
            raise ProviderError("anthropic provider selected without CHARTWIRE_ANTHROPIC_MODEL")
        try:
            from chartwire.notes.providers.anthropic import AnthropicProvider

            return AnthropicProvider(model)
        except ImportError as exc:
            raise ProviderError("anthropic SDK unavailable") from exc
        except Exception as exc:  # SDK client construction (missing key, bad config): class name only
            raise ProviderError(f"anthropic client init failed: {type(exc).__name__}") from exc
    raise ProviderError(f"unknown note provider {name!r}")


# ================================================================== drafting (§7.2 note_draft)


@dataclass(slots=True)
class _Loaded:
    """Everything the read phase produced; ``segments`` is empty when the consent gate failed."""

    sess: SessionModel
    tenant: Tenant
    dek: bytes | None
    segments: list[SegmentView] = field(default_factory=list)
    consent_missing: bool = False


async def _find_tenant(engine: Any, session_id: UUID) -> UUID:
    """ADR-0001: no bypass role, so a session without a known tenant is found by iterating tenants."""
    from chartwire.outbox.runtime import active_tenant_ids

    for tenant_id in await active_tenant_ids(engine):
        async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
            if await sessions_repo.get_session(s, session_id) is not None:
                return tenant_id
    raise LookupError(f"session {session_id} not found in any active tenant")


async def _consent_scopes(s: AsyncSession, patient_id: UUID) -> set[str]:
    if _active_scopes_for_patient is not None:
        return set(await _active_scopes_for_patient(s, patient_id))
    return active_scopes(await patients_repo.list_consents(s, patient_id))


async def _load(s: AsyncSession, keycache: KeyCache, tenant_id: UUID, session_id: UUID) -> _Loaded | None:
    sess = await sessions_repo.get_session(s, session_id)
    if sess is None:
        raise LookupError(f"session {session_id} not found")
    tenant = await s.get(Tenant, tenant_id)
    if tenant is None:
        raise LookupError(f"tenant {tenant_id} not found")
    if sess.state in ("purging", "purged") or sess.dek_wrapped is None:
        return None
    try:
        require_scope(await _consent_scopes(s, sess.patient_id), AI_DRAFTING_SCOPE)
    except ConsentScopeMissing:
        return _Loaded(sess=sess, tenant=tenant, dek=None, consent_missing=True)
    dek = session_dek(keycache, tenant, sess)
    views = await load_segment_views(s, session_id=session_id, dek=dek, started_at=sess.started_at)
    return _Loaded(sess=sess, tenant=tenant, dek=dek, segments=views)


@dataclass(slots=True)
class _Drafted:
    raw: RawDraft | None
    draft: NoteDraftOut | None
    verified: VerifiedDraft | None
    schema_failures: int
    provider_error: bool


async def _run_provider(provider: NoteProvider, dctx: DraftContext) -> _Drafted:
    """Call the provider at most ``MAX_SCHEMA_FAILURES`` times; a transport error ends the attempt."""
    raw: RawDraft | None = None
    draft: NoteDraftOut | None = None
    failures = 0
    for _ in range(policy.MAX_SCHEMA_FAILURES):
        try:
            raw = await provider.draft(dctx)
        except ProviderError as exc:
            log.warning(
                "note provider failed", extra={"provider": provider.name, "error": type(exc).__name__}
            )
            return _Drafted(raw, None, None, failures, True)
        try:
            draft = parse_draft(raw.text)
            break
        except DraftSchemaError as exc:
            failures += 1
            log.warning(
                "draft schema rejected",
                extra={"provider": provider.name, "attempt": failures, "why": str(exc)},
            )
    verified = verify(draft, dctx.segments) if draft is not None else None
    return _Drafted(raw, draft, verified, failures, False)


def _statement_rows(
    tenant_id: UUID, note_id: UUID, dek: bytes, verified: VerifiedDraft
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counters = {"S": 0, "O": 0, "P": 0}
    for st in verified.statements:
        ordinal = counters[st.section]
        counters[st.section] += 1
        rows.append(
            {
                "tenant_id": tenant_id,
                "note_id": note_id,
                "section": st.section,
                "ordinal": ordinal,
                "text_enc": Envelope.encrypt(
                    dek, st.text.encode("utf-8"), statement_aad(tenant_id, note_id, st.section, ordinal)
                ),
                "evidence": [
                    {
                        "seq": ev.seq,
                        "quote_hash": quote_hash(ev.quote),
                        "start": ev.start,
                        "end": ev.end,
                        "method": ev.method,
                    }
                    for ev in st.evidence
                ],
                "verdict": st.verdict,
                "verdict_reason": st.verdict_reason,
                "method": next((ev.method for ev in st.evidence if ev.method), None),
            }
        )
    return rows


async def draft_for_session(
    ctx: HandlerContext,
    session_id: UUID,
    *,
    tenant_id: UUID | None = None,
    provider: NoteProvider | None = None,
) -> NoteOutcome:
    """Draft one note version for a session (the ``session.transcribed`` handler body).

    Read phase (tenant tx): session, tenant, live consent (``ai_drafting`` gate), decrypted segments.
    Provider phase (no tx held outside the poller): draft → ``parse_draft`` (≤2) → ``verify`` → ``decide``.
    Write phase (tenant tx): ``notes`` + ``note_statements`` + ``sessions.state='drafted'`` + audit.
    Then PUBLISH ``note.status`` and record metrics. Under the outbox poller the two transactions are
    the one handler transaction (``HandlerContext.tenant_tx`` joins it), so the note and the
    ``processed_events`` row commit together.
    """
    tenant_id = tenant_id or await _find_tenant(ctx.engine, session_id)
    provider_name = "extractive"
    build_error = False
    if provider is None:
        try:
            provider = provider_from_settings(ctx.settings)
        except ProviderError as exc:
            build_error = True
            provider_name = str(getattr(ctx.settings, "note_provider", "extractive"))
            log.warning(
                "note provider unavailable", extra={"provider": provider_name, "error": type(exc).__name__}
            )
    if provider is not None:
        provider_name = provider.name

    async with ctx.tenant_tx(tenant_id) as s:
        loaded = await _load(s, ctx.keycache, tenant_id, session_id)
    if loaded is None:
        log.info("note draft skipped: session purged", extra={"session_id": str(session_id)})
        return NoteOutcome(session_id, None, 0, "skipped", None, provider_name, None, 0, 0)

    t0 = ctx.clock.monotonic()
    if loaded.consent_missing:
        drafted = _Drafted(None, None, None, 0, False)
        decision = policy.Decision(NoteStatus.abstained, "consent_scope_missing")
    elif build_error or provider is None:
        drafted = _Drafted(None, None, None, 0, True)
        decision = policy.decide(None, provider_error=True)
    else:
        drafted = await _run_provider(provider, DraftContext(session_id=session_id, segments=loaded.segments))
        decision = policy.decide(
            drafted.verified, schema_failures=drafted.schema_failures, provider_error=drafted.provider_error
        )
    elapsed_s = ctx.clock.monotonic() - t0

    verified = drafted.verified
    note_id = uuid7()
    raw_enc = None
    if drafted.raw is not None and loaded.dek is not None:
        raw_enc = Envelope.encrypt(
            loaded.dek, drafted.raw.text.encode("utf-8"), raw_draft_aad(tenant_id, note_id)
        )
    async with ctx.tenant_tx(tenant_id) as s:
        version = await notes_repo.next_version(s, session_id)
        await notes_repo.create_note(
            s,
            id=note_id,
            tenant_id=tenant_id,
            session_id=session_id,
            version=version,
            status=decision.status.value,
            provider=provider_name,
            model=drafted.raw.model if drafted.raw else None,
            prompt_hash=_prompt_hash_bytes(drafted.raw.prompt_hash) if drafted.raw else None,
            grounding_coverage=verified.coverage if verified else None,
            statement_count=len(verified.statements) if verified else 0,
            unsupported_count=verified.unsupported_count if verified else 0,
            abstain_reason=decision.reason,
            raw_draft_enc=raw_enc,
        )
        if verified is not None and loaded.dek is not None:
            await notes_repo.insert_statements(s, _statement_rows(tenant_id, note_id, loaded.dek, verified))
        if loaded.sess.state in DRAFTABLE_STATES:
            await sessions_repo.set_state(s, session_id, "drafted")
        detail = {
            "version": version,
            "status": decision.status.value,
            "abstain_reason": decision.reason,
            "provider": provider_name,
            "statement_count": len(verified.statements) if verified else 0,
            "unsupported_count": verified.unsupported_count if verified else 0,
            "verifier_version": VERIFIER_VERSION,
        }
        for action, rtype, rid in (
            ("note.drafted", "note", note_id),
            ("session.drafted", "session", session_id),
        ):
            await audit.record(
                s,
                tenant_id=tenant_id,
                actor_id=None,
                actor_role="service",
                action=action,
                resource_type=rtype,
                resource_id=rid,
                detail=detail,
            )

    outcome = NoteOutcome(
        session_id=session_id,
        note_id=note_id,
        version=version,
        status=decision.status.value,
        abstain_reason=decision.reason,
        provider=provider_name,
        coverage=verified.coverage if verified else None,
        statement_count=len(verified.statements) if verified else 0,
        unsupported_count=verified.unsupported_count if verified else 0,
        published=await publish_note_status(ctx.redis, session_id, note_id, decision.status.value, verified),
    )
    _record_metrics(outcome, verified, elapsed_s)
    log.info(
        "note drafted",
        extra={
            "session_id": str(session_id),
            "note_id": str(note_id),
            "version": version,
            "status": outcome.status,
            "reason": outcome.abstain_reason,
        },
    )
    return outcome


async def publish_note_status(
    redis: Any, session_id: UUID, note_id: UUID, status: str, verified: VerifiedDraft | None
) -> bool:
    """§6.3 ``note.status{note_id, status, coverage, unsupported_count}`` on ``sess:{sid}:events``."""
    if redis is None:
        return False
    msg = {
        "t": "note.status",
        "note_id": str(note_id),
        "status": status,
        "coverage": verified.coverage if verified else None,
        "unsupported_count": verified.unsupported_count if verified else 0,
    }
    try:
        await redis.publish(keys.sess_events(session_id), json.dumps(msg, ensure_ascii=False))
    except Exception as exc:  # Redis is a cache (ADR-0002): the note is committed regardless
        log.warning("note.status publish failed", extra={"error": type(exc).__name__})
        return False
    return True


def _record_metrics(outcome: NoteOutcome, verified: VerifiedDraft | None, elapsed_s: float) -> None:
    if _metrics is None:
        return
    _metrics.NOTE_STATUS_TOTAL.labels(status=outcome.status).inc()
    _metrics.NOTE_DRAFT_SECONDS.labels(provider=outcome.provider).observe(max(elapsed_s, 0.0))
    if verified is not None:
        _metrics.NOTE_COVERAGE.observe(verified.coverage)
        for st in verified.statements:
            if st.verdict_reason:
                _metrics.NOTE_VERIFY_REASON_TOTAL.labels(reason=st.verdict_reason).inc()


# ================================================================== manual draft request (§6.9)


async def request_draft(
    s: AsyncSession,
    *,
    tenant_id: UUID,
    sess: SessionModel,
    now: datetime,
    actor: TenantCtx,
    request_id: str | None,
) -> int | None:
    """Enqueue ``session.transcribed`` for the note_draft handler (POST /sessions/{id}/notes/draft).

    Idempotency key carries the request time: every manual request drafts a new version.
    """
    from chartwire.outbox import writer

    event_id = await writer.emit(
        s,
        tenant_id=tenant_id,
        aggregate_type="session",
        aggregate_id=sess.id,
        event_type="session.transcribed",
        payload={"session_id": str(sess.id), "patient_id": str(sess.patient_id), "manual": True},
        idempotency_key=writer.idempotency_key("session.transcribed", sess.id, f"manual:{now.isoformat()}"),
    )
    await audit.record(
        s,
        tenant_id=tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="note.draft_requested",
        resource_type="session",
        resource_id=sess.id,
        request_id=request_id,
        detail={"event_id": event_id},
    )
    return event_id


# ================================================================== reading


async def _note_context(s: AsyncSession, note: Note) -> tuple[Tenant, SessionModel]:
    tenant = await s.get(Tenant, note.tenant_id)
    sess = await sessions_repo.get_session(s, note.session_id)
    if tenant is None or sess is None:
        raise NotFound("세션", NOTE_NOT_FOUND)
    return tenant, sess


def _draft_quotes(dek: bytes | None, note: Note) -> dict[tuple[str, int], list[str]]:
    """Evidence quotes per ``(section, ordinal)`` re-read from the encrypted provider output.

    The JSON evidence column stores only a hash (no transcript text in JSONB); the verbatim quote
    lives in ``raw_draft_enc`` under the session DEK, which exists for every unsigned note.
    """
    if dek is None or note.raw_draft_enc is None:
        return {}
    try:
        raw = Envelope.decrypt(dek, note.raw_draft_enc, raw_draft_aad(note.tenant_id, note.id)).decode(
            "utf-8"
        )
        draft = parse_draft(raw)
    except (DecryptError, DraftSchemaError):
        return {}
    out: dict[tuple[str, int], list[str]] = {}
    counters = {"S": 0, "O": 0, "P": 0}
    for st in draft.statements:
        key = (st.section, counters[st.section])
        counters[st.section] += 1
        out[key] = [ev.quote for ev in st.evidence]
    return out


def _statement_views(
    note: Note, rows: list[NoteStatement], dek: bytes, quotes: dict[tuple[str, int], list[str]]
) -> list[StatementView]:
    views: list[StatementView] = []
    for row in rows:
        section = row.section.strip()
        text = Envelope.decrypt(
            dek, row.text_enc, statement_aad(note.tenant_id, note.id, section, row.ordinal)
        ).decode("utf-8")
        edited = None
        if row.edited_text_enc is not None:
            edited = Envelope.decrypt(
                dek,
                row.edited_text_enc,
                statement_aad(note.tenant_id, note.id, section, row.ordinal, edited=True),
            ).decode("utf-8")
        raw_quotes = quotes.get((section, row.ordinal), [])
        evidence = [
            EvidenceView(
                seq=int(ev["seq"]),
                quote=raw_quotes[i] if i < len(raw_quotes) else "",
                start=ev.get("start"),
                end=ev.get("end"),
                method=ev.get("method"),
            )
            for i, ev in enumerate(row.evidence)
        ]
        views.append(
            StatementView(
                id=row.id,
                section=section,
                ordinal=row.ordinal,
                text=text,
                evidence=evidence,
                verdict=row.verdict,
                verdict_reason=row.verdict_reason,
                decision=row.clinician_decision,
                edited_text=edited,
            )
        )
    return views


def _assessment_view(rkey: bytes, note: Note, row: NoteAssessment | None) -> AssessmentView | None:
    if row is None:
        return None
    text = Envelope.decrypt(rkey, row.text_enc, assessment_aad(note.tenant_id, note.id)).decode("utf-8")
    return AssessmentView(text=text, author_id=row.author_id, updated_at=row.updated_at)


def _signed_views(doc: dict[str, Any]) -> list[StatementView]:
    views: list[StatementView] = []
    for section in ("S", "O", "P"):
        for st in doc["sections"].get(section, []):
            views.append(
                StatementView(
                    id=int(st["id"]),
                    section=section,
                    ordinal=int(st["ordinal"]),
                    text=st["text"],
                    evidence=[
                        EvidenceView(
                            seq=int(e["seq"]),
                            quote=e["quote"],
                            start=e.get("start"),
                            end=e.get("end"),
                            method=e.get("method"),
                        )
                        for e in st["evidence"]
                    ],
                    verdict=st["verdict"],
                    verdict_reason=st.get("verdict_reason"),
                    decision=st.get("decision"),
                    edited_text=st.get("edited_text"),
                )
            )
    return views


def _base_view(
    note: Note, sess: SessionModel, statements: list[StatementView], assessment: AssessmentView | None
) -> NoteView:
    return NoteView(
        id=note.id,
        session_id=note.session_id,
        patient_id=sess.patient_id,
        clinician_id=sess.clinician_id,
        version=note.version,
        status=note.status,
        provider=note.provider,
        model=note.model,
        coverage=float(note.grounding_coverage) if note.grounding_coverage is not None else None,
        statement_count=note.statement_count,
        unsupported_count=note.unsupported_count,
        abstain_reason=note.abstain_reason,
        statements=statements,
        assessment=assessment,
        legal_hold=note.legal_hold,
        retention_until=note.retention_until,
        signed_at=note.signed_at,
        signed_by=note.signed_by,
        created_at=note.created_at,
    )


async def read_note(s: AsyncSession, keycache: KeyCache, note: Note) -> NoteView:
    """Decrypted view of a note. Signed notes are rendered from ``signed_content_enc`` (record key)
    only, so they stay readable after the session DEK is destroyed (§8.4 "what survives")."""
    tenant, sess = await _note_context(s, note)
    rkey = record_key(keycache, tenant)
    if note.status == NoteStatus.signed.value and note.signed_content_enc is not None:
        doc = json.loads(
            Envelope.decrypt(rkey, note.signed_content_enc, signed_content_aad(note.tenant_id, note.id))
        )
        a = doc.get("assessment")
        assessment = (
            AssessmentView(
                text=a["text"],
                author_id=UUID(a["author_id"]),
                updated_at=datetime.fromisoformat(a["updated_at"]),
            )
            if a
            else None
        )
        return _base_view(note, sess, _signed_views(doc), assessment)
    assessment = _assessment_view(rkey, note, await notes_repo.get_assessment(s, note.id))
    rows = list(await notes_repo.list_statements(s, note.id))
    statements: list[StatementView] = []
    if rows:
        try:
            dek = session_dek(keycache, tenant, sess)
        except (
            DekDestroyedError
        ):  # unsigned draft of a purged session: purge deletes it; be explicit meanwhile
            raise NotFound("노트", NOTE_NOT_FOUND) from None
        statements = _statement_views(note, rows, dek, _draft_quotes(dek, note))
    return _base_view(note, sess, statements, assessment)


async def get_note(s: AsyncSession, note_id: UUID) -> Note:
    note = await notes_repo.get_note(s, note_id)
    if note is None:
        raise NotFound("노트", NOTE_NOT_FOUND)
    return note


async def latest_note(s: AsyncSession, session_id: UUID) -> Note:
    note = await notes_repo.latest_for_session(s, session_id)
    if note is None:
        raise NotFound("노트", NOTE_NOT_FOUND)
    return note


# ================================================================== clinician review


def _require_unsigned(note: Note) -> None:
    if note.status == NoteStatus.signed.value:
        raise NoteError(NOTE_ALREADY_SIGNED, 409, "이미 서명된 노트는 수정할 수 없습니다")
    if note.status == NoteStatus.abstained.value and note.statement_count == 0:
        raise NoteError(NOTE_NOT_REVIEWABLE, 409, "기권된 노트에는 검토할 문장이 없습니다")


async def decide_statement(
    s: AsyncSession,
    keycache: KeyCache,
    *,
    note: Note,
    statement_id: int,
    decision: Decision,
    edited_text: str | None,
    actor: TenantCtx,
    request_id: str | None,
) -> NoteStatement:
    """accept / edit / reject one statement; ``edit`` stores the clinician text under the session DEK."""
    _require_unsigned(note)
    tenant, sess = await _note_context(s, note)
    row = await s.get(NoteStatement, statement_id)
    if row is None or row.note_id != note.id:
        raise NotFound("문장", NOTE_NOT_FOUND)
    edited_enc = None
    if decision == "edit":
        if not edited_text or not edited_text.strip():
            raise NoteError(NOTE_EDIT_TEXT_REQUIRED, 422, "edit 결정에는 edited_text 가 필요합니다")
        dek = session_dek(keycache, tenant, sess)
        edited_enc = Envelope.encrypt(
            dek,
            edited_text.strip().encode("utf-8"),
            statement_aad(note.tenant_id, note.id, row.section.strip(), row.ordinal, edited=True),
        )
    updated = await notes_repo.set_decision(s, statement_id, decision=decision, edited_text_enc=edited_enc)
    assert updated is not None
    await audit.record(
        s,
        tenant_id=note.tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="note.decision",
        resource_type="note",
        resource_id=note.id,
        request_id=request_id,
        detail={"statement_id": statement_id, "decision": decision, "verdict": row.verdict},
    )
    return updated


async def put_assessment(
    s: AsyncSession,
    keycache: KeyCache,
    *,
    note: Note,
    text: str,
    actor: TenantCtx,
    now: datetime,
    request_id: str | None,
) -> NoteAssessment:
    """The only write path into ``note_assessments`` — clinician-authored, under the record key."""
    _require_unsigned(note)
    if not text.strip():
        raise NoteError(NOTE_EDIT_TEXT_REQUIRED, 422, "평가 내용이 비어 있습니다")
    if actor.user_id is None:
        raise NoteError(NOTE_NOT_REVIEWABLE, 409, "사용자 id 가 없는 토큰으로는 평가를 저장할 수 없습니다")
    tenant, _ = await _note_context(s, note)
    enc = Envelope.encrypt(
        record_key(keycache, tenant), text.strip().encode("utf-8"), assessment_aad(note.tenant_id, note.id)
    )
    row = await notes_repo.upsert_assessment(
        s, note_id=note.id, tenant_id=note.tenant_id, text_enc=enc, author_id=actor.user_id, now=now
    )
    await audit.record(
        s,
        tenant_id=note.tenant_id,
        actor_id=actor.user_id,
        actor_role=actor.role,
        action="note.assessment",
        resource_type="note",
        resource_id=note.id,
        request_id=request_id,
        detail={"chars": len(text.strip())},
    )
    return row


# ================================================================== signing (ADR-0004)


def build_signed_content(
    *, note: Note, sess: SessionModel, view: NoteView, clinician_id: UUID, signed_at: datetime
) -> dict[str, Any]:
    """The self-contained medical record: accepted/edited S/O/P statements with verbatim quotes,
    the assessment, ids and timestamps. Rejected statements are left out; an edited *unsupported*
    statement keeps only the evidence that actually matched (never a fabricated quote)."""
    sections: dict[str, list[dict[str, Any]]] = {"S": [], "O": [], "P": []}
    for st in view.statements:
        if st.decision == "reject":
            continue
        evidence = st.evidence
        if st.verdict != "supported":
            evidence = [e for e in evidence if e.start is not None and e.end is not None]
        sections[st.section].append(
            {
                "id": st.id,
                "ordinal": st.ordinal,
                "text": st.edited_text if st.decision == "edit" and st.edited_text else st.text,
                "original_text": st.text if st.decision == "edit" else None,
                "evidence": [
                    {"seq": e.seq, "quote": e.quote, "start": e.start, "end": e.end, "method": e.method}
                    for e in evidence
                ],
                "verdict": st.verdict,
                "verdict_reason": st.verdict_reason,
                "decision": st.decision or "accept",
                "edited_text": st.edited_text,
            }
        )
    assessment = view.assessment
    return {
        "schema": SIGNED_SCHEMA,
        "note_id": str(note.id),
        "version": note.version,
        "tenant_id": str(note.tenant_id),
        "session_id": str(note.session_id),
        "patient_id": str(sess.patient_id),
        "clinician_id": str(clinician_id),
        "signed_at": signed_at.isoformat(),
        "session": {
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        },
        "provider": note.provider,
        "model": note.model,
        "verifier_version": VERIFIER_VERSION,
        "coverage": view.coverage,
        "sections": sections,
        "assessment": None
        if assessment is None
        else {
            "text": assessment.text,
            "author_id": str(assessment.author_id),
            "updated_at": assessment.updated_at.isoformat(),
        },
        "purge_receipt_id": None,
    }


def retention_until(signed_at: datetime) -> datetime:
    """``signed_at + 10 years`` (leap-day safe: 365.25-day years are avoided; the calendar year is used)."""
    try:
        return signed_at.replace(year=signed_at.year + RETENTION_YEARS)
    except ValueError:  # 29 Feb
        return signed_at.replace(year=signed_at.year + RETENTION_YEARS, day=28) + timedelta(days=1)


async def sign(
    s: AsyncSession,
    keycache: KeyCache,
    *,
    note: Note,
    actor: TenantCtx,
    now: datetime,
    request_id: str | None,
) -> Note:
    """Sign a note: requires an assessment and a reject/edit decision on every unsupported statement.

    Sets ``status='signed'``, ``legal_hold='medical_record'``, ``retention_until = signed_at + 10 y``,
    writes ``signed_content_enc`` under the record key and moves the session to ``signed``.
    """
    if note.status == NoteStatus.signed.value:
        raise NoteError(NOTE_ALREADY_SIGNED, 409, "이미 서명된 노트입니다")
    if note.status == NoteStatus.abstained.value:
        raise NoteError(NOTE_NOT_REVIEWABLE, 409, "기권된 노트는 서명할 수 없습니다")
    if actor.user_id is None:
        raise NoteError(NOTE_NOT_REVIEWABLE, 409, "사용자 id 가 없는 토큰으로는 서명할 수 없습니다")
    tenant, sess = await _note_context(s, note)
    view = await read_note(s, keycache, note)
    if view.assessment is None:
        raise NoteError(NOTE_ASSESSMENT_MISSING, 409, "서명 전에 평가(Assessment)를 작성해야 합니다")
    pending = [
        st.id for st in view.statements if st.verdict != "supported" and st.decision not in ("reject", "edit")
    ]
    if pending:
        raise NoteError(
            NOTE_UNSUPPORTED_PENDING,
            409,
            f"근거가 확인되지 않은 문장 {len(pending)}개를 거부하거나 수정해야 서명할 수 있습니다",
        )
    doc = build_signed_content(note=note, sess=sess, view=view, clinician_id=actor.user_id, signed_at=now)
    blob = Envelope.encrypt(
        record_key(keycache, tenant),
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        signed_content_aad(note.tenant_id, note.id),
    )
    signed = await notes_repo.sign_note(
        s,
        note.id,
        signed_by=actor.user_id,
        signed_at=now,
        signed_content_enc=blob,
        retention_until=retention_until(now),
    )
    assert signed is not None
    await sessions_repo.set_state(s, sess.id, "signed", now=now)
    detail = {
        "version": note.version,
        "accepted": sum(len(v) for v in doc["sections"].values()),
        "rejected": sum(1 for st in view.statements if st.decision == "reject"),
        "retention_until": signed.retention_until.isoformat() if signed.retention_until else None,
        "content_sha256": hashlib.sha256(blob).hexdigest(),
    }
    for action, rtype, rid in (("note.signed", "note", note.id), ("session.signed", "session", sess.id)):
        await audit.record(
            s,
            tenant_id=note.tenant_id,
            actor_id=actor.user_id,
            actor_role=actor.role,
            action=action,
            resource_type=rtype,
            resource_id=rid,
            request_id=request_id,
            detail=detail,
        )
    return signed
