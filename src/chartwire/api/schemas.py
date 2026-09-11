"""Pydantic request/response models of the REST API (spec §3.2).

Every response model forbids extra fields so a router cannot leak a column by accident, and no
response model carries transcript text except the segment/search/timeline views that exist for it
(decrypted per request, under the caller's consent-gated session DEK).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chartwire.consent.scopes import SCOPE_ORDER, Scope


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ auth / users


class TokenRequest(_In):
    tenant_slug: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(_Out):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class MeOut(_Out):
    sub: str
    tenant_id: UUID
    role: str
    user_id: UUID | None
    exp: datetime


class UserCreate(_In):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    role: Literal["clinician", "staff", "admin", "auditor", "recorder"]
    display_name: str = Field(min_length=1, max_length=120)


class UserOut(_Out):
    id: UUID
    role: str
    email: str | None
    display_name: str
    is_active: bool
    created_at: datetime


# ------------------------------------------------------------------ patients / consents


class PatientCreate(_In):
    name: str = Field(min_length=1, max_length=120)
    birth_year: int | None = Field(default=None, ge=1900, le=2100)
    sex: Literal["F", "M", "X"] | None = None
    phone: str | None = Field(default=None, max_length=32)


class PatientOut(_Out):
    id: UUID
    pseudonym: str
    name: str | None
    birth_year: int | None
    sex: str | None
    consent_state: str
    is_synthetic: bool
    created_at: datetime


class ConsentGrant(_In):
    scopes: list[Scope] = Field(min_length=1, max_length=4)
    channel: str = Field(default="console", max_length=32)

    @field_validator("scopes")
    @classmethod
    def _dedupe_and_order(cls, scopes: list[Scope]) -> list[Scope]:
        wanted = set(scopes)
        return [s for s in SCOPE_ORDER if s in wanted]


class ConsentRevoke(_In):
    reason: str | None = Field(default=None, max_length=200)


class ConsentOut(_Out):
    id: UUID
    patient_id: UUID
    version: int
    scopes: list[str]
    granted_at: datetime
    granted_by: UUID | None
    channel: str
    revoked_at: datetime | None
    revoked_by: UUID | None
    revoked_reason: str | None


class ConsentRevokedOut(_Out):
    consent_id: UUID
    patient_id: UUID
    revoked_at: datetime
    purge_job_id: UUID
    outbox_event_id: int | None
    live_sessions_notified: int


# ------------------------------------------------------------------ consultations (public lead form)

_PHONE_RE = re.compile(r"^[0-9+\- ]+$")
CONSULTATION_ROLES = Literal["director", "manager", "staff", "other"]


class ConsultationIn(_In):
    """데모 홈페이지 "서비스 상담신청하기" 본문. 이름·전화·이메일은 저장만 하고 어떤 로그에도 남기지 않는다
    (``tests/integration/test_api_consultations.py``). 실제 연락은 가지 않는 데모 접수함이다.

    ``source`` 만 접수 로그에 실리므로 슬러그(``^[a-z][a-z0-9-]*$``)로 닫아 둔다 — 자유 문자열이면 호출자가
    이메일이나 전화번호를 로그 줄에 밀어 넣을 수 있고, PHI 리댁터는 한국식 전화·주민번호 패턴만 가린다.
    숫자로 시작할 수 없게 한 것도 그 때문이다: ``[a-z0-9-]`` 만으로는 ``010-1234-5678`` 이 통과한다."""

    clinic_name: str = Field(min_length=1, max_length=120)
    contact_name: str = Field(min_length=1, max_length=60)
    phone: str = Field(min_length=5, max_length=32)
    email: str = Field(min_length=3, max_length=254)
    role: CONSULTATION_ROLES | None = None
    message: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="console-home", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    agree_privacy: bool

    @field_validator("clinic_name", "contact_name")
    @classmethod
    def _stripped_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("공백만으로는 채울 수 없습니다")
        return value

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        """공백 연속을 하나로 접고, 숫자·``+``·``-``·공백 이외의 문자는 거절한다 (정규화 뒤 5~32자, 숫자 5개 이상)."""
        value = " ".join(value.split())
        if not _PHONE_RE.fullmatch(value):
            raise ValueError("전화번호는 숫자, +, -, 공백만 쓸 수 있습니다")
        if not 5 <= len(value) <= 32 or sum(ch.isdigit() for ch in value) < 5:
            raise ValueError("전화번호 형식이 올바르지 않습니다")
        return value

    @field_validator("email")
    @classmethod
    def _simple_email(cls, value: str) -> str:
        """``local@domain`` 모양만 본다 — 실제 발송이 없는 데모라 RFC 5322 검증기는 두지 않는다."""
        value = value.strip()
        local, at, domain = value.partition("@")
        if not at or not local or not domain or any(ch.isspace() for ch in value):
            raise ValueError("이메일 형식이 올바르지 않습니다")
        return value

    @field_validator("agree_privacy")
    @classmethod
    def _must_agree(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("개인정보 수집·이용에 동의해야 접수할 수 있습니다")
        return value


class ConsultationOut(_Out):
    id: UUID
    received_at: datetime
    message: str


# ------------------------------------------------------------------ sessions


class SessionCreate(_In):
    patient_id: UUID
    clinician_id: UUID | None = None
    script_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class SessionOut(_Out):
    id: UUID
    state: str
    patient_id: UUID
    clinician_id: UUID
    script_ref: str | None
    started_at: datetime | None
    ended_at: datetime | None
    ack_seq: int
    final_seq: int | None
    epoch: int
    scopes_snapshot: list[str]
    created_at: datetime


class SessionList(_Out):
    items: list[SessionOut]
    next_before: datetime | None


class WsTicketRequest(_In):
    kind: Literal["ingest", "watch"]


class WsTicketOut(_Out):
    ticket: str
    expires_in: int


class SegmentOut(_Out):
    seq: int
    speaker: str
    t_start_ms: int
    t_end_ms: int
    text: str
    confidence: float | None


class TimelineSegmentOut(SegmentOut):
    session_id: UUID
    segment_id: int
    created_at: datetime


class TimelineOut(_Out):
    items: list[TimelineSegmentOut]
    next_before: str | None


class SearchHit(_Out):
    session_id: UUID
    segment_id: int
    seq: int | None
    speaker: str
    snippet: str
    created_at: datetime


# ------------------------------------------------------------------ alerts


class AlertOut(_Out):
    id: int
    session_id: UUID
    patient_id: UUID
    segment_seq: int
    category: str
    severity: int
    span: list[int]
    scope: dict[str, Any]
    detector_version: str
    detected_at: datetime
    sla_deadline_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: UUID | None
    escalation_level: int
    escalated_at: datetime | None


# ------------------------------------------------------------------ purge


class PurgeJobCreate(_In):
    subject_type: Literal["session", "patient"]
    subject_id: UUID


class PurgeReceiptOut(_Out):
    id: UUID
    tenant_id: UUID
    subject_type: str
    subject_id: UUID
    reason: str
    state: str
    requested_by: UUID | None
    requested_at: datetime
    completed_at: datetime | None
    verified_at: datetime | None
    steps: list[dict[str, Any]]
    counts: dict[str, Any]
    dek_fingerprints: list[str]
    receipt_hash: str | None
    receipt_hash_valid: bool | None
    verify_result: dict[str, Any] | None
    artifact: str = "파기 영수증 (purge receipt)"


class VerifyDecryptOut(_Out):
    decrypt_attempted: bool
    unwrap: str
    decrypt_sample: str
    job_state: str


# ------------------------------------------------------------------ audit / ops


class AuditOut(_Out):
    id: int
    at: datetime
    actor_id: UUID | None
    actor_role: str | None
    action: str
    resource_type: str
    resource_id: str | None
    request_id: str | None
    detail: dict[str, Any]


class AuditList(_Out):
    items: list[AuditOut]
    next_before: int | None


class OutboxStatsOut(_Out):
    pending: int
    in_flight: int
    done: int
    dead: int
    lag_seconds: float


class DeadLetterOut(_Out):
    id: int
    outbox_event_id: int
    event_type: str
    attempts: int
    last_error: str | None
    died_at: datetime
    replayed_at: datetime | None


class ReplayOut(_Out):
    outbox_event_id: int
    replayed: bool


class PartitionOut(_Out):
    name: str
    bounds: str | None
    is_default: bool


class Problem(_Out):
    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None
    retryable: bool = False
