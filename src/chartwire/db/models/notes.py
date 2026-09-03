"""``notes`` / ``note_statements`` / ``note_assessments``.

There is deliberately no diagnosis or verdict column written by any provider: the
assessment table is clinician-authored only (spec §0.5, §9).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import (
    Base,
    bigint_identity,
    bytea,
    created_now,
    tenant_fk,
    timestamptz,
    uuid_pk_client,
)

NOTE_STATUSES: tuple[str, ...] = ("drafting", "needs_review", "verified", "abstained", "signed", "rejected")


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("session_id", "version"),
        CheckConstraint(
            "status IN ('drafting','needs_review','verified','abstained','signed','rejected')", name="status"
        ),
        CheckConstraint("legal_hold IN ('medical_record')", name="legal_hold"),
    )

    id: Mapped[uuid_pk_client]
    tenant_id: Mapped[tenant_fk]
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[bytea | None]
    grounding_coverage: Mapped[float | None] = mapped_column(Numeric(5, 4))
    statement_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    unsupported_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    abstain_reason: Mapped[str | None] = mapped_column(Text)
    raw_draft_enc: Mapped[bytea | None]  # provider output under the session DEK
    signed_content_enc: Mapped[bytea | None]  # self-contained record under the tenant record key
    legal_hold: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[timestamptz | None]
    signed_by: Mapped[UUID | None]
    signed_at: Mapped[timestamptz | None]
    created_at: Mapped[created_now]


class NoteStatement(Base):
    __tablename__ = "note_statements"
    __table_args__ = (
        CheckConstraint("section IN ('S','O','P')", name="section"),
        CheckConstraint("verdict IN ('supported','unsupported')", name="verdict"),
        CheckConstraint("clinician_decision IN ('accept','edit','reject')", name="clinician_decision"),
        Index("ix_note_statements", "note_id", "section", "ordinal"),
    )

    id: Mapped[bigint_identity]
    tenant_id: Mapped[tenant_fk]
    note_id: Mapped[UUID] = mapped_column(ForeignKey("notes.id"))
    section: Mapped[str] = mapped_column(CHAR(1))
    ordinal: Mapped[int] = mapped_column(Integer)
    text_enc: Mapped[bytea]
    evidence: Mapped[list] = mapped_column(JSONB)  # [{"seq","quote_hash","start","end","method"}]
    verdict: Mapped[str] = mapped_column(Text)
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    clinician_decision: Mapped[str | None] = mapped_column(Text)
    edited_text_enc: Mapped[bytea | None]


class NoteAssessment(Base):
    __tablename__ = "note_assessments"

    note_id: Mapped[UUID] = mapped_column(ForeignKey("notes.id"), primary_key=True)
    tenant_id: Mapped[tenant_fk]
    text_enc: Mapped[bytea]
    author_id: Mapped[UUID]
    created_at: Mapped[created_now]
    updated_at: Mapped[created_now]
