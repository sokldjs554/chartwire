"""``POST /v1/consultations`` (데모 홈페이지 상담신청, 인증 없음) over real PostgreSQL/Redis.

접수가 ``consultation_requests`` 에 저장되고(202), 동의 없이는 422, 클라이언트 주소당 시간당 10건을 넘으면
429 ``CW-4291`` + ``Retry-After``, 그리고 §0.9 대로 이름·전화·이메일·의원명이 **어떤 로그에도** 남지 않는다.
역할별 접근(익명 허용, 조회 라우트 부재)은 ``test_rbac_matrix.py`` 가 ``rbac.MATRIX`` 로 자동 커버한다.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from chartwire.core.logging import redact_dict
from chartwire.db.cli import purge_consultations
from chartwire.db.engine import as_sync_url, make_sync_engine, with_database
from chartwire.db.models import ConsultationRequest
from chartwire.objectstore.localfs import LocalFs
from chartwire.redis import ratelimit
from tests.integration.api_support import build_app, client, make_deps

pytestmark = pytest.mark.integration

CLINIC = "가상의원-상담검사"
NAME = "가상원장 로그검사"
PHONE = "010-9876-1234"
EMAIL = "lead-9f3a7c@example.test"
BODY = {
    "clinic_name": CLINIC,
    "contact_name": NAME,
    "phone": PHONE,
    "email": EMAIL,
    "role": "director",
    "message": "데모 상담 문의 본문",
    "agree_privacy": True,
}


@pytest.fixture
def api_app(app_engine, redis, settings, tmp_path):
    """소켓 주소를 바꿔 가며 여러 클라이언트를 붙이려면 앱 자체가 필요하다 (레이트리밋 테스트)."""
    return build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))


@pytest.fixture
async def api(api_app):
    async with client(api_app) as c:
        yield c


async def stored(engine, consultation_id: UUID) -> ConsultationRequest | None:
    async with AsyncSession(engine) as s:  # RLS 밖: 테넌트 GUC 없이 읽힌다
        return await s.scalar(select(ConsultationRequest).where(ConsultationRequest.id == consultation_id))


def _record_text(record: logging.LogRecord) -> str:
    """Everything a handler could render: message, args and every ``extra`` attribute."""
    return (
        json.dumps({k: repr(v) for k, v in record.__dict__.items()}, ensure_ascii=False) + record.getMessage()
    )


async def test_accepted_and_stored_without_tenant_ip_or_user_agent(api, app_engine):
    r = await api.post(
        "/v1/consultations",
        json=BODY,
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1", "User-Agent": "ua-x"},
    )
    assert r.status_code == 202, r.text
    out = r.json()
    assert set(out) == {"id", "received_at", "message"} and "데모" in out["message"]
    row = await stored(app_engine, UUID(out["id"]))
    assert row is not None
    assert (row.clinic_name, row.contact_name, row.phone, row.email) == (CLINIC, NAME, PHONE, EMAIL)
    assert row.role == "director" and row.source == "console-home" and row.message == BODY["message"]
    assert row.created_at == datetime.fromisoformat(out["received_at"])
    columns = {c.name for c in ConsultationRequest.__table__.columns}
    assert not columns & {"tenant_id", "ip", "client_ip", "user_agent"}


async def test_without_privacy_agreement_is_422_problem_and_nothing_is_stored(api, app_engine):
    email = (
        f"reject-{uuid4().hex[:8]}@example.test"  # 테이블은 테스트 사이에 비우지 않는다: 이 테스트만의 주소
    )
    r = await api.post("/v1/consultations", json={**BODY, "email": email, "agree_privacy": False})
    assert r.status_code == 422 and r.json()["code"] == "CW-4220"
    assert r.headers["content-type"].startswith("application/problem+json")
    assert NAME not in r.text and PHONE not in r.text  # 검증 오류도 입력값을 되돌려주지 않는다
    assert ["body", "agree_privacy"] in [e["loc"] for e in r.json()["errors"]]
    unknown = await api.post("/v1/consultations", json={**BODY, "email": email, "ip": "203.0.113.7"})
    assert unknown.status_code == 422
    async with AsyncSession(app_engine) as s:
        assert await s.scalar(select(ConsultationRequest).where(ConsultationRequest.email == email)) is None


async def test_429_per_socket_address_and_xff_cannot_reset_it(api_app, redis):
    """버킷은 **소켓 주소**로 갈린다. ``X-Forwarded-For`` 를 바꿔도 같은 소켓이면 계속 429 다."""
    async with client(api_app, address="198.51.100.9") as caller:
        statuses = [
            (await caller.post("/v1/consultations", json=BODY)).status_code
            for _ in range(ratelimit.CONSULTATION_PER_HOUR)
        ]
        assert set(statuses) == {202}
        blocked = await caller.post("/v1/consultations", json=BODY)
        assert blocked.status_code == 429, blocked.text
        problem = blocked.json()
        assert problem["code"] == "CW-4291" and problem["retryable"] is True
        assert blocked.headers["content-type"].startswith("application/problem+json")
        assert 1 <= int(blocked.headers["retry-after"]) <= ratelimit.WINDOW_HOUR

        # 헤더를 돌려 가며 던져도 뚫리지 않는다 — 라우터는 XFF 를 읽지 않는다.
        spoofed = [
            (
                await caller.post(
                    "/v1/consultations", json=BODY, headers={"X-Forwarded-For": f"203.0.113.{i}"}
                )
            ).status_code
            for i in range(1, 6)
        ]
        assert spoofed == [429] * 5, "X-Forwarded-For 로 버킷을 바꿀 수 있으면 안 된다"

    rl_keys = [k async for k in redis.scan_iter(match="rl:-:198.51.100.9:consultation:*")]
    assert len(rl_keys) == 1 and 60 < await redis.ttl(rl_keys[0]) <= ratelimit.WINDOW_HOUR
    spoof_keys = [k async for k in redis.scan_iter(match="rl:-:203.0.113.*:consultation:*")]
    assert not spoof_keys, "헤더 값으로 새 버킷이 생기면 안 된다"

    async with client(api_app, address="198.51.100.10") as other:
        assert (await other.post("/v1/consultations", json=BODY)).status_code == 202


async def test_logs_never_contain_the_applicant(api, caplog):
    caplog.set_level(logging.DEBUG)
    with capture_logs() as structured:
        accepted = await api.post("/v1/consultations", json=BODY)
        rejected = await api.post("/v1/consultations", json={**BODY, "agree_privacy": False})
    assert accepted.status_code == 202 and rejected.status_code == 422

    secrets = {"clinic": CLINIC, "name": NAME, "phone": PHONE, "email": EMAIL, "message": BODY["message"]}
    received = [r for r in caplog.records if r.getMessage() == "consultation received"]
    assert received and received[0].consultation_id == accepted.json()["id"]  # 접수 자체는 기록된다
    assert len([r for r in caplog.records if r.getMessage() == "http request"]) >= 2
    for record in caplog.records:
        rendered = _record_text(record)
        for label, secret in secrets.items():
            assert secret not in rendered, (
                f"{label} leaked through logger {record.name}: {record.getMessage()}"
            )
    for event in structured:
        rendered = json.dumps(redact_dict(dict(event)), ensure_ascii=False, default=repr)
        for label, secret in secrets.items():
            assert secret not in rendered, f"{label} leaked through structlog event {event.get('event')}"


async def test_retention_purge_removes_only_rows_past_the_window(api, app_engine, settings):
    """보존 경로는 owner 의 ``chartwire db purge-consultations`` 하나뿐이다 (``chartwire_app`` 은 DELETE 불가)."""
    fresh = (await api.post("/v1/consultations", json=BODY)).json()["id"]
    stale = (
        await api.post("/v1/consultations", json={**BODY, "email": f"old-{uuid4().hex}@example.test"})
    ).json()["id"]

    # 나이 먹이기도 owner 로만 된다 — app 역할에는 UPDATE 권한이 없다 (아래 테스트가 그 자체를 못 박는다).
    def _age(url: str, row_id: str) -> None:
        engine = make_sync_engine(as_sync_url(url))
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE consultation_requests SET created_at = now() - interval '91 days' WHERE id = :i"
                    ),
                    {"i": row_id},
                )
        finally:
            engine.dispose()

    # owner URL 은 개발 DB 를 가리킨다 — 테스트 DB 로 갈아 끼운다 (tests/conftest.py 와 같은 방식).
    owner_url = with_database(settings.database_owner_url, settings.test_db).render_as_string(
        hide_password=False
    )
    await asyncio.to_thread(_age, owner_url, stale)

    deleted = await asyncio.to_thread(purge_consultations, owner_url, older_than_days=90)
    assert deleted >= 1
    assert await stored(app_engine, UUID(stale)) is None, "보존 기간이 지난 접수는 지워져야 한다"
    assert await stored(app_engine, UUID(fresh)) is not None, "기간 안의 접수는 남아야 한다"


async def test_app_role_cannot_delete_consultations(app_engine):
    """런타임 역할에 DELETE 권한이 없다는 것이 보존 정책의 실질적 보증이다."""
    async with AsyncSession(app_engine) as s:
        with pytest.raises(ProgrammingError) as exc:
            await s.execute(text("DELETE FROM consultation_requests WHERE false"))
    assert "permission denied" in str(exc.value).lower()
