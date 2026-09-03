# WP-G Phase 1 핸드오프 — Worker runtime · ops endpoints · serve CLI

> 상태: **진행 중** (2026-09-03). 중단되면 아래 체크리스트 순서대로 이어서 작업하세요.
> 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_g CHARTWIRE_TEST_REDIS_DB=7`
> 라이브 서버 포트 8106, 워커 HTTP 포트는 테스트에서 끔(`http_port=None`).

## 계획 / 체크리스트

- [x] `outbox/context.py` — `HandlerContext.tenant_tx(tenant_id)` (핸들러 tx 참여, contextvar), `bind_tx()`
- [x] `outbox/runtime.py` — `build_context(settings)`, `context_from_deps(deps)`, `close_context(ctx)`, `run_handler(ctx, spec, event)` (tx + processed_events + commit)
- [x] `outbox/poller.py` — `Poller` (테넌트 순회 claim_batch, 리스 타입별 조정, 동시 실행, 실패 → repo.mark_failed, stuck reclaim 30 s, `outbox:wake` 구독 + 1 s 폴, 메트릭 샘플러)
- [x] `outbox/dlq.py` 확장 — `replay(engine, event_id)`, `list_dead(engine, tenant_id=None, limit)`, `stats_all(engine)`
- [x] `outbox/cli.py` + `outbox/bench.py` — `chartwire outbox stats [--rebuild-sla] | dlq list | dlq replay --id | bench --events --workers --tenants --out`
- [x] `worker/tickers.py` — `Ticker`
- [x] `worker/handlers/partition_ensure.py`, `outbox_prune.py`, `session_reaper.py`
- [x] `worker/main.py` — `run(settings, *, embedded, drainer, health, http_port)`, `run_all(settings, host, port)`, 핸들러 모듈 guarded import
- [x] `worker/cli.py` — `chartwire serve api|worker|stt-worker|all [--embedded] [--host] [--port]`
- [x] `ops/routes.py` — `/healthz /readyz /metrics`, `on_startup(app, deps)`, `on_shutdown(app)`, uvicorn SIGTERM 체이닝
- [ ] `tests/integration/test_outbox_poller.py` — 독약, 멱등 재전달, 리스 reclaim, 두 폴러, unknown event type
- [ ] `tests/integration/test_outbox_cli.py` — dlq list/replay, bench 2K
- [ ] `tests/integration/test_ops_routes.py`
- [ ] `tests/chaos/test_worker_sigkill.py`
- [ ] `docs/ops/runbook.md` 실제 명령으로 갱신
- [ ] ruff / ruff format / mypy --strict src/chartwire/outbox 클린, 최종 테스트 수 기록

## 진행 로그
- 구현 모듈 전부 작성, ruff/mypy(strict outbox) 클린. 다음: 통합·카오스 테스트 작성/실행.
- 시작: 핸드오프 8개·스펙 §0–3, §5, §6.9, §7, §12, §15 및 Phase 0 코드(outbox/ops/repo/conftest) 읽음. 설계 확정(아래 §설계).

## 설계 메모 (구현 중 참조)
- 핸들러 서명은 Phase 0 그대로 `async def fn(ctx, event) -> None`. 핸들러의 DB 작업은 `async with ctx.tenant_tx(event.tenant_id) as session:` 안에서 — 폴러 아래에서는 contextvar 로 폴러가 연 트랜잭션에 **참여**하므로 `processed_events` 와 같은 커밋. 단독(테스트/CLI)에서는 새 tx.
- 리스 하트비트 없음: 리스가 만료돼 다른 워커가 같은 행을 잡아도 `processed_events` PK 삽입이 첫 tx 의 커밋/롤백까지 **블록**된 뒤 충돌 → 스킵. 정확성은 PG 유니크 인덱스가 보장.
- 실패 경로는 WP-A `repo.outbox.mark_failed`(백오프+DLQ 를 repo 가 씀); `outbox/backoff.py`·`dlq.on_failure` 는 규칙의 순수 명세로 남김(단위 테스트가 두 구현의 일치를 고정).
