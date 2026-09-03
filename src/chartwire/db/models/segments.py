"""``transcript_segments`` (range-partitioned by ``created_at``) and the consent-gated
plaintext ``segment_search`` table used only by the SECURITY DEFINER search function."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, bytea, created_now, tenant_fk, timestamptz

SPEAKERS: tuple[str, ...] = ("clinician", "patient", "unknown")


class TranscriptSegment(Base):
    """Partitioned parent. PG16 allows no identity column on partitioned tables, hence the
    explicit sequence; the UNIQUE key includes the partition key (the real idempotency key)."""

    __tablename__ = "transcript_segments"
    __table_args__ = (
        PrimaryKeyConstraint("created_at", "id"),
        UniqueConstraint("session_id", "seq", "created_at"),
        CheckConstraint("speaker IN ('clinician','patient','unknown')", name="speaker"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, server_default=sa_text("nextval('transcript_segments_id_seq')")
    )
    tenant_id: Mapped[tenant_fk]
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    patient_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(Text)
    t_start_ms: Mapped[int] = mapped_column(Integer)
    t_end_ms: Mapped[int] = mapped_column(Integer)
    text_enc: Mapped[bytea]
    text_len: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float(precision=24))  # real
    provider: Mapped[str] = mapped_column(Text)
    created_at: Mapped[timestamptz]  # = sessions.started_at + t_start_ms (deterministic)


class SegmentSearch(Base):
    __tablename__ = "segment_search"
    __table_args__ = (
        Index("ix_search_session", "tenant_id", "session_id"),
        Index("ix_search_patient", "tenant_id", "patient_id", sa_text("segment_created_at DESC")),
    )

    segment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    segment_created_at: Mapped[timestamptz]
    tenant_id: Mapped[tenant_fk]
    session_id: Mapped[UUID]
    patient_id: Mapped[UUID]
    speaker: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    terms: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"))
    created_at: Mapped[created_now]
