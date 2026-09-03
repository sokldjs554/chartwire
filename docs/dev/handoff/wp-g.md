# WP-G handoff — Worker / Outbox / Ops (Phase 0 완료)

> 상태: **Phase 0 완료** (2026-09-03). 이전 실행이 중단된 뒤 재개하여 마무리했습니다. 소유 경로 밖은 건드리지 않았고 git 상태를 바꾸는 명령은 실행하지 않았습니다.

## 1. 무엇을 만들었나 (모듈 지도)

| 경로 | 역할 | 비고 |
|---|---|---|
| `src/chartwire/outbox/registry.py` | `Registry` + `@registry.handler(event_type, *, lease_s=60, max_attempts=8, name=None)` 데코레이터(§3.1 계약), `HandlerSpec`(검증: `noun.verb` 이벤트 타입, 핸들러 이름, 양수 리스, `max_attempts ≥ 1`), `DuplicateHandlerError`(같은 `event_type` 두 번 등록 시, 첫 등록 유지), `UnknownEventTypeError`, 프로세스 전역 `REGISTRY` + 모듈 레벨 `handler` | mypy strict |
| `src/chartwire/outbox/context.py` | `HandlerContext(engine, redis, objectstore, clock, settings, kek, keycache)` — 다른 패키지 타입은 `Any` 또는 구조적 Protocol(`Clock`, `ObjectStore`, `KekProvider`)로만 참조하여 `outbox/`가 단독 임포트 가능. `OutboxEvent.from_row()`(드라이버 중립 UUID/JSON 강제 변환, 읽기 전용 payload), `OutboxStatus` | mypy strict |
| `src/chartwire/outbox/backoff.py` | `base_delay_s(attempts) = min(300, 2**attempts)`, `next_attempt(attempts, now, rng)` ±20 % 지터(RNG 주입 → 시드 결정론), `plan_retry(attempts_before, max_attempts, now, rng) -> RetryPlan` (`attempts ≥ max_attempts` → dead, 정확히 8번째 실패에서) | hypothesis 300 예제 |
| `src/chartwire/outbox/dlq.py` | 순수 데이터 결정: `on_failure(event, exc, *, max_attempts, now, rng) -> FailureOutcome`(행 업데이트 값 + `DeadLetterRecord`), `done_updates(now)`, `replay_updates(now)`, `format_error()`(타입+메시지, 전화/주민번호 형태 `[REDACTED]`, 2,000자 상한) | DB 없이 독약 메시지 테스트 |
| `src/chartwire/ops/metrics.py` | 스펙의 메트릭 이름 27개 전부를 **하나의 비공개 `CollectorRegistry`**에 등록(`ALL_NAMES`), `render() -> (body, content_type)`, `bind_db_pool(fn)`(스크레이프 시 지연 샘플링), `KNOWN_LABEL_VALUES` + `init_known_labels()`(닫힌 라벨 집합을 0으로 선생성), `*_created` 시계열 비활성화 | |
| `src/chartwire/ops/health.py` | `Health`: `livez() -> (200, body)` 항상 200, `readyz() -> (code, body)`: 드레인 중 503, 등록된 의존성 프로브(동기/비동기, 타임아웃·예외는 실패로 집계) 하나라도 실패 시 503 + 프로브별 상태 | |
| `src/chartwire/ops/drain.py` | `Drainer`: `begin()` 멱등, `on_begin` 훅 1회 실행(하나가 예외여도 계속), `install()`로 SIGTERM/SIGINT를 루프에 연결, `track()`으로 진행 중 작업 계수, `wait_drained()`(데드라인 내 `True`/초과 `False`), 주입 가능한 monotonic 시계 | 실제 SIGTERM 테스트 포함 |
| `docs/ops/runbook.md` | §0 전달 보장(최소 1회 + 멱등, "exactly-once" 아님) · §1 프로세스/신호 · §2 배포/드레인 절차 · §3 시나리오 A Redis 손실 · §4 시나리오 B 아웃박스 지연/DLQ · §5 시나리오 C SLA 초과 미확인 경보 · §6 파티션 점검 · §7 로그 이벤트 | 한국어 |
| `docs/ops/failure-modes.md` | 구성요소 × 장애 → 동작 → 복구 → 메트릭 표(24행) | 한국어 |

LOC: 구현 928 / 테스트 779 (예산 700/400 대비 테스트가 많습니다 — 순수 로직이라 테스트로 계약을 고정하는 편을 택했습니다. Phase 1의 poller/worker/tickers는 ~450 LOC 예상이라 구현 총합은 예산 근처에서 끝납니다).

## 2. 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/unit/test_outbox_*.py tests/unit/test_ops_*.py -q     # 69 passed
ruff check src/chartwire/outbox src/chartwire/ops tests/unit/test_outbox_*.py tests/unit/test_ops_*.py
ruff format --check src/chartwire/outbox src/chartwire/ops tests/unit/test_outbox_*.py tests/unit/test_ops_*.py
mypy --strict src/chartwire/outbox                                            # clean
mypy src/chartwire/ops                                                        # clean (strict 의무 아님)
```

`tests/unit` 전체도 통과합니다(400 passed, 3.6 s). DB/Redis는 사용하지 않았습니다(`chartwire_test_g` / Redis 인덱스 7은 Phase 1 통합·카오스 테스트용으로 예약).

## 3. 이번 재개에서 고친 것

1. `test_poison_message_dies_on_eighth_failure_only` — `OutboxEvent`가 `slots=True` 데이터클래스라 `__dict__`가 없어 테스트가 죽었습니다(구현 로직은 정확했음). `dataclasses.replace`로 교체. 7번째 실패까지 `pending`, 8번째 실패에서 `dead` + `DeadLetterRecord(attempts=8)`를 그대로 검증합니다.
2. `test_every_name_is_registered_and_rendered` — Prometheus 텍스트 노출은 카운터를 `# TYPE ws_chunks_total counter`처럼 `_total` 포함 이름으로 씁니다. 테스트가 접미사를 뗀 이름을 찾고 있었습니다. 정정하면서 **라벨 메트릭이 첫 관측 전에는 자식 시계열이 없다**는 실제 운영 문제도 같이 닫았습니다: `KNOWN_LABEL_VALUES`로 스펙이 닫힌 집합으로 정한 라벨 값을 임포트 시 0으로 선생성(테스트 `test_known_label_sets_exist_at_zero_before_any_observation`), `disable_created_metrics()`로 `*_created` 잡음 제거. `failure-modes.md` 머리말에 한 줄 반영.

## 4. 스펙과 다른 점 / 스펙이 침묵한 곳에서의 선택

| 항목 | 선택 | 이유 |
|---|---|---|
| `registry.handler`에 `name=` 키워드 추가 | 기본값은 함수 `__name__` | `processed_events.handler` 키가 함수 이름과 분리되어 리팩터링에도 안정적 |
| `registry.py`의 `KNOWN_EVENT_TYPES` | 등록 제한 아님(문서·라벨 선생성용) | 테스트/데모 핸들러 등록 허용 |
| `HandlerContext.engine/redis/settings/keycache`는 `Any` | 구조적 Protocol은 `clock/objectstore/kek`만 | `outbox/`는 mypy strict이고 다른 WP 패키지를 임포트하지 않아야 함(Phase 0 규칙); 핸들러가 실제 타입을 직접 임포트 |
| 백오프 지수 상한 `_CAP_EXPONENT = 9` | `attempts ≥ 9`는 곧장 300 s | replay 후 재실패 등 큰 attempts에서 거대 거듭제곱 방지, 결과는 동일 |
| `last_error` 2,000자 + PHI 형태 redact | `dlq.format_error` | 스펙은 컬럼 길이만 두고 내용 규칙이 없음; §0-9(로그에 PHI 금지)를 DLQ 진단 문자열에도 적용 |
| `segments_default_partition_rows` 게이지 | 스펙 목록 외 추가 | §7.2 "asserts default partition empty → metric"이 이름을 정하지 않음 |
| `risk_hits_total` 라벨 선생성 안 함 | 첫 관측 시 생성 | `category × severity × suppressed` 큐브는 데이터 주도, 0 채움은 잡음 |
| `Health.readyz()`는 드레인 중 프로브를 **실행하지 않음** | 즉시 503 | 드레인 중 느린 프로브가 LB 제거를 늦추지 않도록 |
| `Drainer.begin()`은 진행 중 작업이 0이면 즉시 `drained` | | 유휴 워커의 SIGTERM은 즉시 종료 |
| 런북 §3-5 `chartwire outbox stats --rebuild-sla` | Phase 1에서 구현 예정인 CLI 옵션을 미리 문서화 | Redis `alerts:sla` 손실 복구 절차가 없으면 시나리오 A가 불완전 |

## 5. Phase 1 설계 (구현 예정, 계약은 스펙 §7 그대로)

### 5.1 `outbox/poller.py` — `Poller`
- 생성자: `Poller(engine, redis, registry, ctx: HandlerContext, drainer: Drainer, *, worker_id: str, clock, rng, batch=100, tick_s=1.0, tenant_cache_s=30.0, reclaim_every_s=30.0, concurrency=8)`.
- `run()`: `while drainer.accepting:` ① 활성 테넌트 목록(`SELECT id FROM tenants WHERE status='active'`, 30 s 캐시; `tenants`는 RLS 대상이 아니므로 `chartwire_app`으로 직접 조회, 아니면 WP-A `repo/tenancy.py` 사용) ② 테넌트마다 `tenant_tx(engine, TenantCtx(tenant_id, None, "service"))` 안에서 `repo.outbox.claim_batch(session, worker_id, batch, lease_s=...)` — **리스 길이는 이벤트 타입별**이므로 클레임 쿼리는 1개로 유지(`ix_outbox_pending` 사용)하고 클레임 직후 같은 tx에서 `HandlerSpec.lease_s`로 `lease_until`을 재설정 ③ 클레임된 행마다 `drainer.track()`으로 감싼 task 실행(테넌트 내 순서는 `next_attempt_at,id`, 동시성 상한 `asyncio.Semaphore(concurrency)`) ④ `outbox:wake` 구독으로 1 s 대기를 조기 종료 ⑤ 30 s마다 `reclaim_stuck`(테넌트 순회) — `attempts` 불변.
- `_execute(event)`: `spec = registry.get(event.event_type)`(미등록 타입 → `UnknownEventTypeError`를 일반 실패로 처리해 백오프/DLQ 경로로: 배포 순서 문제로 새 이벤트 타입이 먼저 들어와도 유실 없음) → **핸들러 트랜잭션** `tenant_tx(engine, TenantCtx(tenant, None, "service"))`: `mark_processed(handler=spec.name, event_id)`가 `False`면 이미 처리 → 커밋 없이 `mark_done`; `True`면 `await spec.fn(ctx, event)` 후 커밋(핸들러 DB 효과 + `processed_events` 동일 tx) → 별도 짧은 tx에서 `repo.outbox.mark_done` → `HANDLER_DURATION_SECONDS.labels(event_type).observe`. 예외 시 `dlq.on_failure(event, exc, max_attempts=spec.max_attempts, now=clock.now(), rng=rng)`의 `event_updates`/`dead_letter`를 기록 + `HANDLER_FAILURES_TOTAL`, dead면 `OUTBOX_DEAD_TOTAL`. 로그는 `id, event_type, attempts, handler`만(payload 금지).
- 핸들러가 `lease_s`보다 오래 걸릴 위험(파기 검증)에 대비해 `lease_s/2`마다 `lease_until` 연장(하트비트 task). 연장이 0행이면(행이 reclaim됨) 핸들러 task 취소.
- 메트릭 샘플러(10 s): `repo.outbox.stats` → `OUTBOX_PENDING`(테넌트 합), `OUTBOX_LAG_SECONDS`(테넌트 최대).

### 5.2 `outbox/dlq.py` DB 계층 추가
`async def replay(engine, event_id) -> bool`(테넌트 id를 `dead_letters`에서 조회 후 `tenant_tx` 안에서 `repo.outbox.replay_dead`), `async def list_dead(engine, *, tenant_id=None, limit=100)`; `outbox/cli.py`에 `chartwire outbox stats | dlq list | dlq replay --id` (typer) — WP-A `cli.py`의 `LAZY_SUBAPPS`에 마운트 요청(§6).

### 5.3 `worker/main.py`
- `async def run(settings, *, embedded=False) -> int`: `Drainer(deadline_s=25).install()`, `Health(info={role:'worker', node_id})` + 프로브(`SELECT 1`, `PING`), `drainer.on_begin(health.mark_draining)`, `drainer.on_begin(poller.stop_claiming)`; `HandlerContext` 조립(engine, redis, objectstore, SystemClock, settings, LocalKek/AwsKmsKek, KeyCache); 핸들러 모듈 임포트로 `REGISTRY` 채움(`worker/handlers/note_draft`(WP-D), `purge_run`/`purge_verify`(WP-E), 제 `partition_ensure`/`outbox_prune`/`session_reaper`, WP-C `alert_sla`); 티커 태스크 4개(`alert_sla` 1 s, `partition_ensure` 1 h + 시작 직후 1회, `outbox_prune` 10 min, `session_reaper` 60 s) — 공통 `Ticker(name, interval_s, fn, drainer, clock)`(예외는 로그 후 계속, 드레인 시 중단); 메트릭/헬스 HTTP는 별도 포트(`CHARTWIRE_WORKER_PORT`, 기본 9001)의 uvicorn 최소 앱(`/healthz`, `/readyz`, `/metrics`). 종료: `await drainer.wait_drained()` → 리소스 정리 → 0(깨끗) / 1(데드라인 초과).
- `--embedded`(Render): `serve all --embedded`가 api 앱의 lifespan 안에서 `worker.main.run(embedded=True)`과 stt-worker를 태스크로 띄움; embedded면 자체 HTTP 포트를 열지 않고 api의 `/metrics`·`/readyz`를 공유(같은 `ops.metrics.REGISTRY`, 같은 `Health`/`Drainer`), 풀 5, 드레인 예산은 api의 20 s.

### 5.4 티커 핸들러(제 소유)
- `partition_ensure`: `SELECT ensure_segment_partition(m)`(현재+1, +2개월; 함수가 SECURITY DEFINER인지 WP-A 마이그레이션 정의를 따름) → `SELECT count(*) FROM transcript_segments_default`(RLS라 테넌트 순회 합) → `SEGMENTS_DEFAULT_PARTITION_ROWS.set`.
- `outbox_prune`: 테넌트별 `repo.outbox.prune_done(batch=5000)`를 0행이 될 때까지(회당 배치 상한 있음).
- `session_reaper`: 테넌트별 `recording|paused` 세션 → `HGET sess:{sid} updated_at`이 `session_idle_timeout_s`보다 오래됐거나(해시가 없으면 `sessions.updated_at` 기준) → `state='ended'` + `XADD {"end":"1"}` 마커(WP-B `redis/keys.py`·`session_state` 사용) + 감사 `session.ended`.

### 5.5 테스트(Phase 1)
- `tests/integration/test_outbox_poller.py`(`CHARTWIRE_TEST_DB=chartwire_test_g`, `CHARTWIRE_TEST_REDIS_DB=7`): 독약 메시지(FakeClock으로 8회, 다른 행 무영향, `dead_letters` 1행, replay 후 성공), 두 워커 동시 클레임 중복 없음, `processed_events` 충돌 스킵, 리스 만료 reclaim.
- `tests/chaos/test_worker_sigkill.py`: 핸들러가 플래그 파일을 기다리는 동안 서브프로세스 워커 SIGKILL → 리스 만료 → 새 워커가 정확히 1회 효과 완료(효과 카운트 테이블).
- 시나리오 H 벤치: 테넌트 30 vs 300에서 이벤트 처리량 → `docs/loadtest/H.json {events_per_s, dlq_count}` + `chartwire.eval.report.build_report` 헤더.

## 6. 다른 WP에 요청

- **WP-A (`db/repo/outbox.py`)** — `backoff_seconds()`/`mark_failed()`가 `outbox/backoff.py`와 같은 규칙을 **중복 구현**하고 있습니다(현재 값은 일치). 단일 진실을 위해 다음 중 하나를 부탁드립니다: (a) `mark_failed(session, event_id, *, plan: RetryPlan, error: str)` 시그니처로 바꿔 결정은 `outbox.dlq.on_failure`가 하고 repo는 쓰기만 하도록; 또는 (b) `backoff_seconds`를 `from chartwire.outbox.backoff import base_delay_s`로 위임. (a)를 선호합니다. 그때까지 Phase 1 poller는 `dlq.on_failure`의 `event_updates`를 `update(OutboxEvent).values(**updates)`로 직접 쓰고 `mark_dead`만 repo를 사용할 계획입니다. 또한 `mark_done`/`mark_failed`가 `locked_at`을 비우지 않습니다 — `locked_by/lease_until`과 함께 `locked_at=None`으로 맞춰 주세요(제 `done_updates`와 동일).
- **WP-A (`cli.py`)** — `LAZY_SUBAPPS`에 `"outbox": "chartwire.outbox.cli:app"`; `serve worker|all [--embedded]`가 `chartwire.worker.main:run`을 호출하도록 마운트.
- **WP-A (`tests/unit/test_core_outbox_backoff.py`)** — 위 (b)를 택하면 이 테스트는 `test_outbox_backoff.py`와 합칠 수 있습니다.
- **WP-B (`ws/drain.py`)** — `ops.drain.Drainer`를 그대로 쓰시면 됩니다: `drainer.on_begin(send_bye_to_recorders)`, 각 라이브 세션을 `async with drainer.track():`으로 감싸고, 앱 종료 훅에서 `await drainer.wait_drained()`. api의 예산은 `Drainer(deadline_s=20)`.
- **WP-E (`api/routers/ops.py`, `api/app.py`)** — `/metrics`는 `body, content_type = ops.metrics.render()`; `/healthz`는 `health.livez()`, `/readyz`는 `await health.readyz()`(둘 다 `(status_code, body)` 반환). 시작 시 `ops.metrics.bind_db_pool(lambda: engine.pool.checkedout())` 1회.
- **WP-C / WP-D / WP-E 핸들러 작성자** — `from chartwire.outbox.registry import handler` 후 `@handler("session.transcribed", lease_s=120)`처럼 등록. 시그니처 `async def fn(ctx: HandlerContext, event: OutboxEvent) -> None`. `event.payload`는 읽기 전용 매핑, `event.attempts`는 이번 실행 **이전** 실패 횟수. 재시도하지 않을 정상 종료(예: 동의 없음 → abstain)는 예외를 던지지 말고 상태만 기록하세요. 카운터/히스토그램은 `chartwire.ops.metrics`의 객체를 임포트해 쓰고 새로 만들지 마세요(레지스트리 단일).
- **WP-F** — `docs/loadtest/H.json` 키 `{events_per_s, dlq_count}`는 인지했습니다(Phase 1 시나리오 H).

## 7. 알려진 이슈 / 남은 일

- Phase 0 범위(순수 로직 + 문서)는 완료. `poller.py`, `worker/main.py`, `worker/handlers/{partition_ensure,outbox_prune,session_reaper}.py`, `outbox/cli.py`, 통합·카오스 테스트, 시나리오 H는 Phase 1(§5 설계대로).
- `docs/ops/runbook.md` §3-5의 `--rebuild-sla` 옵션은 Phase 1 구현 전까지 문서가 코드보다 앞서 있습니다.
- `disable_created_metrics()`는 prometheus_client 전역 설정입니다(비공개 레지스트리만 쓰므로 영향 없음).
