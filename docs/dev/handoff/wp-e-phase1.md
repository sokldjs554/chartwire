# WP-E Phase 1 핸드오프 — 보안 / REST / 동의 / 파기

> 상태: **완료** (2026-09-04, 재개 실행에서 마무리). 게이트: `pytest tests/integration/test_rbac_matrix.py tests/integration/test_api_*.py tests/integration/test_phi_logs.py tests/integration/test_purge_pipeline.py -q -p no:xdist`
> = **29 passed / 0 failed** (≈13 s, 실제 PostgreSQL/Redis/LocalFs) · Phase 0 단위 스위트(`test_auth_*`, `test_crypto_*`, `test_consent_gates`, `test_core_redis_keys`) **281 passed** · 전체 `tests/unit` 690 passed ·
> `ruff check` + `ruff format --check` clean(37 파일) · `mypy src/chartwire/api src/chartwire/purge src/chartwire/consent src/chartwire/redis/{ratelimit,idempotency}.py src/chartwire/worker/handlers/purge_*.py src/chartwire/audit/cli.py` clean(유일한 잔여 오류는 WP-D `api/routers/notes.py:43`, 아래 §6) · `mypy --strict src/chartwire/crypto src/chartwire/outbox` clean ·
> 라이브 스모크 `chartwire serve api --port 8104`: `/healthz` 200, `/readyz` `{"postgres":"ok","redis":"ok"}`, `/console` 200, `/v1/me` 401 problem+json → dev 토큰 200, `/metrics` 27 계열, WS 라우트 마운트, SIGTERM → `drain started` → `ws drain started` → 정상 종료.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5`. 테스트는 in-process ASGI(`httpx.ASGITransport`)라 포트를 쓰지 않는다. git 상태를 바꾸는 명령은 실행하지 않았다. 트리의 `synth/*`, `test_seed.py`, `test_bulk_small.py`, `wp-f-phase1.md` 는 동시 작업 중인 WP-F 의 것이다.

## 0. 체크리스트

- [x] `redis/ratelimit.py`, `redis/idempotency.py`
- [x] `api/deps.py`, `api/schemas.py`, `api/middleware.py`, `api/app.py`
- [x] `consent/service.py`
- [x] routers: auth, users, patients, consents, sessions, segments, search, alerts, purge, audit, opsviews
- [x] `purge/pipeline.py`, `purge/verify.py`, `purge/receipt.py`, `purge/cli.py`, `audit/cli.py`
- [x] `worker/handlers/purge_run.py`, `worker/handlers/purge_verify.py`
- [x] tests: `api_support.py`(공용 시드), `test_rbac_matrix.py`, `test_api_auth_users.py`, `test_api_patients_consents.py`, `test_api_sessions.py`, `test_api_middleware.py`, `test_phi_logs.py`, `test_purge_pipeline.py`
- [x] docs: `consent-purge.md`, `adr/0004-retention-vs-purge-split.md`, `security/threat-model.md` 갱신
- [x] ruff / ruff format / mypy, 라이브 스모크, 최종 테스트, 이 문서

## 1. 만든 것 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `api/app.py` (≈300) | `create_app(settings)`: lifespan 이 `AppDeps` 를 만들어 `app.state.deps` 에 두고 `keys:invalidate` 구독 태스크(DEK 캐시 제거)를 띄운 뒤 `ws.routes.on_startup(app, deps)` → `ops.routes.on_startup(app, deps, role="api")`; 종료는 역순. ws/notes/ops 라우터는 `_optional()`(ImportError 가드) 로 포함. problem+json 핸들러: `AppError`, `ConsentScopeMissing`(CW-4031), Starlette `HTTPException`(라우터 자체 404/405 포함, `WWW-Authenticate` 헤더 보존), `RequestValidationError`(CW-4220, `errors[]` 에 `loc/msg/type` 만 — `input` 은 자유 텍스트일 수 있어 제외), `CryptoError`(CW-5001, 사유 토큰만), `Exception`(CW-5000). `/console`·`/console/index.html` 은 `console/index.html` 을 `FileResponse` 로(경로: `CHARTWIRE_CONSOLE_DIR` → 저장소 → `/app/console`). 미들웨어 순서(바깥→안): RequestId → SecurityHeaders → CORS(`CHARTWIRE_CORS_ORIGINS`) → BodyLimit → RateLimit → Idempotency. `jwt_secret`/`clock` 의존성을 `Settings.jwt_secret`·`deps.clock` 으로 오버라이드 | §6.9, §8.1, §13.3 |
| `api/deps.py` | `AppDeps(settings, engine, redis, kek, keycache, objectstore, clock, node_id)`, `get_deps(request|websocket)`(없으면 503 CW-5030), `open_tx(request, principal)` = `tenant_tx` + 세 GUC, `principal_ctx`, `actor_user_id`(dev 토큰은 행을 소유할 수 없음 → 403 CW-4032), `load_tenant`(비활성 403 CW-4033), `session_dek`(KeyCache 경유, 파기 세션 410 CW-4100), `not_found`(RLS 로 안 보이는 행과 없는 행은 동일하게 404) | §3.1 |
| `api/middleware.py` (≈430) | 순수 ASGI(`BaseHTTPMiddleware` 아님 — WS 스코프 통과, 스트리밍 비버퍼링). `RequestId`(`X-Request-Id` 수용/생성 uuid7, structlog `bind_context`, 응답 헤더 에코, 접근 로그 한 줄: method·path·status·duration — **쿼리스트링 없음**), `SecurityHeaders`(nosniff, DENY, no-referrer, CSP — 콘솔은 inline 허용·API 는 `default-src 'none'`, HSTS, no-store), `BodyLimit`(1 MB, Content-Length 선검사 + 스트림 계수 → 413 CW-4130), `RateLimit`(§5 `rl:` 키, `rest` 120/min·`ws-ticket` 30/min 버킷, 토큰이 있으면 `sub` 로·없으면 클라이언트 주소로, 429 CW-4290 + `Retry-After`/`X-RateLimit-*`; Redis 장애는 **fail-open** + 경고), `Idempotency`(§5 `idem:{tenant}:{key}` SET NX 24 h; POST sessions / patients/{id}/consents / consents/{id}/revoke / purge-jobs; 같은 키·같은 본문 → 저장된 status/content-type/body 재생 + `Idempotent-Replayed: true`, 다른 본문 422 CW-4222, 처리 중 409 CW-4095, 5xx·예외는 저장 안 함, 키 형식 422 CW-4223) | §5, §8.1 |
| `api/schemas.py` | §3.2 모델 전부 + `MeOut, UserCreate/Out, ConsentRevoke/RevokedOut, SessionList, TimelineOut, VerifyDecryptOut, AuditList, OutboxStatsOut, DeadLetterOut, ReplayOut, PartitionOut, Problem`. 모든 응답 모델 `extra='forbid'` | §3.2 |
| `routers/auth.py` | `POST /auth/token`(테넌트 slug → 이메일 블라인드 인덱스 → scrypt 검증; 테넌트/사용자 부재 시에도 더미 해시를 검증해 타이밍 동일; 15 분 HS256; 감사 `auth.login`), `GET /me` | §8.1 |
| `routers/users.py` | admin: 생성(이메일 = `email_hmac` 블라인드 인덱스 + record key 로 `email_enc`, AAD 가 인덱스에 바인딩; scrypt; 감사 `user.created`), 목록(복호화 실패 시 `email: null`, 목록은 절대 실패하지 않음) | §6.9 |
| `routers/patients.py` | 환자 DEK 생성·래핑, `name_enc`/`phone_enc`(AAD `tenant:patient:<pseudonym>:field`), `name_hmac`; `GET /patients?name=` 은 정확 일치(Q6); 이름은 clinician/staff 에게만 복호화, DEK 파기 뒤 `null` | §6.9, §8.2 |
| `routers/consents.py` | grant(새 버전)·list·revoke — 로직은 `consent/service.py` | §8.3 |
| `routers/sessions.py` | 생성(환자 존재·미파기 → 동의 `recording` 게이트 CW-4031 → 임상의 검증 → DEK 생성·테넌트 KEK 래핑·지문 → `scopes_snapshot` → 감사 `session.created`), keyset 목록(`before`=created_at; clinician 은 본인 것만, staff/admin 은 테넌트), 조회(own 규칙 → 403), ws-ticket(`chartwire.redis.tickets.issue`; clinician 은 own 세션의 ingest/watch, staff 는 watch, recorder 는 ingest; 종료 상태 세션의 ingest 는 409), `POST /end`(ack_seq → final_seq, 감사 `session.ended`, 커밋 뒤 `SessionState.xadd_end` + 해시 `ended` + `session.state` 발행 + 24 h TTL) | §6.9 |
| `routers/segments.py` | `GET /sessions/{id}/segments`(`replay(started_at=)` 로 파티션 프루닝, DEK 는 먼저 언래핑 → 파기 세션은 행이 없어도 410), `GET /patients/{id}/timeline`(Q1 keyset `(created_at,id)`, 6 개월 창, 세션별 DEK 캐시, clinician 은 본인 세션만) | §4.6 Q1/Q1a |
| `routers/search.py` | `search_segments()` 경유; text 모드 <3자 422 CW-4220; 히트에 `seq` 를 붙이기 위해 `segments.by_keys` | §4.6 Q2 |
| `routers/alerts.py` | 목록(Q3 `list_open`; clinician 은 본인 세션 필터; `phrase` 는 응답에 없음), ack — `chartwire.risk.alerts.ack(deps, …)`(WP-C, 뷰어 소켓과 같은 구현) 를 쓰고 모듈이 없으면 인라인 폴백(UPDATE + 감사 + ZREM + `risk.ack` 발행) | §6.9 |
| `routers/purge.py` | admin `POST /purge-jobs` → 작업 행 + 아웃박스 `purge.requested` + 감사 → 202 `PurgeReceiptOut`(queued); `GET /purge-jobs/{id}` 영수증(`receipt_hash_valid` 재계산); `POST …/verify-decrypt` 는 **지금** 언래핑·샘플 복호화를 시도(감사 `purge.verify_decrypt`) | §8.4 |
| `routers/audit.py`, `routers/opsviews.py` | 감사 keyset(id) + `action` 필터; `/ops/outbox`(테넌트 stats), `/ops/dead-letters`, `/ops/dead-letters/{id}/replay`(`outbox.dlq.replay` + 감사 `outbox.replayed`), `/ops/partitions`(pg_inherits) | §6.9 |
| `consent/service.py` | `active_scopes_for_patient`, `require_scope_for_patient`, `policy_hash`, `grant`(파기된 환자 409 CW-4096, 감사 `consent.granted`), `revoke`(한 tx: 철회 + `consent_state` + 환자 단위 purge job + 아웃박스 `consent.revoked {patient_id, consent_id, purge_job_id}` + 감사 2건; 재철회 409 CW-4097), `notify_revoked`(커밋 뒤 live 세션마다 `ctl` `consent_revoked`, `outbox:wake`) | §8.3 |
| `purge/pipeline.py` | `run(ctx, job_id, tenant_id=)`: §8.4 1–6 단계, 단계마다 `steps` 추가(`steps || …` SQL) + 감사 `purge.step`, 카운트 합산, `session.purged`, `purge.completed`(감사 + 아웃박스 + `outbox:wake`), `keys:invalidate`. 완료/검증 작업은 그대로 반환, 이미 파기된 세션은 `skipped`. 환자 단위 = 모든 세션 + `patient_shred`. `create_job` | §8.4 |
| `purge/verify.py` | `run(ctx, job_id, tenant_id=)`: 세션마다 rows/objectstore/redis/dek_null/unwrap/decrypt_sample, 환자 단위 `patient_shredded`; 판정을 **자기 트랜잭션**에 커밋한 뒤 실패면 `PurgeVerifyFailed`(→ 재시도 → DLQ); `attempt_decrypt` 는 REST 데모와 공유 | §8.4 7단계 |
| `purge/receipt.py` | `canonical_json`, `receipt_hash(steps, counts, dek_fingerprints)`, `build(job)`(+ `receipt_hash_valid`) | §8.4 6단계 |
| `purge/cli.py`, `audit/cli.py` | `chartwire purge run --tenant --session|--patient [--no-verify]` · `receipt --job` · `verify --job`(실패 exit 1, `--tenant` 없으면 활성 테넌트 순회) · `chartwire audit list --tenant [--action] [--limit] [--before]`. 둘 다 `LAZY_SUBAPPS` 에 자동 마운트됨(확인) | §13.1 |
| `worker/handlers/purge_run.py` | `purge.requested`·`consent.revoked` 핸들러(lease 300 s). `consent.revoked` 에 `purge_job_id` 가 없으면 핸들러 tx 안에서 환자 단위 작업을 만든다 | §7.2 |
| `worker/handlers/purge_verify.py` | `purge.completed` → `verify.run` (lease 300 s) | §7.2 |
| `redis/ratelimit.py`, `redis/idempotency.py` | `hit()`→`Decision(allowed, count, limit, retry_after_s)`, §3.1 `check()`; `Record`/`begin`(SET NX)/`complete`/`release`, `body_hash`, `valid_key` | §5 |
| `docs/consent-purge.md` | 범위·게이트·철회 tx·파기 단계 표·영수증 JSON·검증 표·법적 잔존물·명시된 한계·명령 (한국어) | §14 |
| `docs/adr/0004-retention-vs-purge-split.md` | 두 키 스코프, 서명 = record key 재봉인, crypto-shred + hard delete, "영수증" 명명, WAL/백업 한계 | §0.6 |
| `docs/security/threat-model.md` | 5·7·9·11·12 행의 `(예정)` 을 실제 파일/테스트로 교체, 추가 항목 3개(테넌트 간 REST, Idempotency-Key, 파기 뒤 재기록) | §8.6 |

LOC(비공백): 구현 3,009(Phase 0 auth/crypto/consent 984 별도) / 테스트 1,585. §0 예산(1,800/800, Phase 0 포함)을 넘는다 — 라우터 11개와 미들웨어 5종이 대부분이며, 절단 후보는 `opsviews`(≈80, WP-G CLI 와 중복)와 `alerts._ack_inline` 폴백(≈20).

## 2. 테스트 (29 passed)

```bash
source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5
pytest tests/integration/test_rbac_matrix.py tests/integration/test_api_*.py tests/integration/test_phi_logs.py tests/integration/test_purge_pipeline.py -q -p no:xdist   # 29, ≈13 s
pytest tests/unit/test_auth_*.py tests/unit/test_crypto_*.py tests/unit/test_consent_gates.py -q                                                                       # Phase 0, 281
ruff check src/chartwire/api src/chartwire/purge src/chartwire/consent src/chartwire/redis src/chartwire/audit src/chartwire/worker/handlers/purge_*.py tests/integration/test_api_*.py tests/integration/test_rbac_matrix.py tests/integration/test_phi_logs.py tests/integration/test_purge_pipeline.py tests/integration/api_support.py
mypy src/chartwire/api src/chartwire/purge src/chartwire/consent src/chartwire/redis/ratelimit.py src/chartwire/redis/idempotency.py src/chartwire/worker/handlers/purge_run.py src/chartwire/worker/handlers/purge_verify.py src/chartwire/audit/cli.py
CHARTWIRE_DATABASE_URL=postgresql+asyncpg://chartwire_app:chartwire_app@localhost:5432/chartwire_test_e CHARTWIRE_REDIS_URL=redis://localhost:6379/5 chartwire serve api --port 8104   # 라이브 스모크
```

| 파일 | 검증 |
|---|---|
| `test_rbac_matrix.py` (3) | 실제 `create_app` 의 모든 `APIRoute`(`_IncludedRouter.original_router` 재귀, notes/ops 포함) 집합 == `rbac.MATRIX` 키 집합(양방향) · 37 라우트 × 5 역할(실제 사용자 행) + 익명: 허용 역할은 401/403 아님, 거부 역할은 정확히 `403 CW-4030` problem+json(조회·본문 검증보다 먼저), 공개 라우트는 익명 통과, 나머지는 익명 `401 CW-4010` + `WWW-Authenticate: Bearer` · `service` 토큰은 모든 사용자 라우트에서 403 |
| `test_api_auth_users.py` (5) | admin 사용자 생성 → 이메일 대소문자 무시 중복 409 → 목록에서 record key 복호화 → 로그인(대문자 이메일) → `/me` → DB 해시 `scrypt$`, 감사 `user.created`,`auth.login` · 없는 사용자/없는 테넌트/틀린 비밀번호 전부 같은 `401 CW-4010`(메시지에 힌트 없음) · 토큰 없음/변조/다른 비밀 401 |
| `test_api_patients_consents.py` (4) | 이름 암호문·블라인드 인덱스 정확 일치(정규화)·부분 검색 불가 · **테넌트 B 토큰은 A 의 환자·동의·세션 생성 전부 404 / 빈 배열** · 동의 버전 1→2→3, 중복 제거·정렬, 동의 없음/최신 버전에 recording 없음 → `403 CW-4031`, `scopes_snapshot`, 알 수 없는 범위 422(입력 미에코) · 철회 202 → `ctl:{sid}` 에 `{"t":"consent_revoked"}` 수신, `consent_state=revoked`, purge job(queued, patient), 아웃박스 페이로드·멱등 키, 감사 순서, 재철회 409, 이후 세션 생성 CW-4031 |
| `test_api_sessions.py` (7) | 생성: 래핑 DEK 언래핑 가능·지문·감사 detail, dev 토큰 CW-4032, 잘못된 clinician_id CW-4224 · own 규칙(타 임상의 403, staff 200, 타 테넌트 404), keyset 페이지 · ws-ticket: 페이로드·GETDEL 1회, staff/recorder/타 임상의 규칙, **ws-ticket 버킷 30/min 429 이고 REST 버킷은 무관** · end: `final_seq=ack_seq`, 스트림 엔드 마커 `{"end":"1","ep":"3"}`, 해시 `ended`, TTL, `session.state` 발행, 재종료 409 · 세그먼트 복호화·`after_seq`, 타임라인 keyset 커서, 검색 <3자 422 / text / term(+patient_id) · 경보 목록(own 필터, phrase 없음) + ack(ZREM, `risk.ack` 발행, 감사 `via=rest`, 404) · 파기 세션 세그먼트 `410 CW-4100` |
| `test_api_middleware.py` (4) | 요청 id 에코/생성, 보안 헤더, 404/422/미존재 경로 problem+json(`input` 없음) · **Idempotency-Key**: 같은 키·본문 재생(`Idempotent-Replayed`, 행 1개), 다른 본문 CW-4222, 테넌트 범위(다른 principal 도 재생), 4xx 도 재생, 키 형식 CW-4223, 비대상 라우트 무시 · **429**: 120회 200 → 121회째 CW-4290 + `Retry-After`, `rl:` 키 TTL, 다른 principal 무관 · 1 MB 초과 413, CORS 허용/거부, `/console` 200 + CSP, `/healthz` |
| `test_phi_logs.py` (1) | 세그먼트·환자·검색·타임라인·422(마커 본문)·티켓·경보 ack 요청 7건을 지나며 caplog(DEBUG, **모든 레코드 필드**)와 structlog 이벤트를 수집 → 전사 마커·환자 이름·전화번호·JWT·티켓 0건, 접근 로그에 `q=` 없음, 응답에는 PHI 가 실제로 있었음(공허한 통과 방지) |
| `test_purge_pipeline.py` (5) | **세션 단위 e2e**: `POST /purge-jobs` → `purge.requested` → `Poller.run_once` → 5 단계·카운트(objects 5, chunks 5, segments 4, search 4, risk 1, notes 1(초안만), redis_keys 4, stt_active 1) → `ctl purge`·`keys:invalidate` 수신 + api KeyCache 즉시 제거 → 모든 테이블 0행·오브젝트 0·Redis 0 → 서명 노트만 남고 record key 로 열림 → 툼스톤 → 2번째 패스 `verified` (6 검사) → 영수증 `receipt_hash_valid` → `verify-decrypt` `{failed:dek_destroyed, failed:invalid_tag}` → 트리거가 DEK 복원 거부 → `DekDestroyedError`/`DecryptError` → 감사 `purge.step`×5·`purge.completed`(detail 에 PHI 키 없음) · 완료 작업 재실행/재검증 no-op, 같은 세션 2번째 작업은 `skipped{already_purged}` · **환자 단위**(철회 → `consent.revoked` → 세션 2개 + `patient_shred`, 지문 3개, 이름/전화 NULL, `consent_state=purged`, 동의 기록 409 CW-4096, 세그먼트 410, `patient_shredded` 검사) · **검증 실패**: 잔존 오브젝트 → `failed` 저장 + 예외 → 8회 → DLQ 1행(`PurgeVerifyFailed`) → 잔존물 제거 → REST replay → `verified`, `/ops/outbox`·`/ops/partitions` · **CLI**: `run_purge`/`load_receipt`(테넌트 순회)/`run_verify`/`audit list`, 없는 작업은 `BadParameter` |

## 3. 이번 실행에서 테스트가 잡은 결함 (구현 쪽)

| 결함 | 어떻게 드러났나 | 수정 |
|---|---|---|
| `pipeline.finalize` 가 `self.job.steps` 를 읽음 — `append_step` 의 UPDATE 가 매핑 인스턴스의 컬럼을 만료시켜 async 밖 lazy load(`MissingGreenlet`) | purge e2e 5건 전부 | `_Run` 이 매핑 인스턴스 대신 `job_id/tenant_id/prior_steps` 평문 값을 들고, 영수증은 `prior_steps + steps` 로 계산 |
| 파기된 세션의 `GET /segments` 가 행이 없으면 `200 []` (DEK 검사 전에 조기 반환) | 환자 단위 파기 테스트의 410 단정 | DEK 언래핑을 replay 앞으로 → 파기 세션은 항상 `410 CW-4100` |
| 라우터가 매칭되지 않는 경로의 404 가 Starlette 기본 `{"detail":"Not Found"}` | 미들웨어 problem 형태 테스트 | 핸들러를 `starlette.exceptions.HTTPException`(기반 클래스) 에 등록 |
| record key 를 못 여는 테넌트에서 `GET /users` 가 트레이스백 500 | RBAC 매트릭스(픽스처 자리표시자 키) | `CryptoError` → `500 CW-5001` problem(사유 토큰만 로그), 테스트 테넌트는 실제 래핑 키 |
| `patients.py` 에 쓰지 않는 `redis.keys` import 와 인라인 `_uuid` 헬퍼, `alerts.py` import 순서, `segments.py` 미타입 헬퍼 | ruff / 리뷰 | 정리(경로 파라미터는 `UUID` 타입으로 422) |

## 4. 스펙·계약과 다르게 한 점 (이유)

1. **`consent.revoked` 는 환자 단위 작업 하나** — §7.2 는 "세션마다 purge_jobs + 환자 단위" 라고 쓰지만, 한 작업이 모든 세션을 순회해 영수증 하나·트랜잭션 하나로 끝난다(단계마다 `subject_id` 로 세션이 구분됨). 세션마다 작업을 만들면 영수증이 N+1 개가 되고 검증도 N+1 회다. 철회 tx 가 작업 행을 미리 만들어 응답에 `purge_job_id` 를 실어 준다(콘솔이 바로 폴링).
2. **`Idempotency-Key` 대상에 `POST /consents/{id}/revoke` 포함** — §5 의 "sessions/consents/purge-jobs" 를 동의 변경 두 라우트로 해석했다. 키 범위는 §5 그대로 **테넌트** 단위(`idem:{tenant}:{key}`).
3. **레이트리밋은 Redis 장애 시 fail-open** — 스로틀은 기밀성 통제가 아니고, Redis 가 흔들릴 때 모든 REST 를 거부하면 캐시 장애가 api 장애가 된다(ingest 는 §5 대로 fail-closed). 경고 로그 1줄.
4. **검증(`purge_verify`)은 폴러 tx 에 참여하지 않는다** — 판정을 먼저 자기 tx 로 커밋한 뒤 예외를 던져야 `failed` 가 재시도·DLQ 와 무관하게 남는다. `purge_run` 은 참여한다(전부-또는-무).
5. **`GET /alerts?open=0` 은 빈 배열** — repo 에 닫힌 경보 목록 함수가 없다(확인된 경보는 감사와 세션 재생으로 추적). 필요하면 WP-A 에 `risk.list_for_tenant(acknowledged=True)` 요청.
6. **오류 코드 추가**: CW-4032(dev 토큰은 행 소유 불가), 4033(비활성 테넌트), 4095(멱등 키 처리 중), 4096(파기된 환자), 4097(이미 철회), 4098(가명 번호 경합), 4099(세션 상태), 4100(파기 세션, 410), 4130(본문 크기), 4220(검증), 4222/4223(멱등 키), 4224(clinician_id), 4225(타임라인 커서), 4290(429), 5000(예상 밖), 5001(키 자료), 5030(미준비). 역할 거부 4030, 인증 4010, 동의 4031 은 Phase 0 그대로.
7. **감사 액션 추가**: `user.created`, `patient.created`, `outbox.replayed`, `purge.verify_decrypt`(§8.5 목록 밖; 전부 id/카운트만).
8. **`POST /purge-jobs` 응답 = `PurgeReceiptOut`(state=queued)** — 스펙은 "202" 만 정한다. 콘솔이 같은 모양으로 폴링할 수 있게 했다.
9. **`verify-decrypt` 의 환자 단위 작업** — DEK 가 아직 살아 있는 첫 세션을 보고하고, 전부 파기됐으면 마지막 세션. 샘플은 작업의 `sample_ciphertext`(첫 세션의 첫 `text_enc`) 하나.
10. **CORS 허용 목록은 `CHARTWIRE_CORS_ORIGINS` 환경변수** — `Settings` 는 WP-A 소유라 필드를 추가하지 않았다(§5 요청).
11. **`/console` CSP 는 `script-src 'self' 'unsafe-inline'`** — 단일 파일 콘솔(§13.3)이 인라인 스크립트다. API 경로는 `default-src 'none'`.
12. **접근 로그에 쿼리스트링 없음** — `GET /search?q=` 의 검색어가 전사 단어일 수 있다(§0.9).
13. `ratelimit.hit()` 이 `Decision` 을 돌려주고 §3.1 의 `check() -> bool` 은 그 위의 얇은 래퍼.
14. LOC 초과 — §1.

## 5. 다른 WP 에 요청 (정확한 diff)

- **WP-A (`core/config.py`)** — CORS·콘솔 경로를 Settings 로:
  ```python
  +    # --- api ---------------------------------------------------------------
  +    cors_origins: list[str] = []
  +    """콘솔 오리진 허용 목록. 비어 있으면 CORS 미들웨어를 붙이지 않는다."""
  +    console_dir: Path | None = None
  ```
  반영되면 `api/app.py` 의 `os.environ.get(CORS_ORIGINS_ENV)`/`CONSOLE_DIR_ENV` 두 줄을 `settings.cors_origins`/`settings.console_dir` 로 바꾼다.
- **WP-A (`db/repo/ops.py` 신규 또는 `repo/outbox.py`)** — `opsviews.partitions` 의 카탈로그 SQL 을 repo 로:
  ```python
  +async def list_segment_partitions(session: AsyncSession) -> list[tuple[str, str | None]]:
  +    stmt = text("SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i "
  +                "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
  +                "WHERE p.relname = 'transcript_segments' ORDER BY c.relname")
  +    return [(str(n), b) for n, b in (await session.execute(stmt)).all()]
  ```
- **WP-D (`api/routers/notes.py:43`)** — `chartwire.api.deps.get_deps` 가 이제 존재하므로 조건부 재정의(mypy `misc`)를 제거:
  ```python
  -try:
  -    from chartwire.api.deps import get_deps
  -except ImportError:
  -    def get_deps(request: Request) -> Any: ...
  +from chartwire.api.deps import get_deps
  ```
  `create_app` 은 `AppError` 핸들러를 자체 등록하므로 `install_error_handlers(app)` 호출은 no-op 으로 두거나 제거해도 된다(`test_notes_rest.py` 의 `application/problem+json` + `code` 기대와 동일한 모양).
- **WP-B (`ws/watch.py::_ack_alert`)** — WP-C 요청 5 와 같음: `risk.alerts.acknowledge_in_tx` + `after_ack` 로 교체하면 REST(`routers/alerts.py`)·WS 가 한 구현.
- **WP-G** — 변경 없음. `worker/main.py` 의 `HANDLER_MODULES` 이름(`purge_run`, `purge_verify`) 그대로 등록됨을 확인(`REGISTRY` 에 `consent.revoked`, `purge.requested`, `purge.completed`).
- **WP-H (console)** — 철회 응답 `ConsentRevokedOut{consent_id, patient_id, revoked_at, purge_job_id, outbox_event_id, live_sessions_notified}` 의 `purge_job_id` 로 `GET /v1/purge-jobs/{id}` 를 폴링하면 `steps[]` 가 늘어난다; "복호화 시도" 는 `POST /v1/purge-jobs/{id}/verify-decrypt` → `{decrypt_attempted, unwrap, decrypt_sample, job_state}`. 요청 헤더 `Idempotency-Key`·`X-Request-Id` 허용, 응답 헤더 `X-Request-Id`, `Retry-After`, `X-RateLimit-Remaining`, `Idempotent-Replayed` 노출. 콘솔 오리진은 `CHARTWIRE_CORS_ORIGINS`(쉼표 구분) 에 넣는다. 세션 목록은 `{items, next_before}`, 타임라인은 `{items, next_before: "<iso>,<id>"}`.
- **통합자** — `types-redis` 스텁이 redis 5(`aclose`, 비제네릭 `Redis`) 보다 오래되어 `app.py` 에 `type: ignore[attr-defined]` 2곳이 있다. redis-py 는 inline 타입을 싣고 있으므로 dev 의존성에서 `types-redis` 를 빼면 ignore 를 지울 수 있다. `pytest tests/integration` 전체 실행 시 이 WP 의 7 파일도 직렬(`-p no:xdist`).

## 6. 알려진 이슈

- `mypy src/chartwire/api` 는 WP-D `routers/notes.py:43` 의 조건부 정의 오류 1건을 보고한다(위 §5 diff 로 해소).
- `GET /alerts` 의 clinician 필터는 열린 경보의 세션마다 1회 조회(≤ limit, 세션별 캐시). `risk_repo.list_open(session_ids=)` 에 임상의의 세션 id 를 넘기는 쪽이 한 쿼리지만 `sessions` 를 먼저 훑어야 해 지금은 그대로 뒀다.
- `KeyCache` 는 프로세스별이라 `keys:invalidate` 를 놓친 노드는 최대 600 s 동안 언래핑된 키를 메모리에 둘 수 있다(새 요청은 `dek_wrapped IS NULL` 로 막힘). `docs/consent-purge.md` §7.
- `_keys_invalidate_loop` 는 `get_message(timeout=1.0)` 폴링(유휴 1 s 깨어남, WP-B `SubscriberManager` 와 같은 패턴).
- 환자 단위 파기는 한 트랜잭션이라 세션이 매우 많으면 리스 300 s 를 넘길 수 있다(효과는 `processed_events` PK 가 두 번째 커밋을 막아 정확히 1회).
- 라이브 스모크는 WP-E 테스트 DB 로 했다. `docker-compose`/`make dev-up` 경로(dev DB `chartwire` + seed)는 통합자 몫.
