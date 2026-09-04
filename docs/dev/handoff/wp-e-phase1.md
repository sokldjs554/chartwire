# WP-E Phase 1 핸드오프 — 보안/REST/동의/파기

> 상태: **진행 중** (2026-09-03 시작). 중단되면 §0 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5`
> 라이브 포트 8104, 테스트 DB `chartwire_test_e`, Redis 인덱스 5.

## 0. 체크리스트 (재개용)

- [ ] `redis/ratelimit.py`, `redis/idempotency.py`
- [ ] `api/deps.py` (AppDeps, get_deps, principal/ctx 의존성), `api/schemas.py`
- [ ] `api/middleware.py` (request-id, 보안 헤더, 본문 상한, rate limit, idempotency)
- [ ] `api/app.py` (`create_app`, lifespan, problem+json 핸들러, guarded include, `/console`)
- [ ] `consent/service.py` (grant / revoke / active_scopes_for_patient)
- [ ] routers: auth, users, patients, consents, sessions, segments, search, alerts, purge, audit, opsviews
- [ ] `purge/pipeline.py`, `purge/verify.py`, `purge/receipt.py`, `purge/cli.py`, `audit/cli.py`
- [ ] `worker/handlers/purge_run.py`, `worker/handlers/purge_verify.py`
- [ ] tests: `test_api_*.py`, `test_rbac_matrix.py`, `test_purge*.py`, `test_phi_logs.py`
- [ ] docs: `consent-purge.md`, `adr/0004`, `security/threat-model.md` 갱신
- [ ] ruff / ruff format / mypy, 최종 테스트, 이 문서
