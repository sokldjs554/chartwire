# WP-B 핸드오프 — Phase 1: ingest/watch 비동기 셸, Redis 상태, 원장 배처, WS 라우트, e2e

> 상태: **완료** (2026-09-03, 재개 2회차에서 마무리). 게이트 결과는 §7. 중단되면 §0 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_b CHARTWIRE_TEST_REDIS_DB=2`
> 라이브 서버 포트 8101, 테스트 DB `chartwire_test_b`, Redis 인덱스 2.

## 0. 체크리스트 (재개용)

- [x] `redis/scripts/hello.lua`, `redis/scripts/xadd_chunk.lua`
- [x] `redis/session_state.py` — `SessionState(redis)`: hello/get/set_fields/xadd_chunk/xadd_end/rehydrate/expire_after_end/stt_lag/publish_event/publish_ctl/viewer_join/viewer_leave
- [x] `redis/tickets.py` — `issue(...)`, `consume(...)`, `TicketPayload`
- [x] `ws/ledger.py` — `LedgerBatcher`, `ChunkRow`, SQL 로 검증되는 `sessions.ack_seq` 갱신
- [x] `ws/pubsub.py` — 프로세스당 하나의 PubSub 리더(`SubscriberManager`), 세션 채널 refcount
- [x] `ws/ingest.py` — `IngestConnection` (단일 태스크 이벤트 루프 + store worker)
- [x] `ws/watch.py` — `WatchConnection` (`OutboundQueues`: partial_q/critical_q, sender 태스크)
- [x] `ws/drain.py` — `ConnectionRegistry` (bye{drain} 방송, 20 s 대기)
- [x] `ws/routes.py` — `router`, `on_startup(app, deps)`, `on_shutdown(app)`, 노드 heartbeat, Drainer 훅
- [x] `ws/runtime.py`, `ws/_nometrics.py` — 공유 런타임, ops 패키지 없이도 import 되는 메트릭 대체물
- [x] `messages.py`: `risk_event_id` 를 `int` 로 (DB `risk_events.id` 는 bigint identity)
- [x] `tests/ws/_live.py` — uvicorn(8101) 스레드 픽스처 + 프로토콜 준수 녹음기 클라이언트 + 뷰어 헬퍼
- [x] `tests/integration/test_ws_state.py` — SessionState/tickets/LedgerBatcher (실제 Redis/PG)
- [x] `tests/integration/test_ws_ingest.py` — happy path 300 청크, 강제 kill → resume ×3, superseded, stale epoch, 4009, 4011, 4012, FLUSHDB → 4503 → 재수화, pause/resume_rec, ctl consent_revoked/purge
- [x] `tests/integration/test_ws_watch.py` — replay+live 무결(중복·틈 메우기), from_seq, 경보 중복 제거·risk.ack·presence, 정지된 뷰어 격리(4013)
- [x] `tests/integration/test_ws_drain.py` — bye{drain} → 원장 플러시 → 1012, 드레인 노드의 신규 접속 거절
- [x] `tests/ws/test_watch_queues.py` — 큐 격리 단위 테스트
- [x] `docs/protocol.md` §4.2/§4.9/§5/§9 갱신
- [x] ruff / ruff format / mypy --strict(core, codec), 최종 테스트 실행, 이 문서

## 1. 만든 것 (모듈 맵)

| 모듈 | 내용 | 스펙 |
|---|---|---|
| `ws/ingest.py` (≈590줄) | `IngestConnection`: 연결당 **메인 태스크 하나**가 인바운드 큐를 소비하며 `IngestCore` 를 호출하는 유일한 주체. 생산자: 소켓 수신, 200 ms 틱(`stt:lag` 는 1 s 마다), 프로세스 공용 pub/sub 콜백(`ctl`), 원장 future 콜백(`ledgered`, 커밋 묶음을 한 번의 `on_ledgered` 로 합침), 저장 워커(`Store` 순서 그대로 sha256 → `Envelope.encrypt`(DEK 는 `KeyCache`) → `objectstore.put` → `xadd_chunk.lua` 3회 재시도 → `LedgerBatcher.submit`). handshake: 5 s 내 hello → 티켓 GETDEL(4001) → 세션 로드(4004/4012) → 동의 `recording`(4011) → DEK(파기됨 4012) → ctl 구독 → 해시 없으면 `rehydrate` → `hello.lua` → `sessions.epoch` + 감사 `ws.hello` → `on_hello`. `Transition('ended')` = 엔드 마커 XADD → `sessions.state/final_seq/ended_at` + 감사 `session.ended` → `session.state` 발행 → 키 TTL 24 h. ack 마다 `sess:{sid}` 에 `ack_seq/ledger_seq/credit` 미러링 | §6.1, §6.4 |
| `ws/watch.py` (≈460줄) | `WatchConnection` + `OutboundQueues`(§6.7 격리: partial 256 → 가장 오래된 것 폐기 + `viewer.lagged` 5 s 에 한 번, critical 1024 → `4013`). hello 순서 SUBSCRIBE → `welcome{state, last_final_seq}` → DB 재생(100행 배치, 열린 경보 포함, DEK 로 복호화) → `on_replay_done` → 라이브. 틈 메우기 재생이 0행이면 200 ms 대기. `risk.ack` 는 REST ack 와 같은 효과(뷰어 컨텍스트 tx + 감사 + ZREM + 발행). presence SET + `viewer.presence` 발행. `4013` 시 막힌 sender 를 취소하고 close 프레임에 5 s 상한 | §6.7 |
| `ws/ledger.py` (≈230줄) | `LedgerBatcher`: 첫 행이 50 ms 창을 열고 500 행이면 즉시 플러시. 테넌트별 한 트랜잭션(`insert_chunks` ON CONFLICT DO NOTHING + `_raise_ack_stmt`). **`sessions.ack_seq` 는 `(이전 ack, 힌트]` 구간의 행을 같은 트랜잭션에서 전부 셀 수 있을 때만 올라간다** — 힌트는 검증 대상이지 진실이 아니다. 실패 시 배치의 모든 future 에 `LedgerError`(셸은 4503). 메트릭 `ledger_flush_seconds/rows/pending_rows` | §6.6, §0 규칙 3 |
| `ws/pubsub.py` (142줄) | `SubscriberManager`: 프로세스당 PubSub 연결 하나, 세션별 `sess:{sid}:events` + `ctl:{sid}` refcount 구독, 콜백은 큐에 넣기만 함. Redis 유실 시 모든 콜백에 `viewer.degraded` 방송 + 백오프 재접속·재구독 | §6.7, §5 |
| `ws/drain.py` (69줄) | `ConnectionRegistry`: `begin()` 이 모든 연결에 drain 이벤트, `wait_closed(20 s)`, 드레인 중 `add()` 된 연결도 bye 를 받음 | §6.4 규칙 7 |
| `ws/routes.py` (≈150줄) | `/ws/v1/ingest`, `/ws/v1/watch`; `on_startup(app, deps)` 가 `WsRuntime` 조립(`SessionState`, `LedgerBatcher`, `SubscriberManager`, `ConnectionRegistry`), `app.state.ws_runtime`/`app.state.ledger`, `node:{node_id}` heartbeat(10 s, TTL 30 s), `app.state.drainer`(없으면 `ops.drain.Drainer(20 s)` 를 만들어 둠) 에 `registry.begin` 을 `on_begin` 훅으로 등록. `on_shutdown` = begin → ≤20 s 대기 → 배처 플러시·정지 → pub/sub 정지 → node 키 삭제. 드레인 중 신규 접속은 `1012` 로 즉시 거절 | §6.1, §7.3 |
| `redis/session_state.py` (163줄) | `SessionState`: `hello`(Lua), `xadd_chunk`(Lua, `False`=stale, `StateLost`=해시 없음), `xadd_end`(`{"end":"1","ep":n}`), `rehydrate`(`HSETNX epoch` 가드), `expire_after_end`, `stt_lag`, `publish_event/ctl`, `viewer_join/leave`. 스크립트는 `register_script`(NOSCRIPT 자동 재로드) | §5 |
| `redis/scripts/hello.lua` | `HINCRBY epoch` → `HSET node/conn/updated_at` → `PERSIST` → `PUBLISH ctl superseded{epoch}` → `XGROUP CREATE stt 0 MKSTREAM`(pcall) → `SADD stt:active` → `{epoch, HGETALL}` | §5 |
| `redis/scripts/xadd_chunk.lua` | `HGET epoch` 비교 → 다르면 0(XADD 없음), 해시 없으면 −1, 같으면 `XADD MAXLEN ~ N` + `updated_at` | §5 |
| `redis/tickets.py` (76줄) | `issue(redis, *, tenant_id, user_id, role, session_id, kind, ttl_s=30) -> str`(`secrets.token_urlsafe(32)`), `consume(redis, ticket) -> TicketPayload | None`(GETDEL). UUID 가 아닌 dev `sub` 는 `TicketPayload.sub` 로 보존 | §5, §6.1 |
| `tests/ws/_live.py` | `live_server()`(uvicorn 스레드, `AppDeps` 와 같은 모양의 `SimpleNamespace` 를 `app.state.deps` 에, 루프를 `app.state.loop` 에), `seed_session()`(실제 래핑 DEK + 동의), `Recorder`(credit 규칙·nack 재전송·pong·resume·`close_hard()` = TCP abort), `open_viewer/viewer_recv` | §15 |

Phase 0 코어(`core.py`, `codec.py`, `messages.py`, `actions.py`, `credit.py`)는 변경 없음(mypy strict 유지).

## 2. 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_b CHARTWIRE_TEST_REDIS_DB=2
python -m pytest tests/ws -q                                   # 149 (144 Phase 0 + 5 큐 격리), DB/Redis 불필요, ≈28 s
python -m pytest tests/integration/test_ws_*.py -q -p no:xdist # 35, 실제 PG/Redis + uvicorn 8101, ≈55 s
ruff check src/chartwire/ws src/chartwire/redis tests/ws tests/integration/test_ws_*.py
ruff format --check src/chartwire/ws src/chartwire/redis tests/ws tests/integration/test_ws_*.py
mypy --strict src/chartwire/ws/core.py src/chartwire/ws/codec.py
```

통합 테스트 파일은 **직렬**로 돌려야 한다(8101 포트 하나). `test_ws_drain.py` 는 드레인한 서버를 버리므로 자체 모듈 픽스처를 쓴다.

| 파일 | 게이트 | 검증 내용 |
|---|---|---|
| `test_ws_ingest.py` (19) | happy path 300 청크 → `bye{ended, 300}` + 1000, `audio_chunks` 1..300, 샘플 복호화 == 전송 바이트, 스트림 300 + 엔드 마커, 해시 TTL, 감사 `ws.hello`/`session.ended` · ack 가 원장을 앞서지 않음(매 ack 시점 DB 조회) · **3 세션 동시 TCP abort → resume, 손실/중복 0** · 4008 · superseded 4409 · stale epoch 펜싱(스트림·원장 미도달, ack 없음) · 4009 · 4011 · 4001/4004/4005/4010/4012 · 티켓 1회용 · hello 타임아웃 · **FLUSHDB → 4503(ack 없음) → 재접속 시 epoch 2 로 재수화, `missing=[[41,45]]`, 소비자 그룹 복구** · pause/resume_rec · ctl consent_revoked → `bye` + 4011 · ctl purge → 4012 |
| `test_ws_watch.py` (6) | DB 200행 재생 중 라이브 중복(150–199)·신규(200–299)·지연 중복 발행 → 뷰어 final 정확히 0..299 · from_seq · 열린 경보 재생, 경보 재발행 중복 제거, `risk.ack` → DB `acknowledged_by` + `alerts:sla` ZREM + 두 뷰어에 `risk.ack{by}`, presence 1→2→1→0 · 티켓 kind 검사 · 녹음기 종료가 뷰어에 `session.state{ended}` · **정지된 뷰어(4 KB rcvbuf, 읽지 않음)와 건강한 뷰어: 건강한 쪽은 300 final 전부 순서대로, 정지된 쪽은 엄격한 접두사만 받고 4013, `ws_dropped_partials_total` 증가** (4013 으로 죽는 뷰어는 critical 큐에 갇힌 `viewer.lagged` 를 보지 못한다 — 통지 의미론은 `test_watch_queues.py`) |
| `test_ws_drain.py` (1) | 40 청크 중 일부만 durable 인 상태에서 `registry.begin()` → `bye{drain, ack_seq}` 한 번 → ack 40 까지 → 1012, 원장 40행, 세션 `recording` 유지, 뷰어 `bye{drain}` + 1012, 신규 접속 1012 |
| `test_ws_state.py` (9) | 티켓 GETDEL, dev sub · hello.lua epoch/superseded/그룹/`stt:active` · xadd_chunk stale/StateLost/엔드 마커/TTL · rehydrate HSETNX 가드 · 배처: 테넌트 그룹화·future 커밋 후 해결·중복 무시·행 상한 즉시 플러시·**실패 배치는 예외, 구멍 위로 ack 가 뛰지 않고 채워지면 전진** |
| `test_watch_queues.py` (5) | partial 폐기·lag 통지 5 s 규칙, critical 초과 → False, sender 우선순위/대기, 꽉 찬 critical 은 통지도 못 실음 |

## 3. 이번 재개에서 고친 결함 (근본 원인)

| 증상 | 원인 | 수정 |
|---|---|---|
| `test_ws_state` 배처 테스트가 영원히 멈춤 | `LedgerBatcher.submit()` 이 행 상한(500)에서만 배처를 깨웠다. 배처가 한 번 유휴 대기에 들어가면 그 뒤의 행은 500개가 쌓일 때까지 커밋되지 않았다(실서비스라면 첫 연결 뒤 ack 가 끊긴다). 창 대기 직전 `clear()` 가 상한 wake 도 지웠다 | 첫 행이 창을 연다(`len == 1` 에서 wake), 창 대기 전에 상한을 확인, 상한/stop 만 wake 를 다시 건다 |
| 이론상 데이터 손실: 실패한 배치 뒤 새 hello 가 억제를 풀면 아직 대기 중이던 옛 연결의 힌트가 구멍 위로 `sessions.ack_seq` 를 올릴 수 있었다(`welcome.ack_seq` 부풀림 → 녹음기가 링 버퍼를 버림) | 힌트를 신뢰하는 `GREATEST(ack_seq, hint)` + 프로세스 내 억제 집합 | `_raise_ack_stmt` 가 같은 트랜잭션에서 `(ack_seq, hint]` 행 수를 세어 일치할 때만 갱신. 억제 집합·`reset_session` 제거(코드가 줄고 불변식이 SQL 에 있음) |
| 정지된 뷰어 e2e 가 "격리 실패" 처럼 보임 | 테스트 결함: `"가" * 65536` 은 permessage-deflate 로 수십 바이트가 되어 커널 버퍼가 39 MB 를 삼켰다(uvicorn `websockets-sansio` 의 `pause_writing` 백프레셔는 정상 동작함 — 비압축 페이로드로 실측 4–6 MB 에서 차단) | 메시지마다 `secrets.token_urlsafe(49152)`(비압축) |
| 뷰어 큐 초과 시 `ws.close()` 가 막힌 소켓 뒤에서 무기한 대기 가능 | close 프레임도 같은 전송 버퍼를 탄다 | `_close` 가 sender 태스크를 먼저 취소하고 `wait_for(close, 5 s)` |
| 갭 메우기 재생이 핫 루프가 될 수 있음(발행자가 커밋 전에 발행하면) | `on_replay_done` 이 보류분이 남아 있는 한 `Replay` 를 다시 냄 | 갭 메우기 재생이 0행이면 200 ms 뒤에 `replay_done` |
| stale epoch e2e 가 경합에 따라 실패 | 이전 연결의 프레임이 새 hello 의 `hello.lua` 보다 먼저 도착하면 정당한 epoch 1 항목 | Redis `HSET epoch 2`(다른 노드의 hello 를 흉내, 통지는 보류) 로 창을 결정론적으로 만든 뒤 프레임 → 스트림·원장·ack 모두 없음 → 통지 발행 → 4409 |

## 4. 스펙·계약과 다르게 한 점 (이유)

1. **`sessions.ack_seq` 갱신을 SQL 로 검증** — §6.6 은 `GREATEST(ack_seq, :v)` 한 문장. 구현은 `(ack_seq, hint]` 행 수 검사를 붙인다(위 §3). 정상 경로의 비용은 배치당 세션마다 PK 범위 인덱스 스캔(ack 이후 행 수 ≤ credit) 하나.
2. **ack 는 원장 배치당 최대 하나** — `on_ledgered` 가 커밋 묶음 단위로 호출되므로 credit 을 가득 채우는 녹음기는 ≈credit 청크마다 ack 를 받는다(8개 규칙은 ack 를 *미루지 않는* 하한). `docs/protocol.md` §4.2 에 명시. happy path 테스트의 ack 개수 기대치를 이에 맞춤.
3. **Redis 저장 경로 실패 정책** — §5 "3회 연속 실패 후 닫기" 를 "XADD 3회 재시도(100/200/300 ms) 후 4503, 원장 실패는 즉시 4503" 으로 구체화. 실패한 청크는 이 연결에서 다시는 ack 될 수 없으므로 연결을 유지할 이유가 없다.
4. **`Rehydrate` 액션은 셸이 hello.lua 전에 처리** — epoch 가 PostgreSQL 값 위에서 이어져야 옛 연결이 계속 펜싱된다. 코어가 내는 `Rehydrate` 는 순서 기록용.
5. **`hello.lua` 가 `XGROUP CREATE … 0 MKSTREAM`** — 스펙의 `$` 대신 `0`: 워커가 그룹 생성 전에 들어온 항목을 놓치지 않도록(stt-worker 가 FLUSHALL 뒤 만드는 그룹은 §7.4 대로 `$` + 원장 rebuild).
6. **`WatchCore.on_hello(from_seq, *, state, last_final_seq)`** — Phase 0 시그니처 확장(키워드). `welcome.state/last_final_seq` 는 DB 에서 읽는다.
7. **`stt:lag` 는 1 s 마다 읽음**(틱은 200 ms) — Redis 왕복을 연결당 5/s 로 제한. 원장 적체는 로컬 값이라 매 틱.
8. **`ws.hello` 감사 `detail`** = `{kind, epoch, resume, node}` — 스펙 `ws.hello{kind}` 의 상위 집합(PHI 키 없음).
9. **`api/routers/sessions.py` ws-ticket** — Phase 1 계약대로 WP-E 소유. 호출 형태는 §6 참조.
10. **뷰어 `risk.ack`** 은 REST `POST /v1/alerts/{id}/ack` 와 같은 효과를 셸이 직접 낸다(WP-E 의 서비스 함수가 아직 없어 repo 계층 사용). WP-E 가 `alerts.service.acknowledge(...)` 를 만들면 §6 요청대로 교체.
11. `watch._load_session` 의 `last_final_seq` 조회는 Core 쿼리 한 개를 셸에 둔다(repo 에 없음, §6 요청 참조).
12. LOC — ws+redis 구현 ≈2,470(비공백, Phase 0 ≈990 포함) / 테스트 ≈2,250. §0 예산(1,500/900)을 넘는다. 셸 두 개(≈1,050)와 e2e 테스트가 대부분이며, 절단 후보는 `_live.py` 의 뷰어 헬퍼 통합과 `watch.py` 의 alert 재생 분리 정도다(통합자 판단).

## 5. 메트릭·로그

`ws_connections{kind}`, `ws_chunks_total{result=stored|duplicate|reordered|stale|rejected}`, `ws_ack_latency_seconds`, `ws_credit`, `ws_resume_total{result}`, `ws_dropped_partials_total`, `ledger_flush_seconds`, `ledger_flush_rows`, `ledger_pending_rows` — 전부 `chartwire.ops.metrics` 의 객체(WP-G 단일 레지스트리). ops 패키지가 없으면 `_nometrics` 로 대체. 로그에는 seq·epoch·에러 타입만 남기고 청크 바이트·전사·티켓은 절대 남기지 않는다.

## 6. 다른 WP 에 요청

- **WP-E (`api/app.py`, `api/routers/sessions.py`)** — 계약대로 `include_router(chartwire.ws.routes.router)` + lifespan 에서 `await ws.routes.on_startup(app, deps)` / `await ws.routes.on_shutdown(app)`. `ops.routes.on_startup` 과의 순서는 무관(둘 다 `app.state.drainer` 를 재사용). ws-ticket:
  ```python
  from chartwire.redis import tickets
  token = await tickets.issue(deps.redis, tenant_id=principal.tenant_id, user_id=principal.user_id or principal.sub,
                              role=principal.role, session_id=session_id, kind=body.kind)   # ttl_s=30
  return WsTicketOut(ticket=token, expires_in=30)
  ```
  `POST /v1/consents/{id}/revoke` 는 환자의 live 세션마다 `SessionState(redis).publish_ctl(sid, {"t": "consent_revoked"})`, 파기는 `{"t": "purge"}` (ingest 4011/4012, watch 4011/4012). `alerts.service.acknowledge(session, *, event_id, by, tenant_id)` 를 만들면 `ws/watch.py::_ack_alert` 가 그것을 호출하도록 바꿔 주면 좋다(현재는 repo 직접 호출 + 감사 `alert.acked` + ZREM + 발행을 셸이 한다).
- **WP-A (`db/repo/segments.py`)** — `async def last_seq(session, session_id, *, started_at=None) -> int` (`max(seq)`, 없으면 −1, `created_at >= started_at` 프루닝). 있으면 `ws/watch.py::_load_session` 의 Core 쿼리를 이것으로 바꾼다:
  ```python
  -            stmt = select(func.coalesce(func.max(TranscriptSegment.seq), -1)).where(...)
  -            last = int((await s.execute(stmt)).scalar_one())
  +            last = await segments_repo.last_seq(s, row.id, started_at=row.started_at)
  ```
- **WP-C (`stt/worker.py`)** — 스트림 계약 그대로: 필드 `seq,key,len,off,fl,ep,ts`(문자열), 엔드 마커 `{"end":"1","ep":"<epoch>"}`, 그룹 `stt` 는 hello 가 `0` 으로 만든다. 세그먼트 `text_enc` 의 AAD 는 `chartwire.ws.watch.segment_aad(tenant_id, session_id, seq)` = `aad(tenant, "session", sid, f"segment:{seq}")` — 뷰어 재생·REST 세그먼트·노트가 모두 이 값으로 복호화한다. `transcript.final`/`risk.alert` 는 **커밋 뒤** 발행(뷰어의 갭 메우기가 DB 를 읽는다). `risk.alert.risk_event_id` 는 정수. 발행 페이로드는 `docs/protocol.md` §3.4.
- **WP-G (`worker/handlers/session_reaper.py`)** — 세션을 끝낼 때 `SessionState.xadd_end(sid, epoch)` + `set_fields(sid, state="ended")` + `publish_event(sid, {"t":"session.state","state":"ended"})` + `expire_after_end(sid)` 순서. `ops.routes.on_startup` 이 `app.state.drainer` 를 재사용하는 현재 구현이면 추가 변경 없음.
- **WP-H (loadtest/console)** — `tests/ws/_live.py::Recorder` 가 준수 클라이언트의 최소 구현(credit 규칙, `welcome.missing`/`nack` 재전송, pong, `bye` 처리). 정지된 뷰어 테스트에서 배운 것: 부하 테스트 페이로드는 비압축이어야 백프레셔가 측정된다(deflate 가 켜져 있음).

## 7. 최종 결과

마지막 실행(2026-09-03, §2 의 명령 그대로):

- `tests/ws`: **149 passed** in 28.7 s (hypothesis 2,100 예제 포함)
- `tests/integration/test_ws_*.py`: **35 passed** in 52.7 s (state 9 · ingest 19 · watch 6 · drain 1)
- `ruff check` / `ruff format --check`: clean (33 files)
- `mypy --strict src/chartwire/ws/core.py src/chartwire/ws/codec.py`: clean

### 7.1 알려진 이슈

- 정지된 뷰어 e2e 는 커널 소켓 버퍼 크기에 의존한다(loopback 에서 4–6 MB 를 흡수한 뒤 차단). `tcp_wmem` 최대가 아주 큰 박스에서는 `total` 을 올려야 한다. 결정론적 큐 의미론은 `test_watch_queues.py` 가 고정한다.
- `IngestConnection._mirror_ack` 는 ack 마다 짧은 태스크를 만든다(추적하지 않음). 종료 시 Redis 오류는 억제된다.
- `SubscriberManager` 는 `get_message(timeout=1.0)` 폴링 루프라 유휴 프로세스에서도 1 s 마다 깨어난다(부하 무시 가능).
- 세션 재생 시 열린 경보는 재생 끝에 한 번 실린다. 재생 도중 라이브로 들어온 같은 경보는 id 로 중복 제거된다.
