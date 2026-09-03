"""``sessions`` (state machine + ack ledger head), ``audio_chunks`` (ledger) and ``stt_offsets``."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, bytea, created_now, tenant_fk, timestamptz, uuid_pk_client

SESSION_STATES: tuple[str, ...] = (
    "created",
    "recording",
    "paused",
    "ended",
    "transcribed",
    "drafted",
    "signed",
    "purging",
    "purged",
)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("state IN (" + ",".join(f"'{s}'" for s in SESSION_STATES) + ")", name="state"),
        Index("ix_sessions_clinician", "tenant_id", "clinician_id", text("created_at DESC")),
        Index("ix_sessions_patient", "tenant_id", "patient_id", text("created_at DESC")),
        Index("ix_sessions_live", "tenant_id", postgresql_where=text("state IN ('recording','paused')")),
    )

    id: Mapped[uuid_pk_client]
    tenant_id: Mapped[tenant_fk] = mapped_column(ForeignKey("tenants.id"))
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"))
    clinician_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'created'"))
    script_ref: Mapped[str | None] = mapped_column(Text)
    codec: Mapped[str] = mapped_column(Text, server_default=text("'pcm16le'"))
    sample_rate: Mapped[int] = mapped_column(Integer, server_default=text("16000"))
    chunk_ms: Mapped[int] = mapped_column(Integer, server_default=text("200"))
    stt_provider: Mapped[str] = mapped_column(Text, server_default=text("'simulator'"))
    scopes_snapshot: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    dek_wrapped: Mapped[bytea | None]
    dek_fingerprint: Mapped[bytea | None]
    dek_destroyed_at: Mapped[timestamptz | None]
    ack_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    final_seq: Mapped[int | None] = mapped_column(BigInteger)
    epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[timestamptz | None]
    ended_at: Mapped[timestamptz | None]
    transcribed_at: Mapped[timestamptz | None]
    signed_at: Mapped[timestamptz | None]
    purged_at: Mapped[timestamptz | None]
    created_at: Mapped[created_now]
    updated_at: Mapped[created_now]


class AudioChunk(Base):
    """Ledger row per chunk; audio bytes live in the object store under ``storage_key``."""

    __tablename__ = "audio_chunks"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[tenant_fk]
    byte_len: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[bytea]
    storage_key: Mapped[str] = mapped_column(Text)
    offset_ms: Mapped[int] = mapped_column(Integer)
    flags: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    received_at: Mapped[timestamptz]


class SttOffset(Base):
    __tablename__ = "stt_offsets"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    tenant_id: Mapped[tenant_fk]
    last_chunk_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_segment_seq: Mapped[int] = mapped_column(Integer, server_default=text("-1"))
    updated_at: Mapped[created_now]
