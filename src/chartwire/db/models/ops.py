"""Operational tables: transactional outbox, idempotency ledger, DLQ, append-only audit, purge jobs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import (
    Base,
    bigint_identity,
    bytea,
    created_now,
    jsonb_list,
    jsonb_obj,
    tenant_fk,
    timestamptz,
    uuid_pk_client,
)

OUTBOX_STATUSES: tuple[str, ...] = ("pending", "in_flight", "done", "dead")
PURGE_STATES: tuple[str, ...] = ("queued", "running", "completed", "verified", "failed")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending','in_flight','done','dead')", name="status"),
        Index("ix_outbox_aggregate", "aggregate_id"),
    )

    id: Mapped[bigint_identity]
    tenant_id: Mapped[tenant_fk]
    aggregate_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[created_now]
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[timestamptz | None]
    lease_until: Mapped[timestamptz | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_now]
    done_at: Mapped[timestamptz | None]


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    handler: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[tenant_fk]
    processed_at: Mapped[created_now]


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[bigint_identity]
    tenant_id: Mapped[tenant_fk]
    outbox_event_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    died_at: Mapped[created_now]
    replayed_at: Mapped[timestamptz | None]


class AuditEvent(Base):
    """Append-only (trigger + INSERT/SELECT-only grants). ``detail`` holds ids/counts/hashes, never PHI."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", text("at DESC")),)

    id: Mapped[bigint_identity]
    tenant_id: Mapped[tenant_fk]
    at: Mapped[created_now]
    actor_id: Mapped[UUID | None]
    actor_role: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[jsonb_obj]


class PurgeJob(Base):
    __tablename__ = "purge_jobs"
    __table_args__ = (
        CheckConstraint("subject_type IN ('session','patient')", name="subject_type"),
        CheckConstraint("reason IN ('consent_revoked','admin','retention')", name="reason"),
        CheckConstraint("state IN ('queued','running','completed','verified','failed')", name="state"),
    )

    id: Mapped[uuid_pk_client]
    tenant_id: Mapped[tenant_fk]
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[UUID]
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[UUID | None]
    requested_at: Mapped[created_now]
    state: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    steps: Mapped[jsonb_list]
    counts: Mapped[jsonb_obj]
    dek_fingerprints: Mapped[jsonb_list]
    sample_ciphertext: Mapped[bytea | None]
    receipt_hash: Mapped[bytea | None]
    completed_at: Mapped[timestamptz | None]
    verified_at: Mapped[timestamptz | None]
    verify_result: Mapped[dict | None] = mapped_column(JSONB)
