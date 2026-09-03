"""``tenants`` (no RLS, app SELECT only) and ``users``."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, bytea, created_now, jsonb_obj, tenant_fk, uuid_pk


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (CheckConstraint("status IN ('active','suspended')", name="status"),)

    id: Mapped[uuid_pk]
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    kek_ref: Mapped[str] = mapped_column(Text)
    record_key_wrapped: Mapped[bytea]  # record DEK for signed notes; never destroyed
    settings: Mapped[jsonb_obj]
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[created_now]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email_hmac"),
        CheckConstraint("role IN ('clinician','staff','admin','auditor','recorder')", name="role"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[tenant_fk] = mapped_column(ForeignKey("tenants.id"))
    role: Mapped[str] = mapped_column(Text)
    email_hmac: Mapped[bytea]
    email_enc: Mapped[bytea]
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[created_now]
