# WP-B 핸드오프 — Phase 1: ingest/watch 비동기 셸, Redis 상태, 원장 배처, WS 라우트, e2e

> 상태: **진행 중** (2026-09-03). 중단되면 아래 체크리스트 순서대로 이어서 작업한다. 파일은 작성 즉시 디스크에 있다.
> 실행 환경: `set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_b CHARTWIRE_TEST_REDIS_DB=2`

## 0. 계획 / 진행 체크리스트

- [ ] `redis/scripts/hello.lua`, `redis/scripts/xadd_chunk.lua`
- [ ] `redis/session_state.py` — `SessionState(redis)`: hello/get/set_fields/xadd_chunk/xadd_end/rehydrate/expire_after_end/stt_lag
- [ ] `redis/tickets.py` — `issue(...)`, `consume(...)`, `TicketPayload`
- [ ] `ws/ledger.py` — `LedgerBatcher`, `ChunkRow`
- [ ] `ws/pubsub.py` — 프로세스당 하나의 PubSub 리더(`SubscriberManager`), 세션 채널 refcount
- [ ] `ws/ingest.py` — `IngestConnection` (단일 태스크 이벤트 루프 + store worker)
- [ ] `ws/watch.py` — `WatchConnection` (partial_q/critical_q, sender 태스크)
- [ ] `ws/drain.py` — `ConnectionRegistry` (bye{drain} 방송, 20 s 대기)
- [ ] `ws/routes.py` — `router`, `on_startup(app, deps)`, `on_shutdown(app)`
- [ ] `messages.py`: `risk_event_id` 를 `int` 로 (DB `risk_events.id` 는 bigint identity)
- [ ] `tests/ws/_live.py` — uvicorn(8101) 스레드 픽스처 + 테스트 클라이언트 헬퍼
- [ ] `tests/integration/test_ws_state.py` — SessionState/tickets/LedgerBatcher (실제 Redis/PG)
- [ ] `tests/integration/test_ws_ingest.py` — happy path 300 청크, 강제 kill → resume, superseded, 4009, 4011, FLUSHDB → 4503 → 재수화
- [ ] `tests/integration/test_ws_watch.py` — replay+live 무결, presence, risk.ack
- [ ] `tests/ws/test_watch_queues.py` — 느린 뷰어 격리 단위 테스트
- [ ] `docs/protocol.md` 갱신
- [ ] ruff/mypy, 최종 테스트 실행, 이 문서 완성
