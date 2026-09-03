"""``patients`` (identifiers encrypted under the patient DEK) and versioned ``consents``."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, bytea, created_now, tenant_fk, timestamptz, uuid_pk

CONSENT_SCOPES: tuple[str, ...] = ("recording", "transcription", "ai_drafting", "search_index")


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "pseudonym"),
        CheckConstraint("consent_state IN ('none','granted','revoked','purged')", name="consent_state"),
        Index("ix_patients_name_hmac", "tenant_id", "name_hmac"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[tenant_fk] = mapped_column(ForeignKey("tenants.id"))
    pseudonym: Mapped[str] = mapped_column(Text)
    name_enc: Mapped[bytea | None]
    name_hmac: Mapped[bytea | None]
    birth_year: Mapped[int | None] = mapped_column(Integer)
    sex: Mapped[str | None] = mapped_column(CHAR(1))
    phone_enc: Mapped[bytea | None]
    dek_wrapped: Mapped[bytea | None]
    dek_fingerprint: Mapped[bytea | None]
    dek_destroyed_at: Mapped[timestamptz | None]
    consent_state: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[created_now]
    purged_at: Mapped[timestamptz | None]


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (
        UniqueConstraint("patient_id", "version"),
        CheckConstraint(
            "scopes <@ ARRAY['recording','transcription','ai_drafting','search_index']", name="scopes"
        ),
        Index("ix_consents_active", "tenant_id", "patient_id", postgresql_where=text("revoked_at IS NULL")),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[tenant_fk]
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    version: Mapped[int] = mapped_column(Integer)
    granted_at: Mapped[created_now]
    granted_by: Mapped[UUID | None]
    channel: Mapped[str] = mapped_column(Text, server_default=text("'console'"))
    policy_hash: Mapped[bytea | None]
    revoked_at: Mapped[timestamptz | None]
    revoked_by: Mapped[UUID | None]
    revoked_reason: Mapped[str | None] = mapped_column(Text)
