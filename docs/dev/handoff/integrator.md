# 통합자(integrator) 핸드오프 — 전체 시스템 end-to-end 통합

> 상태: **진행 중** (2026-09-04 시작). 중단되면 §0 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a`
> 자체 테스트 DB `chartwire_test_i` / Redis 9. 데모 서버 포트 8000, 임시 서버 8110.

## 0. 체크리스트 (재개용)

- [ ] 1. 크로스 WP 요청 수집·적용 (§1 표)
- [ ] 2. 앱 조립 확인: `create_app()` ws/notes/ops/console/problem+json/middleware, `serve all --embedded` 기동
- [ ] 3. 전체 스위트 직렬 그린 (§3 표) + ruff / ruff format / mypy strict / pip check
- [ ] 4. e2e 데모 흐름 (§4) + `docs/dev/e2e.md` + `make demo`
- [ ] 5. 콘솔 Playwright 검증 + `docs/images/*.png`
- [ ] 6. 하우스키핑: `_nometrics` 제거, pyproject deps, `cdk-nag==2.38.2`, ci.yml, AGENT_ENV.md

## 1. 크로스 WP 요청 처리 표

(작업하면서 채운다: 요청 | 출처 | 적용/거절 | 한 줄 이유)

## 2. 앱 조립

## 3. 스위트 결과

## 4. e2e 데모

## 5. 콘솔

## 6. 하우스키핑

## 7. 알려진 이슈
