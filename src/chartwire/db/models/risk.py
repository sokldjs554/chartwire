"""``risk_events`` — deterministic detector hits with the SLA/ack/escalation lifecycle."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, SmallInteger, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, bigint_identity, created_now, jsonb_obj, tenant_fk, timestamptz

RISK_CATEGORIES: tuple[str, ...] = ("suicidal_ideation", "self_harm", "harm_to_others", "substance_acute")


class RiskEvent(Base):
    __tablename__ = "risk_events"
    __table_args__ = (
        CheckConstraint(
            "category IN ('suicidal_ideation','self_harm','harm_to_others','substance_acute')",
            name="category",
        ),
        CheckConstraint("severity BETWEEN 1 AND 3", name="severity"),
        Index("ix_risk_session", "session_id", "detected_at"),
        Index("ix_risk_tenant_time", "tenant_id", text("detected_at DESC")),
    )

    id: Mapped[bigint_identity]
    tenant_id: Mapped[tenant_fk]
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    patient_id: Mapped[UUID]
    segment_id: Mapped[int] = mapped_column(BigInteger)  # soft reference (partitioned PK)
    segment_created_at: Mapped[timestamptz]
    segment_seq: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    severity: Mapped[int] = mapped_column(SmallInteger)
    phrase: Mapped[str] = mapped_column(Text)
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    scope: Mapped[jsonb_obj]
    detector_version: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[created_now]
    sla_deadline_at: Mapped[timestamptz | None]
    acknowledged_at: Mapped[timestamptz | None]
    acknowledged_by: Mapped[UUID | None]
    escalated_at: Mapped[timestamptz | None]
    escalation_level: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
