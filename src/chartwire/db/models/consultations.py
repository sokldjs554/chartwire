"""``consultation_requests`` — 데모 홈페이지의 "서비스 상담신청" 접수함 (플랫폼 레벨 리드, ``tenants`` 처럼 RLS 밖).

``tenant_id`` 가 없다: 신청자는 아직 어떤 의원(테넌트)에도 속하지 않는다. 같은 이유로 조회 REST 는 두지
않는다 — 어느 테넌트의 admin 이 읽어도 테넌트 밖 데이터가 된다. IP·User-Agent 는 저장하지 않는다
(개인정보 최소화, §0.9).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from chartwire.db.base import Base, created_now, uuid_pk

CONSULTATION_ROLES: tuple[str, ...] = ("director", "manager", "staff", "other")


class ConsultationRequest(Base):
    __tablename__ = "consultation_requests"
    __table_args__ = (CheckConstraint("role IN ('director','manager','staff','other')", name="role"),)

    id: Mapped[uuid_pk]
    clinic_name: Mapped[str] = mapped_column(Text)
    contact_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default=text("'console-home'"))
    created_at: Mapped[created_now]
