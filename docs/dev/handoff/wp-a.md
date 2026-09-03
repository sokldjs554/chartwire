# WP-A 핸드오프 — Foundation (core / db / migrations / fixtures / infra 골격)

> 상태: **완료** (2026-09-03, 재개 실행에서 마무리). 게이트: `pytest tests/rls` 35 passed · `pytest tests/integration` 3 passed ·
> `pytest tests/unit/test_core_*.py` 전부 passed(단위 스위트 전체 428 중 유일한 실패는 WP-C 의 `test_risk_detector.py`, 작업 중) ·
> `ruff check src tests scripts` clean · `mypy src/chartwire/outbox src/chartwire/crypto src/chartwire/ws/{core,codec}.py` clean ·
> `chartwire db schema-dump --check` = "schema matches".
>
> 실행 환경: `set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_a CHARTWIRE_TEST_REDIS_DB=1`

## 1. 무엇을 만들었나 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `core/config.py` | `Settings(BaseSettings)`, env 접두사 `CHARTWIRE_`; `kek_master` 32바이트 검증; `test_db`/`test_redis_db`(테스트 격리); `get_settings()` (캐시 없음) | §3.1 |
| `core/logging.py` | structlog JSON, `redact_phi` 프로세서(키 집합 + 전화/주민번호 패턴, 중첩 dict/list 재귀), `bind_context()` | §0.9, §3.1 |
| `core/errors.py` | `AppError(code, status, detail, retryable)` → RFC 9457 problem+json dict; `CW-4xxx/5xxx` | §3.1 |
| `core/clock.py`, `core/ids.py` | `Clock` Protocol, `SystemClock`, `FakeClock(advance/set)`; RFC 9562 `uuid7()` (프로세스 내 단조, 12비트 카운터) | §3.1 |
| `db/engine.py` | `make_engine(url)`(asyncpg, `pool_reset_on_return="rollback"`, pre-ping), `make_sync_engine`, `as_async_url/as_sync_url/with_database` | §3.1 |
| `db/tenant.py` | `TenantCtx(tenant_id, user_id, role)` + `TenantCtx.service()`, `tenant_tx(engine, ctx)`(BEGIN 안에서 `set_config(..., true)` 3개), `apply_ctx(session, ctx)` | §3.1, §4.1 |
| `db/base.py`, `db/models/*` | SQLAlchemy 2.0 `Mapped[]` 모델(§4.2 전 테이블), `TENANT_TABLES` 튜플, 파티션 부모는 `postgresql_partition_by` | §3.1 |
| `db/repo/*` | 모든 SQL: `tenancy`(tenant/user 생성·조회), `patients`(환자, 동의 grant/revoke/latest_active, HMAC 검색), `sessions`(생성/상태/epoch/청크 원장 `insert_chunks`·`ledgered_seqs`·`max_ledger_seq`·`chunks_between`, stt_offsets), `segments`(`insert_final` 멱등, `replay`, `timeline` keyset, `by_keys`), `search`(`search_segments()` 호출, `upsert_search_row`), `risk`, `notes`, `outbox`(claim/lease/retry/DLQ/replay/prune/stats), `audit`, `purge` | §4.5 |
| `migrations/` | Alembic env(sync psycopg, owner, `transaction_per_migration`), `helpers.py`(GRANT 헬퍼·RLS 헬퍼·`ensure_segment_partition` v1/v2), `versions/0001..0007` 전부 `downgrade()` 구현 | §4.2, §4.3 |
| `db/cli.py` | `chartwire db bootstrap-roles|upgrade|downgrade|schema-dump [--check]|partitions ensure`; `normalize_schema_dump()`, `month_start()`; 함수는 conftest/Makefile 에서 직접 import | §13.1 |
| `redis/client.py`, `redis/keys.py` | `get_redis()`, `with_db(url, index)`; §5 키 이름의 단일 출처 | §5 |
| `objectstore/` | `ObjectStore` Protocol, `LocalFs(root)`, `S3(bucket)`(boto3 import-guard), `chunk_key()` | §3.1 |
| `outbox/writer.py` | `emit(...)`(같은 트랜잭션에 outbox 행), `idempotency_key()` | §7.1 |
| `audit/service.py` | `record(...)`; `detail` 에 `{text, quote, name, phone}` 키가 있으면(중첩 포함) `ValueError` | §8.5 |
| `cli.py` | typer 루트. `db`, `dev keygen` 은 직접, 나머지(`synth, seed, eval, perf, serve, simulate, loadtest, token, outbox, purge, audit, readme-numbers`)는 `LAZY_SUBAPPS` 로 모듈이 있을 때만 마운트 | §13.1 |
| `tests/conftest.py` | 아래 §2 픽스처 카탈로그 | §4.4 |
| `tests/rls/*` | `test_rls_leak`, `test_partition_direct`, `test_superuser_leaks`, `test_guc_leak`, `test_audit_immutable`, `test_search_function`, `test_repo_under_rls`(repo 계약 전부를 app 역할로) | §4.3, §8.1 |
| `tests/integration/test_migrations.py` | head→base→head 왕복, 덤프 동일성, 리비전별 1단계 downgrade, `normalize_schema_dump` 단위 | §4.3 |
| `docs/db/schema.sql`, `docs/db/schema.md` | 결정론적 pg_dump; 한국어 스키마 문서(ERD, 테이블, RLS 정책 표, 파티션, 마이그레이션 순서, 함정 9개) | §4, §14 |
| `docs/adr/0001-*.md`, `docs/adr/0002-*.md` | 단일 app 역할 + 테넌트 순회 / ack = PG 커밋, Redis 는 캐시 | §0.3, §0.7 |
| `Makefile`, `docker-compose.yml`, `docker/initdb/01_roles.sql`, `docker/nginx/drain.conf`, `Dockerfile`, `.github/workflows/ci.yml`, `scripts/dev_up.sh` | §12.2/§12.3 골격 — `docker compose config -q`, `bash -n`, YAML 파싱, `make -n` 으로 검증(도커 빌드는 CI `docker` 잡, WP-H) | §12 |

## 2. 픽스처 카탈로그 (`tests/conftest.py`)

| 픽스처 | 범위 | 준다 | 비고 |
|---|---|---|---|
| `settings` | session | `Settings()` | 환경변수 그대로 |
| `db_urls` | session | `DbUrls(name, owner, app, superuser, redis)` | `CHARTWIRE_TEST_DB` 로 DB 이름 치환, `CHARTWIRE_TEST_REDIS_DB` 로 Redis 인덱스 |
| `migrated_db` | session | `DbUrls` | superuser URL 이 있으면 `bootstrap_roles(databases=[test_db])`, 그 다음 `downgrade base` + `upgrade head` (`CHARTWIRE_TEST_KEEP_SCHEMA=1` 이면 downgrade 생략) |
| `owner_engine` / `app_engine` / `su_engine` | function | `AsyncEngine` | 테스트마다 새로 만들고 dispose (pytest-asyncio 루프 바인딩 때문). `su_engine` 은 superuser 누설 테스트·perf 전용 |
| `clean_db` | function | owner `AsyncEngine` | `TRUNCATE <TENANT_TABLES>, tenants RESTART IDENTITY CASCADE` |
| `factories` | function | `Factories` | `tenant(slug)`, `user(tenant_id, role)`, `patient(tenant_id)`, `session(tenant_id, patient_id, clinician_id, started_at=)` — 전부 합성 이름(`가상의원-`, `가상환자-NNNN`) |
| `tenant_a`, `tenant_b` | function | `Tenant` | slug `clinic-a`/`clinic-b` |
| `clinician_a`, `clinician_b` | function | `User` | role `clinician` |
| `patient_a`, `patient_b` | function | `Patient` | DEK 자리표시자 60바이트, `consent_state='none'` |
| `session_a`, `session_b` | function | `Session` | state `recording`, `started_at=now`, scopes_snapshot 4개 전부 |
| `ctx_a`, `ctx_b` | function | `TenantCtx(tenant, clinician, 'clinician')` | 서비스 컨텍스트는 `TenantCtx.service(tenant_id)` |
| `redis` | function | `redis.asyncio.Redis`(decode_responses) | 자기 인덱스만 `FLUSHDB` 전/후 |
| `fake_clock` | function | `FakeClock(2026-09-01T09:00Z)` | DB 기본값 `now()` 와 비교하는 테스트는 `SELECT now()` 로 앵커링(예: outbox 테스트의 `_tx_clock`) |
| `owner_session(engine)` | helper | `AsyncSession` | 컨텍스트 없는 owner 세션(`tenants` 쓰기 등) |

## 3. 다른 WP 가 쓰는 법

```python
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.db.repo import sessions as sessions_repo

async with tenant_tx(app_engine, TenantCtx.service(tenant_id)) as session:   # 한 트랜잭션, GUC 3개 설정, 종료 시 commit
    await sessions_repo.set_state(session, session_id, "ended", now=clock.now())
```
- 라우터/핸들러는 원시 SQL 을 쓰지 않는다 — 필요한 쿼리가 없으면 `db/repo/<area>.py` 에 추가 요청(핸드오프에 diff).
- 테스트 파일 상단에 `pytestmark = pytest.mark.integration`; PG/Redis 가 필요한 스위트는 직렬(`-p no:xdist`).
- 환경: `CHARTWIRE_TEST_DB=chartwire_test_<wp>`, `CHARTWIRE_TEST_REDIS_DB=<n>` (A=1 … H=8). `migrated_db` 가 DB 를 만들고 마이그레이션한다 — 남의 DB 를 지우지 말 것.
- Redis 키 이름은 `redis/keys.py` 함수로만 만든다.
- 감사 기록: `audit.service.record(session, tenant_id=…, actor_id=…, actor_role=…, action="session.created", resource_type="session", resource_id=id, detail={"chunk_count": 3})`. `detail` 에 PHI 키 금지(코드가 거부).
- outbox: 도메인 변경과 같은 `session` 에서 `outbox.writer.emit(...)`; 커밋 뒤 `outbox:wake` PUBLISH 는 호출자 몫.

## 4. 이번 실행에서 고친 것 (근본 원인)

| 실패 | 원인 | 수정 |
|---|---|---|
| `test_audit_immutable::*` 3건 | `INSERT … RETURNING` 은 SELECT 정책을 탄다 → 임상의 컨텍스트에서 감사 기록 자체가 실패. 또 0006 이 audit 에 INSERT/SELECT 정책만 두어 owner 의 UPDATE 가 0행(트리거 미도달) | 0006: `tenant_isolation`(ALL) + RESTRICTIVE `audit_read_gate`(SELECT, auditor/admin/service). `repo/audit.record`: `inline()` INSERT + `currval(pg_get_serial_sequence)`; 0005 에 `audit_events_id_seq` GRANT. 테스트: owner UPDATE/DELETE 는 `service` 컨텍스트로 실행 |
| `test_outbox_*` 2건 | `next_attempt_at DEFAULT now()`(DB 시각)인데 `FakeClock` 이 2026-09-01 고정 → "due" 가 아님 | 테스트가 트랜잭션의 `SELECT now()` 로 클록을 앵커링(`_tx_clock`) |
| `test_purge_*` | `to_jsonb($1)` 에 dict 파라미터 → asyncpg 다형 타입 오류 | `append_step`: `bindparam(type_=JSONB)` 로 `steps || '[…]'::jsonb` |
| `test_replay_prunes_to_started_at_partition` | `created_at >= started_at` 은 **과거** 파티션만 잘라낸다(미래 파티션·DEFAULT 는 남음). 테스트가 "1개"를 요구 | 테스트를 스펙 의미로 수정: 조건 없이는 −1월 파티션 포함, 조건이 있으면 제외 + 진부분집합 |
| `test_round_trip_and_schema_dump_equality` | `docs/db/schema.sql` 부재 | `chartwire db schema-dump` 로 생성(1,613줄) |
| `test_redacts_phi_keys_*` | `02-123-4567`(서울 국번 2자리)이 `\d{3}-…` 에 안 걸림 | 정규식을 `\d{2,4}-\d{3,4}-\d{4}` 로 확장(스펙 패턴의 상위 집합) |
| `test_uuid7_layout_and_timestamp` (전체 단위 스위트에서만) | 다른 테스트가 먼저 `uuid7()` 을 호출하면 단조 클램프가 고정 `now_ms` 를 앞으로 민다 | 테스트가 `monkeypatch` 로 모듈 상태를 초기화 |

## 5. 스펙과의 편차 (이유 포함)

1. **audit_events 정책 모양** — 스펙 §4.2 0006 은 "INSERT WITH CHECK 테넌트 / SELECT USING 테넌트 AND 역할" 이라고 쓰지만, 같은 절의 루프가 audit_events 에도 `tenant_isolation` 을 건다. 구현은 `tenant_isolation`(ALL) + RESTRICTIVE `audit_read_gate`(SELECT). app 역할 관점의 효과는 동일하고(INSERT 는 테넌트 안에서, SELECT 는 세 역할만), owner 의 UPDATE/DELETE 가 append-only 트리거에 도달한다(§8.5 테스트 요구).
2. **`audit_events_id_seq` GRANT (USAGE, SELECT)** — §4.1 권한 목록에 없다. RETURNING 없이 id 를 돌려주기 위한 `currval` 용.
3. **PHI 전화번호 정규식 확장** — §3.1 의 `\d{3}-\d{3,4}-\d{4}` 를 포함하는 `\d{2,4}-\d{3,4}-\d{4}`.
4. **`Settings.superuser_url`** 필드 추가(기본 `.env.example` 값) — `bootstrap-roles` 와 conftest 가 사용. `owner_password`/`app_password`/`db_single_role`/`test_db`/`test_redis_db` 도 스펙 목록 밖의 운영 필드.
5. **Dockerfile `CMD`** 는 `["sh","-c","exec chartwire serve ${CHARTWIRE_ROLE:-api}"]` — 스펙의 exec 형식 `["chartwire","serve","${CHARTWIRE_ROLE:-api}"]` 는 변수를 확장하지 않는다. `exec` 로 SIGTERM 이 그대로 전달된다(drain).
6. **compose `migrate`** 는 `chartwire seed --demo --if-empty`(스펙: `seed --demo`) — 컨테이너 재시작이 멱등하도록.
7. **`schema.sql` 정규화** — 월별 파티션 블록·`pg_dump` 버전 주석·`\restrict` 토큰 제거(스펙은 "version-comment lines stripped" 만 언급). 이유: 초기 파티션 이름이 달력에 따라 달라 CI 가 매달 깨진다.
8. **conftest `migrated_db`** 는 항상 `downgrade base` 후 `upgrade head` — 스펙 §4.4 의 "upgrade head" 보다 강한 보장(파티션/정책이 항상 마이그레이션에서만 나옴). `CHARTWIRE_TEST_KEEP_SCHEMA=1` 로 끌 수 있다.
9. **`segments.replay(..., started_at=None)`** — 인자를 안 주면 `sessions.started_at` 을 조회해 넣는다(스펙 시그니처의 확장, 호출자 편의).

## 6. 스텁 / 알려진 갭

- `objectstore/s3.py` 는 boto3 import-guard 만 있고 오프라인에서 실행되지 않는다(스펙대로).
- `chartwire serve|seed|simulate|loadtest|eval|perf|outbox|purge|audit` 는 해당 WP 가 `app = typer.Typer()` 를 노출해야 나타난다(현재 `db, dev, synth, token, readme-numbers` 마운트 확인).
- `docker-compose.yml` 의 `migrate`/`api`/`worker`/`stt-worker` 는 `chartwire serve`/`seed` 가 생겨야 실제로 뜬다; `docker build` 는 이 박스에서 검증하지 않았다(CI `docker` 잡, WP-H).
- CI 는 `lint/unit/integration/migrations` 4개 잡만 정의; `eval-smoke/load-smoke/cdk/docker/frozen-artifacts` 자리는 파일 하단 주석에 이름·`needs`·명령까지 적어 두었다.
- `docs/db/schema.md` 의 성능 수치는 없다(의도적) — `docs/perf/README.md`(WP-F) 를 가리킨다.
- 단일 역할 폴백(`CHARTWIRE_DB_SINGLE_ROLE=1`)은 마이그레이션 헬퍼 수준(`grant()` 가 역할 없으면 생략)까지만 구현; 런타임 URL 전환은 `Settings` 사용자(WP-E/G) 몫.

## 7. 다른 WP 에 요청

- **WP-G (`chartwire/worker/cli.py`)**: `app = typer.Typer()` + `@app.callback(invoke_without_command=True) def serve(role: str = typer.Argument("api"), embedded: bool = False)` — `cli.py` 의 `LAZY_SUBAPPS["serve"]` 가 이 모듈을 마운트하고 Dockerfile/compose 가 `chartwire serve <role>` 을 호출한다. `/healthz` 는 드레인 중에도 200(컨테이너 HEALTHCHECK), `/readyz` 만 503.
- **WP-F (`chartwire/synth/seed_cli.py`)**: 같은 방식으로 `chartwire seed --demo [--if-empty] [--seed 1]` (`LAZY_SUBAPPS["seed"]`). compose `migrate` 와 `scripts/dev_up.sh` 가 호출한다. `chartwire.eval.cli`, `chartwire.perf.cli` 도 같은 규약.
- **WP-H (`chartwire/loadtest/simulate_cli.py`, `chartwire/loadtest/cli.py`)**: `simulate` 와 `loadtest` 는 서로 다른 모듈이어야 한다(같은 sub-app 을 두 이름에 마운트하면 명령 트리가 어긋난다). CI 나머지 잡은 `ci.yml` 하단 주석대로; `tests/chaos` 가 준비되면 integration 잡 명령에 추가.
- **WP-E**: `token` sub-app 은 이미 lazy 마운트되어 있다(추가 코드 불필요). `outbox`/`purge`/`audit` CLI 모듈 경로는 `LAZY_SUBAPPS` 참조.
- **WP-C**: 전체 단위 스위트에서 `tests/unit/test_risk_detector.py::test_alertable_hit_preferred_over_suppressed_higher_severity` 가 실패 중(작업 중인 것으로 보임) — WP-A 범위 밖.

## 8. 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_a CHARTWIRE_TEST_REDIS_DB=1
pytest tests/rls tests/integration -q -p no:xdist      # 38 tests, PG 필요
pytest tests/unit/test_core_*.py -q                     # 서비스 불필요
make lint                                               # ruff, ruff format --check, mypy strict 모듈, pip-audit
make migrate-roundtrip                                  # 개발 DB 에서 왕복 + schema.sql 비교
make schema-dump                                        # 마이그레이션을 바꿨을 때 schema.sql 재생성 (테스트 DB 가 head 인 상태에서)
```
