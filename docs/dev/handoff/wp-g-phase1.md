# WP-G Phase 1 핸드오프 — Worker runtime · outbox poller · ops endpoints · serve CLI

> 상태: **완료** (2026-09-03, 3차 실행에서 마무리). 게이트: `pytest tests/unit/test_outbox_*.py tests/unit/test_ops_*.py tests/unit/test_worker_cli.py tests/integration/test_outbox_*.py tests/integration/test_ops_routes.py tests/chaos -q -p no:xdist` = **98 passed / 0 failed** (19.6 s) · `ruff check` + `ruff format --check` clean · `mypy --strict src/chartwire/outbox` clean (`ops/`, `worker/` 도 mypy clean).
> 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_g CHARTWIRE_TEST_REDIS_DB=7`. 라이브 서버 포트 8106 (`CHARTWIRE_WORKER_PORT=8106`).
> 소유 경로 밖은 건드리지 않았고 git 상태를 바꾸는 명령은 실행하지 않았습니다. 트리의 다른 변경(`ws/*`, `notes/*`, `wp-b/-d` 핸드오프)은 동시 작업 중인 WP-B/WP-D 의 것입니다.

## 1. 무엇을 만들었나 (모듈 지도, Phase 1 신규)

| 경로 | 역할 | 스펙 |
|---|---|---|
| `outbox/runtime.py` | `build_context(settings, pool_size=)`(독립 워커: `chartwire_app` 엔진, Redis, 오브젝트 스토어, `LocalKek`, `KeyCache`; `embedded` 면 풀 5), `context_from_deps(deps)`(api 의 `AppDeps` 공유), `close_context`, `active_tenant_ids(engine)`, **`run_handler(ctx, spec, event)`** — 핸들러 트랜잭션의 유일한 위치: `tenant_tx(service)` 안에서 `processed_events` 삽입(PK 충돌 → `"skipped"`) → `bind_tx` 로 핸들러의 `ctx.tenant_tx()` 가 같은 tx 에 **참여** → 함께 커밋 | §7.1 |
| `outbox/context.py` (+) | `HandlerContext.tenant_tx(tenant_id)` — 폴러 아래서는 열린 핸들러 tx 에 참여(contextvar), 단독(티커/CLI/테스트)에서는 새 tx | §7.1 |
| `outbox/poller.py` | `Poller(ctx, worker_id=, registry=, drainer=, clock=, rng=, batch=100, tick_s=1, tenant_cache_s=30, reclaim_every_s=30, stats_every_s=10, concurrency=8, done_batch=50, tenants=None)`. `run()` = 1 s 틱 또는 `outbox:wake` 구독으로 조기 기동, 드레인 시 클레임 중단 + 진행 중 핸들러 완료. `run_once()` = 30 s 마다 `reclaim_stuck` → 활성 테넌트마다 `claim_batch`(`app.tenant_id` 아래, `FOR UPDATE SKIP LOCKED`, 리스 = 등록된 핸들러 중 최대 `lease_s`) → 동시 실행(세마포어) → 완료분은 **테넌트별 한 tx 로 배치 `mark_done`**(50개 또는 패스 끝) → 실패는 `repo.outbox.mark_failed`(백오프 ±20 %, 8회째 `dead` + `dead_letters`). 미등록 이벤트 타입은 `UnknownEventTypeError` 로 일반 실패 경로(배포 순서 문제로 새 타입이 먼저 와도 유실 없음). `PassStats`/`totals`, 메트릭 `outbox_pending`/`outbox_lag_seconds`/`outbox_dead_total`/`handler_duration_seconds`/`handler_failures_total`. 로그에는 id·event_type·attempts·handler 만 | §7.1, §7.3 |
| `outbox/dlq.py` (+) | DB 계층: `replay(engine, event_id, tenant_id=None)`(테넌트 순회, `dead`→`pending`, `attempts=0`, `replayed_at`), `list_dead`, `stats_all` | §7.1 |
| `outbox/cli.py` | `chartwire outbox stats [--rebuild-sla] · dlq list [--tenant] [--limit] · dlq replay --id [--tenant] · bench --events --workers --tenants --seed --kill-at --out`. `--rebuild-sla` 는 `risk_events` 로부터 `alerts:sla` ZSET 재구축(런북 §3-5). 출력에 payload 없음 | §13.1 |
| `outbox/bench.py` | 시나리오 H: 이벤트 벌크 삽입(`insert_s`, 측정 제외) → **워커 프로세스 K 개**(`python -m chartwire.outbox.bench --worker i …`, 실제 `Poller.run()` + Drainer) → 진행률 `kill_at` 에서 워커 0 에 **SIGKILL** → 생존자의 `reclaim_stuck` 이 회수 → `pending+in_flight = 0` 까지 측정 → SIGTERM 드레인 → `docs/loadtest/H.json {events_per_s, dlq_count, reclaimed, passes, claim_ms_p50/p95, worker_exit_codes, per_worker, …}` + §11.1 헤더. `done` 행은 실행 뒤 정리 | §11.2 H |
| `worker/tickers.py` | `Ticker(name, interval_s, fn, drainer=, run_at_start=)` — 실패는 로그 후 계속, 진행 중 tick 은 드레인 대상, 드레인 시작 시 즉시 중단 | §7.2 |
| `worker/handlers/partition_ensure.py` | 1 h(+시작 직후): `ensure_segment_partition` 현재~+2개월, DEFAULT 파티션 행 수(테넌트 순회 합) → `segments_default_partition_rows` | §7.2 |
| `worker/handlers/outbox_prune.py` | 10 min: 테넌트별 24 h 지난 `done` 행 5,000 배치 삭제(회당 20 배치 상한) | §7.1 |
| `worker/handlers/session_reaper.py` | 60 s: `recording/paused` 세션의 마지막 활동(`sess:{sid}.updated_at`, 없으면 `sessions.updated_at`)이 `session_idle_timeout_s` 초과 → `ended` + 감사 `session.ended` + `{"end":"1","ep":n}` 마커(`SessionState.xadd_end`) + `session.state` 이벤트 | §7.2 |
| `worker/main.py` | `run(settings, *, ctx, drainer, health, http_port, install_signals, poller_kwargs, handler_modules, ready)`: 핸들러 모듈 **guarded import**(`note_draft`(D) `purge_run`/`purge_verify`(E) `alert_sla`(C) + 제 3개; 없으면 로그 후 건너뜀, 모듈 *안*의 ImportError 는 그대로 올림), 티커 4개(`alert_sla` 는 모듈이 없으면 `risk.alerts.escalate_due(ctx, now)` 로 폴백), 폴러, `OpsServer`(uvicorn, 시그널은 Drainer 소유) 로 `/healthz /readyz /metrics` 별도 포트. SIGTERM → 클레임 중단 → 진행 중 완료(≤25 s) → 0 / 예산 초과 1. `run_all(settings, host, port)` = `serve all --embedded` | §7.3, §12.2 |
| `worker/cli.py` | `chartwire serve api|worker|stt-worker|all [--embedded] [--host] [--port] [--log-level]` (`LAZY_SUBAPPS["serve"]`, WP-A 계약대로 callback). `api` → `uvicorn.run(create_app(settings))`, `stt-worker` → `chartwire.stt.worker.main(settings)`; 모듈이 없으면 종료 코드 2 + 한 줄 안내(트레이스백 없음) | §13.1 |
| `ops/routes.py` | `router`(`/healthz` 항상 200, `/readyz` PG+Redis 프로브·드레인 503, `/metrics`), `on_startup(app, deps, role=, deadline_s=20, install_signals=)`, `on_shutdown(app)`, `build_health`, `chain_signals(drainer, chain=)`, `standalone_app` | §6.9, §7.3 |
| `ops/drain.py` (+) | `Drainer.installed` | |
| `docs/ops/runbook.md` | 실제 명령(§4 명령 블록), 종료 코드, SIGKILL/리스 설명, 처리량 조정 지침, 검증 테스트 명령(§7), 로그 이벤트 표 확장 | §14 |

LOC(비공백): Phase 1 신규 구현 1,573 + Phase 0 928 ≈ 2,500 / 테스트 781 + Phase 0 779 ≈ 1,560. 예산(700/400)을 크게 넘습니다 — 이유: `bench.py`(≈300, 측정 하네스) · `session_reaper`/`partition_ensure`(스펙이 티커 3개를 요구) · `main.py` 의 embedded 모드. 순수 폴러+런타임+CLI 는 ≈ 640 입니다.

## 2. 테스트 (98 passed)

```bash
source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_g CHARTWIRE_TEST_REDIS_DB=7
pytest tests/unit/test_outbox_*.py tests/unit/test_ops_*.py tests/unit/test_worker_cli.py -q        # 76, 서비스 불필요
pytest tests/integration/test_outbox_poller.py -q -p no:xdist   # 7  독약→DLQ 정확히 1행(8회, 다른 행 무영향, replay), 크래시 창 재전달 skip, 효과+원장 동반 롤백, 리스 reclaim(attempts 불변), 두 폴러 40건 중복 0, unknown type 재시도 경로, wake+drain 루프, 게이지
pytest tests/integration/test_outbox_tickers.py -q -p no:xdist  # 6  Ticker 격리/드레인, prune 24 h, reaper(마커·감사·Redis updated_at), partition_ensure
pytest tests/integration/test_outbox_cli.py -q -p no:xdist      # 3  stats/--rebuild-sla, dlq list/replay(typer runner), bench 2 K × 2 프로세스 + SIGKILL → 리포트 형태·exit codes [-9, 0]·reclaimed ≥ 1·정리
pytest tests/integration/test_ops_routes.py -q -p no:xdist      # 5  실제 PG/Redis 프로브, 드레인 503, 의존성 실패 503(프로브 이름), on_startup 재사용, SIGTERM 체인, chain=False/프로그램적 begin
pytest tests/chaos -q -p no:xdist                                # 1  실제 서브프로세스 워커 SIGKILL(핸들러 중) → in_flight 유지 → 두 번째 워커 reclaim 후 정확히 1회 효과(audit 1, processed_events 1, attempts 0) → SIGTERM exit 0
ruff check src/chartwire/outbox src/chartwire/ops src/chartwire/worker tests/...; mypy --strict src/chartwire/outbox
```

라이브 스모크(수동, 이번 실행): `CHARTWIRE_WORKER_PORT=8106 chartwire serve worker` → `/healthz` 200 `{"status":"ok","role":"worker",…}`, `/readyz` 200 `{"checks":{"postgres":"ok","redis":"ok"}}`, `/metrics` 에 `outbox_pending 0.0`·`segments_default_partition_rows 0.0`, 로그 `partitions ensured`, SIGTERM → `drain started` → `worker stopped` → **exit 0**. `chartwire outbox stats` 정상 출력.

## 3. 이번 실행에서 잡은 결함 (전부 실제 실행/테스트가 발견)

| 결함 | 어떻게 드러났나 | 수정 |
|---|---|---|
| 드레인 후 이전 시그널 핸들러를 **모든** 시그널에 대해 재호출 → 독립 워커(`asyncio.run`)에서는 SIGINT 의 이전 핸들러가 asyncio Runner 의 `_on_sigint` 라서 깨끗한 드레인이 `KeyboardInterrupt`(exit -2)로 끝남 | chaos 테스트 생존자 `exit 0` 단정 실패 | `chain_signals(drainer, chain=)`: **실제로 온 시그널**(`drainer.reason`)의 이전 핸들러만, 프로그램적 `begin()` 은 체이닝 없음; 워커는 `chain=False`(스스로 종료) — 회귀 테스트 추가 |
| `log.info(..., extra={"module": …})` — `module` 은 LogRecord 예약 필드 → 핸들러 모듈이 하나라도 없으면 워커 기동 시 `KeyError` | 라이브 스모크 첫 실행 | `handler_module` 로 변경 + 단위 테스트 |
| uvicorn 0.52 에서 `Server.startup()` 직접 호출 불가(`lifespan` 은 `_serve()` 가 세팅) → ops HTTP 서버가 `AttributeError` | 라이브 스모크 두 번째 실행 | `OpsServer(capture_signals 무효화)` + 공개 `serve()` 를 태스크로; 종료는 `should_exit` |
| `chartwire serve api --port 8106` 처럼 role 뒤의 옵션이 "No such command" | CLI 단위 테스트 | typer 앱 `context_settings={"allow_interspersed_args": True}` |
| 벤치 "워커"가 한 프로세스 안의 태스크 → 워커 수를 늘려도 처리량 불변(CPU 1개, ~230 ev/s) | `outbox bench` 프로파일링(1 vs 2 워커 동일) | 워커를 **프로세스**로, 크래시는 실제 SIGKILL; 이벤트별 `mark_done` tx 를 배치화 → 같은 박스에서 2 프로세스 554 ev/s (2 K, 5 테넌트, kill 없음; 참고값일 뿐 README 수치 아님) |

## 4. 스펙과 다른 점 / 스펙이 침묵한 곳에서의 선택

1. **`mark_done` 배치** — §7.1 "then `mark_done`" 을 이벤트마다 별도 tx 대신 완료 순서대로 50개 또는 패스 끝에 테넌트별 한 tx 로 씁니다. 크래시 창(핸들러 커밋 뒤 `mark_done` 전)이 조금 넓어지지만 그 창의 의미는 동일합니다: 리스 만료 → 재전달 → `processed_events` 충돌 → skip. 테스트 `test_redelivery_after_crash_before_mark_done_is_skipped` 가 그 경로를 고정합니다.
2. **리스 하트비트 없음** — 핸들러가 `lease_s` 를 넘기면 다른 워커가 같은 행을 잡을 수 있지만, 그 워커의 `processed_events` INSERT 는 첫 tx 의 커밋/롤백까지 PK 에서 **블록**된 뒤 충돌/성공하므로 효과는 정확히 한 번입니다. 긴 핸들러는 `@handler(..., lease_s=)` 로 리스를 늘리세요(`purge_verify` 권장 300).
3. **클레임 리스 = 등록된 핸들러 중 최대 `lease_s`** — 클레임 쿼리를 테넌트당 1개로 유지하기 위해(스펙의 `ix_outbox_pending` 단일 쿼리). 타입별로 다른 리스는 §7.1 의 크래시 회복 시간에만 영향.
4. **미등록 이벤트 타입 = 일반 실패** (백오프 → 8회 뒤 DLQ, replay 가능) — 유실 대신 가시성. 핸들러 배포 뒤 `dlq replay`.
5. **`session_reaper` 마지막 활동** — 스펙은 `sess:{sid}.updated_at` 만 언급; 해시가 없으면(Redis 손실) `sessions.updated_at` 으로 폴백해 유휴 세션이 영원히 살아남지 않게 했습니다.
6. **`partition_ensure` 는 현재 월도 포함**(현재, +1, +2) — 멱등이라 비용 없음, 첫 기동 시 보험.
7. **워커 HTTP 포트** `CHARTWIRE_WORKER_PORT`(기본 9001; `0` 이면 없음) — 스펙이 포트를 정하지 않음. embedded 모드는 api 포트의 라우트를 공유(별도 서버 없음).
8. **`serve all` 은 `--embedded` 가 없어도 embedded 로 실행**(안내 문구 출력) — 스펙상 `all` 의 유일한 형태.
9. **벤치의 이벤트 삽입은 SQLAlchemy Core 벌크 INSERT**(`outbox/bench.py`, 1,000행 청크) — repo 에 벌크 함수가 없고 100 K 건을 `insert_event` 로 넣으면 삽입이 측정을 지배합니다. 런타임 코드가 아닌 측정 하네스이므로 여기 두었습니다(§5 요청 참조).
10. **`chain_signals` 의 체이닝 대상** — api 에서만(uvicorn `handle_exit`), 그 시그널 하나만. uvicorn 은 종료 후 잡았던 시그널을 `raise_signal` 로 되돌리므로 api 의 SIGTERM 종료 코드는 143 입니다(uvicorn 표준 동작; 컨테이너 런타임은 정상 종료로 취급).

## 5. Phase 2 통합자 / 다른 WP 에 요청

- **WP-A (`db/repo/outbox.py`)** — (a) `mark_done_many(session, ids, *, now)` 한 UPDATE 로 배치 완료(지금은 한 tx 안에서 `mark_done` 을 N 번 호출). (b) `insert_events(session, rows)` 벌크 삽입(벤치의 Core INSERT 를 repo 로 이동). (c) Phase 0 요청 유지: `mark_done/mark_failed/reclaim_stuck` 이 `locked_at` 도 NULL 로 — 현재 `locked_by/lease_until` 만 지웁니다(정확성 영향 없음, ops 뷰 가독성).
- **WP-E (`api/app.py`)** — 계약대로 `chartwire.ops.routes.router` + `on_startup(app, deps)`(lifespan startup) / `await on_shutdown(app)`. `on_startup` 은 `app.state.drainer/health` 가 이미 있으면 재사용하고, `drainer.installed` 면 시그널을 다시 설치하지 않습니다(embedded 러너가 먼저 설치). api 의 드레인 예산은 기본 20 s. `run_all` 은 `app.state.deps` 에서 `context_from_deps` 로 컨텍스트를 공유합니다(없으면 자체 생성).
- **WP-B (`ws/routes.py`)** — 드레이너를 직접 만들지 말고 `app.state.drainer`(ops.on_startup 이 만듦, 또는 먼저 만들어 두면 ops 가 재사용)를 쓰세요: `drainer.on_begin(...)`, `async with drainer.track()`.
- **WP-C (`worker/handlers/alert_sla.py`)** — `INTERVAL_S` 와 `async def tick(ctx)` 를 노출하면 그대로 티커에 붙습니다(없으면 `risk.alerts.escalate_due(ctx, now)` 폴백).
- **WP-D/E 핸들러** — `worker/handlers/{note_draft,purge_run,purge_verify}.py` 는 import 만으로 등록됩니다(`from chartwire.outbox.registry import handler`). DB 효과는 반드시 `async with ctx.tenant_tx(event.tenant_id) as session:` 안에서(폴러 tx 참여 → `processed_events` 와 동반 커밋). `purge_verify` 는 `lease_s=300` 권장.
- **WP-H / 통합자 (Phase 2 측정)** — `chartwire outbox bench --events 100000 --workers 2 --tenants 30` 과 `--tenants 300` 을 유휴 박스에서 실행해 `docs/loadtest/H.json` 을 만드세요(이번 실행은 스크래치 경로에만 썼고 `docs/loadtest/` 에는 파일을 남기지 않았습니다 — 측정은 직렬 창). CI integration 잡에 `tests/chaos` 추가(≈15 s).
- **WP-A (`Makefile`/`docker-compose.yml`)** — worker 서비스에 `CHARTWIRE_WORKER_PORT` 노출(HEALTHCHECK `/healthz`).

## 6. 알려진 이슈 / 남은 일

- `run_all`(embedded) 은 `chartwire.api.app.create_app` 이 아직 없어 end-to-end 로 실행하지 못했습니다(코드 경로는 단위 수준: import 가드·시그널 순서·`should_exit` 종료). WP-E 통합 뒤 `chartwire serve all --embedded --port 8106` 로 확인 필요.
- 벤치 2 K 실행에서 kill 이 있으면 리스(5 s)+reclaim 주기 대기가 elapsed 의 절반을 차지합니다 — 100 K 에서는 무시 가능; 리포트의 `reclaimed`/`worker_exit_codes` 로 크래시 경로가 실제로 돌았는지 확인하세요.
- `Poller.tenants()` 는 활성 테넌트 전부를 순회하므로 개발 DB 에서 벤치를 돌리면 다른 테넌트의 pending 이벤트도 벤치 워커가 집습니다(벤치 워커는 `tenants=` 로 고정해 이를 막음; 개발 DB 에서 일반 워커와 벤치를 동시에 돌리지 마세요).
- `outbox_events.locked_at` 은 WP-A repo 가 완료/실패 시 지우지 않습니다(위 요청).
