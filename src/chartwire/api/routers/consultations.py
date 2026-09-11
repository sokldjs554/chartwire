"""``POST /v1/consultations`` — 데모 홈페이지의 "서비스 상담신청하기" 접수 (인증 없음, ``rbac.PUBLIC``).

실제 서비스처럼 서버에 저장하되 데모라 연락은 가지 않는다. 설계 결정:

* **테넌트 밖 데이터**: 신청자는 아직 어느 의원에도 속하지 않으므로 ``consultation_requests`` 에는
  ``tenant_id`` 가 없고 RLS 도 없다 (``tenants`` 와 같은 급). 그래서 **조회 라우트를 두지 않는다** —
  어느 테넌트의 admin 이 읽어도 자기 테넌트 밖 개인정보를 보게 되기 때문이다. 접수함은 운영자가
  DB 로만 본다 (``chartwire_app`` 은 INSERT/SELECT 뿐).
* **개인정보 최소화**: IP·User-Agent 는 저장하지 않고, 본문(이름·전화·이메일)은 어떤 로그에도 남기지
  않는다 — 접수 로그는 id 와 ``source`` 만 (``tests/integration/test_api_consultations.py`` 가 못 박는다).
* **남용 방지**: 클라이언트 주소당 시간당 10건 고정 윈도(``rl:-:{client}:consultation:{hour}``,
  :mod:`chartwire.redis.ratelimit`). 클라이언트는 미들웨어와 **같은 함수**(:func:`~chartwire.api.middleware.client_host`)
  로 구한 소켓 주소다 — ``X-Forwarded-For`` 를 여기서 읽으면 헤더만 바꿔 가며 우회할 수 있어서
  읽지 않는다. 프록시 뒤 배포는 :data:`~chartwire.core.config.Settings.trusted_proxy_ips` 로
  uvicorn 이 소켓 주소를 복원하게 한다. 초과는 ``429 CW-4291`` + ``Retry-After``. 미들웨어의
  ``rest`` 120/min 도 그대로 겹친다. Redis 장애는 미들웨어와 같은 이유로 fail-open (경고 로그) —
  접수함 throttle 은 기밀성 통제가 아니다.
* **보존**: 접수함은 :data:`RETENTION_DAYS` 일만 둔다. 지우는 경로는 owner 역할의
  ``chartwire db purge-consultations`` 하나뿐이다 (``chartwire_app`` 에는 DELETE 권한이 없다).
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import APIRouter, Request, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.api.deps import get_deps
from chartwire.api.middleware import ANONYMOUS_TENANT, client_host
from chartwire.api.schemas import ConsultationIn, ConsultationOut
from chartwire.core.errors import RateLimited
from chartwire.db.repo import consultations as consultations_repo
from chartwire.redis import ratelimit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["consultations"])

RETENTION_DAYS: Final = 90
"""접수 후 보존 기간. 폼의 개인정보 문구와 ``docs/db/schema.md`` §3.1 이 같은 값을 말한다."""
RECEIVED_MESSAGE: Final = "상담 신청이 접수되었습니다. 데모 서비스라 실제 연락은 가지 않습니다."


@router.post("/consultations", response_model=ConsultationOut, status_code=status.HTTP_202_ACCEPTED)
async def create_consultation(body: ConsultationIn, request: Request) -> ConsultationOut:
    deps = get_deps(request)
    client = client_host(request.scope)
    if deps.redis is not None:
        try:
            decision = await ratelimit.hit(
                deps.redis,
                ANONYMOUS_TENANT,
                client,
                ratelimit.BUCKET_CONSULTATION,
                ratelimit.CONSULTATION_PER_HOUR,
                now=deps.clock.now(),
                window_s=ratelimit.WINDOW_HOUR,
            )
        except (RedisError, OSError):
            log.warning(
                "rate limit store unavailable; allowing request",
                extra={"bucket": ratelimit.BUCKET_CONSULTATION},
            )
        else:
            if not decision.allowed:
                raise RateLimited(
                    "상담 신청이 너무 많습니다. 잠시 후 다시 시도하세요",
                    decision.retry_after_s,
                    code="CW-4291",
                )
    # ``consultation_requests`` 는 RLS 밖: 테넌트 GUC 없이 plain 세션으로 쓴다 (``auth.login`` 의 tenants 읽기와 같은 이유).
    async with AsyncSession(deps.engine, expire_on_commit=False) as s, s.begin():
        row = await consultations_repo.create_request(
            s,
            clinic_name=body.clinic_name,
            contact_name=body.contact_name,
            phone=body.phone,
            email=body.email,
            role=body.role,
            message=body.message,
            source=body.source,
        )
    # 본문은 절대 로그에 싣지 않는다 — id 와 유입 경로만.
    log.info(
        "consultation received", extra={"consultation_id": str(row.id), "source": row.source}
    )  # source 는 닫힌 어휘
    return ConsultationOut(id=row.id, received_at=row.created_at, message=RECEIVED_MESSAGE)
