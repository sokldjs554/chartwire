"""Notes REST (spec §6.9): latest/get, manual draft, statement decision, assessment, sign.

Role rules are enforced twice on purpose — here by ``rbac.require`` and in PostgreSQL by the
``role_gate`` RLS policy on the notes tables (staff/admin get zero rows even if a router forgot).
The transaction runs under the caller's ``TenantCtx`` (tenant + user + role GUCs), so "clinician
(own)" is a row check on ``sessions.clinician_id`` and an auditor gets metadata only: every field
that carries transcript-derived text (``text``, ``edited_text``, evidence ``quote``, ``assessment``)
is stripped before the response is built.

Errors are ``core.errors.AppError`` (problem+json); :func:`install_error_handlers` registers the
renderer on an app that does not have one yet (the test app; ``create_app`` may bring its own).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import get_deps
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.core.errors import AppError, Forbidden, NotFound
from chartwire.db.models import Note
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.notes import service
from chartwire.notes.service import NoteView
from chartwire.redis import keys

router = APIRouter(prefix="/v1", tags=["notes"])

PROBLEM_MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "x-request-id"


# ------------------------------------------------------------------ schemas (§3.2)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceOut(_Out):
    seq: int
    quote: str | None
    start: int | None
    end: int | None
    method: str | None


class StatementOut(_Out):
    id: int
    section: str
    ordinal: int
    text: str | None
    evidence: list[EvidenceOut]
    verdict: str
    verdict_reason: str | None
    decision: str | None
    edited_text: str | None


class AssessmentOut(_Out):
    text: str | None
    author_id: UUID
    updated_at: datetime


class NoteOut(_Out):
    id: UUID
    session_id: UUID
    version: int
    status: str
    provider: str
    model: str | None
    coverage: float | None
    statement_count: int
    unsupported_count: int
    abstain_reason: str | None
    statements: list[StatementOut]
    assessment: AssessmentOut | None
    legal_hold: str | None
    retention_until: datetime | None
    signed_at: datetime | None
    signed_by: UUID | None
    created_at: datetime


class StatementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "edit", "reject"]
    edited_text: str | None = Field(default=None, max_length=400)


class AssessmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class DraftQueued(_Out):
    session_id: UUID
    event_id: int | None
    queued: bool


def note_out(view: NoteView, *, redact: bool) -> NoteOut:
    """``redact=True`` (auditor): metadata only — no statement text, edited text, quotes or assessment."""
    statements = [
        StatementOut(
            id=st.id,
            section=st.section,
            ordinal=st.ordinal,
            text=None if redact else st.text,
            evidence=[
                EvidenceOut(
                    seq=e.seq, quote=None if redact else e.quote, start=e.start, end=e.end, method=e.method
                )
                for e in st.evidence
            ],
            verdict=st.verdict,
            verdict_reason=st.verdict_reason,
            decision=st.decision,
            edited_text=None if redact else st.edited_text,
        )
        for st in view.statements
    ]
    assessment = None
    if view.assessment is not None:
        assessment = AssessmentOut(
            text=None if redact else view.assessment.text,
            author_id=view.assessment.author_id,
            updated_at=view.assessment.updated_at,
        )
    return NoteOut(
        id=view.id,
        session_id=view.session_id,
        version=view.version,
        status=view.status,
        provider=view.provider,
        model=view.model,
        coverage=view.coverage,
        statement_count=view.statement_count,
        unsupported_count=view.unsupported_count,
        abstain_reason=view.abstain_reason,
        statements=statements,
        assessment=assessment,
        legal_hold=view.legal_hold,
        retention_until=view.retention_until,
        signed_at=view.signed_at,
        signed_by=view.signed_by,
        created_at=view.created_at,
    )


# ------------------------------------------------------------------ plumbing


def problem_response(exc: AppError, request_id: str | None) -> JSONResponse:
    return JSONResponse(exc.to_problem(request_id), status_code=exc.status, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    """Render :class:`AppError` as RFC 9457 problem+json (idempotent; skips an app that already has one)."""
    if AppError in app.exception_handlers:
        return

    async def _handler(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, AppError)
        return problem_response(exc, request.headers.get(REQUEST_ID_HEADER))

    app.add_exception_handler(AppError, _handler)


def _request_id(request: Request) -> str | None:
    return request.headers.get(REQUEST_ID_HEADER)


def _ctx(principal: Principal) -> TenantCtx:
    return TenantCtx(tenant_id=principal.tenant_id, user_id=principal.user_id, role=principal.role)


@asynccontextmanager
async def _tx(request: Request, principal: Principal) -> AsyncIterator[tuple[Any, AsyncSession]]:
    deps = get_deps(request)
    async with tenant_tx(deps.engine, _ctx(principal)) as s:
        yield deps, s


def _check_owner(view_clinician_id: UUID, principal: Principal) -> None:
    """ "clinician (own)": a clinician sees only sessions they conducted; an auditor sees the tenant."""
    if principal.role == "clinician" and view_clinician_id != principal.user_id:
        raise Forbidden("본인이 진행한 세션의 노트만 볼 수 있습니다")


async def _read(
    s: AsyncSession, deps: Any, note: Note, principal: Principal, request_id: str | None
) -> NoteOut:
    view = await service.read_note(s, deps.keycache, note)
    _check_owner(view.clinician_id, principal)
    redact = principal.role == "auditor"
    await audit.record(
        s,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        actor_role=principal.role,
        action="note.read",
        resource_type="note",
        resource_id=note.id,
        request_id=request_id,
        detail={"version": note.version, "status": note.status, "redacted": redact},
    )
    return note_out(view, redact=redact)


async def _owned_note(s: AsyncSession, note_id: UUID, principal: Principal) -> Note:
    note = await service.get_note(s, note_id)
    sess = await sessions_repo.get_session(s, note.session_id)
    if sess is None:
        raise NotFound("세션", service.NOTE_NOT_FOUND)
    _check_owner(sess.clinician_id, principal)
    return note


# ------------------------------------------------------------------ routes


@router.get("/sessions/{id}/notes/latest", response_model=NoteOut)
async def get_latest_note(
    id: UUID, request: Request, principal: Principal = Depends(require("clinician", "auditor"))
) -> NoteOut:
    async with _tx(request, principal) as (deps, s):
        note = await service.latest_note(s, id)
        return await _read(s, deps, note, principal, _request_id(request))


@router.get("/notes/{id}", response_model=NoteOut)
async def get_note(
    id: UUID, request: Request, principal: Principal = Depends(require("clinician", "auditor"))
) -> NoteOut:
    async with _tx(request, principal) as (deps, s):
        note = await service.get_note(s, id)
        return await _read(s, deps, note, principal, _request_id(request))


@router.post("/sessions/{id}/notes/draft", response_model=DraftQueued, status_code=status.HTTP_202_ACCEPTED)
async def request_draft(
    id: UUID, request: Request, principal: Principal = Depends(require("clinician"))
) -> DraftQueued:
    """Enqueue drafting through the outbox (the worker's ``note_draft`` handler does the work)."""
    async with _tx(request, principal) as (deps, s):
        sess = await sessions_repo.get_session(s, id)
        if sess is None:
            raise NotFound("세션", service.NOTE_NOT_FOUND)
        _check_owner(sess.clinician_id, principal)
        if sess.state in ("purging", "purged"):
            raise service.NoteError(service.NOTE_NOT_REVIEWABLE, 409, "파기된 세션은 초안을 만들 수 없습니다")
        event_id = await service.request_draft(
            s,
            tenant_id=principal.tenant_id,
            sess=sess,
            now=deps.clock.now(),
            actor=_ctx(principal),
            request_id=_request_id(request),
        )
    redis = getattr(deps, "redis", None)
    if redis is not None:
        with contextlib.suppress(Exception):  # the poller ticks every second regardless (§7.1)
            await redis.publish(keys.OUTBOX_WAKE, "1")
    return DraftQueued(session_id=id, event_id=event_id, queued=event_id is not None)


@router.post("/notes/{id}/statements/{sid}/decision", response_model=NoteOut)
async def decide_statement(
    id: UUID,
    sid: int,
    body: StatementDecision,
    request: Request,
    principal: Principal = Depends(require("clinician")),
) -> NoteOut:
    async with _tx(request, principal) as (deps, s):
        note = await _owned_note(s, id, principal)
        await service.decide_statement(
            s,
            deps.keycache,
            note=note,
            statement_id=sid,
            decision=body.decision,
            edited_text=body.edited_text,
            actor=_ctx(principal),
            request_id=_request_id(request),
        )
        view = await service.read_note(s, deps.keycache, note)
        return note_out(view, redact=False)


@router.put("/notes/{id}/assessment", response_model=NoteOut)
async def put_assessment(
    id: UUID, body: AssessmentIn, request: Request, principal: Principal = Depends(require("clinician"))
) -> NoteOut:
    """The only write path into ``note_assessments`` (encrypted under the tenant record key)."""
    async with _tx(request, principal) as (deps, s):
        note = await _owned_note(s, id, principal)
        await service.put_assessment(
            s,
            deps.keycache,
            note=note,
            text=body.text,
            actor=_ctx(principal),
            now=deps.clock.now(),
            request_id=_request_id(request),
        )
        view = await service.read_note(s, deps.keycache, note)
        return note_out(view, redact=False)


@router.post("/notes/{id}/sign", response_model=NoteOut)
async def sign_note(
    id: UUID, request: Request, principal: Principal = Depends(require("clinician"))
) -> NoteOut:
    async with _tx(request, principal) as (deps, s):
        note = await _owned_note(s, id, principal)
        signed = await service.sign(
            s,
            deps.keycache,
            note=note,
            actor=_ctx(principal),
            now=deps.clock.now(),
            request_id=_request_id(request),
        )
        view = await service.read_note(s, deps.keycache, signed)
        return note_out(view, redact=False)
