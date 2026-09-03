# WP-B 핸드오프 — Phase 0: WebSocket 프로토콜 코어 (ingest/watch)

담당 범위(Phase 0): `src/chartwire/ws/{codec,messages,core,credit,actions}.py`, `tests/ws/`, `docs/protocol.md`.
Phase 1 항목(`ws/ingest.py`, `ws/watch.py`, `ws/ledger.py`, `ws/drain.py`, `redis/session_state.py`,
`redis/scripts/*.lua`, `redis/tickets.py`, `api/routers/sessions.py` 의 ws-ticket, uvicorn e2e 테스트)은 아직 코드가 없고
설계는 §5 에 적어 두었다. 이 문서는 중단된 실행을 다른 에이전트가 이어받을 수 있도록 쓴 것이다.

## 1. 만든 것 (모듈 맵)

| 모듈 | 내용 | 스펙 |
|---|---|---|
| `ws/actions.py` | 코어가 돌려주는 액션 dataclass(`Store, Ack, Nack, SendCredit, Send, Close, Transition, Rehydrate, Subscribe, Replay`), `CloseCode` IntEnum + 표준 reason 문자열, `ranges(seqs)`(닫힌 구간 압축), `error_msg`, `IngestStats` 카운터 | §6.5, §6.8 |
| `ws/codec.py` (mypy strict) | 12 B LE 헤더 인코드/디코드. 검증 실패는 전부 `FrameError` 하위 예외로, 예외가 종료 코드(`4010`, SIM 금지는 `4005`)와 reason 을 들고 있다. `decode_frame(b, allow_sim=)`, `encode_frame(h, payload)`, `decode(b)`. 상한 65,536 B, 빈 페이로드·seq 0·알 수 없는 플래그 거부 | §6.2 |
| `ws/messages.py` | 양방향·양 엔드포인트 pydantic v2 모델(`extra='forbid'`, frozen). `parse_client_message(kind, raw)`, `parse_server_message`, `dump(msg)`, `render(action)`(Ack/Nack/SendCredit → JSON). `hello.resume=true` 는 `last_sent_seq` 필수. 잘못된 JSON/알 수 없는 `t`/필드 오류 → `MessageError(close_code=4005)` | §6.3 |
| `ws/credit.py` | `compute(base, stt_lag, node_pending)`, `changed_significantly`(25 % 규칙, 광고값 0 은 1 로 취급), `violates`(+20 허용), 상수(`LOW_WATER=10`, `ZERO_PAUSE_MS=2000`, `RING_BUFFER_CHUNKS=150`, `PING_EVERY_MS=15000`), `Heartbeat`(ping 타이머, 2회 누락 → 4000) | §6.4 규칙 3, 5 |
| `ws/core.py` (mypy strict, 297줄) | `IngestCore`(녹음기 상태 기계)와 `WatchCore`(뷰어 상태 기계). 순수 이벤트 → 액션 목록. 아래 §2 | §6.5 |
| `docs/protocol.md` | 규범 프로토콜 문서(한국어): 엔드포인트·티켓, 프레임 레이아웃, 메시지 표 4개, 상태 기계 mermaid 2개, 규칙 §4.1–4.9, 뷰어 규칙, 종료 코드 표, 재개 워크드 예시 | §6 |

## 2. 코어 요약 (리뷰어가 여는 파일)

### `IngestCore`

커서: `contig_seq`(이 연결에서 저장됨 또는 이미 durable 인 연속 접두사) ≥ `ledger_seq`(PostgreSQL 커밋 연속 접두사) ≥
`ack_seq`(마지막 누적 ack). `highest_seen − ack_seq` 가 outstanding.

- `on_hello(ack_seq_from_store, resume, last_sent_seq, epoch, now_ms, ledgered_after_ack=(), hot_state_present=True)`
  → `[Rehydrate?] Transition('recording') Send(welcome)`. 갭 > 150 이면 `error 4008 + Transition('ended') + Close`.
  `welcome.ack_seq` 는 저장된 `ack_seq` 와 연속인 durable 행까지 포함한다(이미 커밋된 행을 다시 받을 이유가 없다).
- `on_chunk` → 기대 seq 면 `Store`(재정렬 버퍼 배출 포함, durable 행은 건너뜀), 앞선 seq 면 버퍼(≤64) + `Nack`(같은 집합은
  100 ms 에 한 번), `≤ ack_seq` 중복이면 `Ack` 재전송, 그 밖의 중복은 무시. credit+20 초과 → 4009. `final_seq` 초과 → 4012.
- `on_ledgered(seqs)` → 원장 커서 전진, ack 규칙(8개 / 100 ms / credit<10), `end`·drain 완료 판정.
- `on_end(final_seq)` → 모순이면 4010, 누락이면 즉시 `Nack` 후 10 s 마다, 완료 시 `Ack → Transition('ended') → bye{ended} → 1000`.
- `on_tick(now, stt_lag, node_pending)` → credit 재계산 + 25 % 규칙 `SendCredit`, `credit==0` 2 s → `pause`, 시간 기반 ack,
  `end` 데드라인 nack, heartbeat(드레인 중에는 정지). 4000 이 나가면 코어는 닫힌 상태가 된다.
- `on_drain(now)` → `bye{drain, ack_seq}` 한 번; 저장분이 모두 durable 해지면 즉시, 아니면 20 s 데드라인에 `Ack? → Close 1012`.
- `on_superseded(new_epoch)` / `on_consent_revoked()` → 강제 `Ack` → `bye` → 4409 / 4011.

### `WatchCore`

`on_hello(from_seq)` → `Subscribe, Send(welcome), Replay(from_seq−1)`; 재생 중 라이브 final 은 보류, 재생 후 연속분만 flush,
틈이 있으면 `Replay` 재요청; 경보는 id 로 중복 제거; 보류 1,024 초과 → 4013; heartbeat 4000.

## 3. 테스트

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/ws -q                      # 144개, DB/Redis 불필요 (CHARTWIRE_TEST_DB=chartwire_test_b, Redis index 2 는 미사용)
ruff check src/chartwire/ws tests/ws && ruff format --check src/chartwire/ws tests/ws
mypy --strict src/chartwire/ws/core.py src/chartwire/ws/codec.py
```

| 파일 | 내용 |
|---|---|
| `test_codec.py` | 헤더 라운드트립, 각 검증 실패 → 예외/종료 코드, 경계값(65,536 / 65,537 B), hypothesis 라운드트립 |
| `test_messages.py` | 각 메시지 파싱, `extra='forbid'`, 판별자 오류, `render`, resume 검증 |
| `test_credit.py` | `compute` 클램프, 25 % 규칙, 허용치, `Heartbeat` 타이밍 |
| `test_ingest_core.py` | 종료 코드 경로 전부 + ack/nack/credit/resume/end/drain/superseded/consent 예시 테스트, 재개 워크드 예시(문서 §8 과 동일 숫자) |
| `test_ingest_core_props.py` | hypothesis stateful 1,100 예제 × 32 스텝: 손실·중복·재정렬·드롭·원장 순서 뒤섞기·틱·재접속 resume·superseded·drain. 불변식: ledger 되지 않은 seq 에 Ack 없음, ack 단조(welcome.ack_seq 포함), 저장 집합 == 전송 집합, 준수 녹음기는 4009/4000/4008 을 받지 않음, 종료 시 1000 |
| `test_watch_core.py`, `test_watch_core_props.py` | Subscribe→Replay 순서, 재생/라이브 교차·지연·중복·재접속 하에서 final 이 정확히 1씩 증가, 경보 중복 없음 (1,000 예제 × 32 스텝) |

hypothesis 합계 2,100 예제. 더 깊은 소크(4,000 + 2,000 예제, 80 스텝)는 `settings` 만 바꾼 스크립트로 수동 실행했다
(§6 버그 저널). 실행 시간은 §8.

## 4. 스펙과 다르게 한 점 (이유)

1. **`on_hello` 키워드 인자 2개 추가** — `ledgered_after_ack: Iterable[int]`(store 의 `ack_seq` 뒤에 이미 커밋된 seq 들;
   `welcome.missing` 계산에 필요)와 `hot_state_present: bool`(False 면 `Rehydrate` 액션을 먼저 낸다). 스펙 시그니처의
   호출은 그대로 동작한다.
2. **`welcome.ack_seq` 가 store 값보다 클 수 있음** — 저장된 `ack_seq` 와 연속인 durable 행을 포함한다. "ack = durable"
   원칙에 부합하고, 그 행들은 `missing` 에도 없으므로 녹음기 관점에서 일관된다.
3. **drain 의 `bye` 는 한 번만** — 스펙 문장(bye → flush → 1012)대로. 데드라인 종료 시 두 번째 bye 를 보내지 않는다.
   drain 중에는 heartbeat `ping` 을 보내지 않는다(이미 닫겠다고 알렸으므로 생존 검사는 무의미).
4. **`on_ledgered` 에서도 100 ms 규칙을 평가** — 스펙 규칙 2 의 문장 그대로. 원장 배치가 50 ms 마다 커밋되므로 저속
   스트림에서도 ack RTT 가 틱 주기(200 ms)에 묶이지 않는다.
5. **`on_consent_revoked()`, `on_drain(now_ms)`, `WatchCore.on_replay_done()`, `on_tick`, `on_pong` 추가** — 스펙 목록에 없지만
   규칙 7/8, §6.7 을 코어에서 결정론적으로 검증하기 위해 필요했다.
6. **`pause{}` / `resume_rec{}`** 는 코어가 다루지 않는다. 세션 상태 표시(`Transition` + `session.state` 이벤트)만
   바꾸는 셸 책임으로 정의했고 문서 §3.1 에 명시했다.
7. **duplicate 재-ack 는 마지막 광고 credit 을 실어 보낸다**(현재 계산값이 아니라). credit 광고 채널은 `ack`(정규)와
   `credit{}`(25 % 규칙)뿐이라는 규칙을 단순하게 유지하기 위해서다.
8. **stateful 스텝 수 32** — 스펙 목표(≥2,000 예제, <30 s)를 다른 에이전트가 같은 박스에서 테스트를 돌리는 상황에서도
   지키기 위해 40 → 32. 긴 상호작용은 80 스텝 소크로 별도 확인했다.

## 5. Phase 1 설계 (아직 코드 없음)

- **`ws/ingest.py` (녹음기 셸)** — 연결당 코루틴 하나: `hello` 파싱 → 티켓 `GETDEL` → 동의 `recording` 게이트(WP-E
  `consent.gates.require_scope`, 실패 4011) → `hello.lua`(epoch++, node/conn 기록, superseded PUBLISH) →
  `IngestCore.on_hello`. 이후 `asyncio.TaskGroup` 으로 (a) 소켓 수신 루프(바이너리 → `decode_frame` → `on_chunk`, 텍스트 →
  `parse_client_message`), (b) 200 ms 틱(`stt:lag` HGET, `LedgerBatcher.pending_rows` → `on_tick`), (c) `ctl:{sid}` 구독
  (superseded / consent_revoked / purge). 액션 실행기 `run(actions)`: `Store` → sha256 → `Envelope.encrypt`(DEK 는
  `KeyCache`) → `objectstore.put` → `xadd_chunk.lua` → `batcher.submit(row)` 의 future 콜백에서 `on_ledgered(seqs)`
  (배치 단위로 묶어 한 번 호출). `Ack/Nack/SendCredit/Send` → `render` → `orjson` 텍스트 프레임. `Close` → 소켓 close.
  `Transition` → `repo/sessions.set_state`. `Rehydrate` → `SessionState.rehydrate(sid)`. Redis 실패 3회 연속 → 4503.
  코어 메서드는 항상 같은 태스크에서 호출한다(코어는 동시성 안전하지 않다 — 단일 태스크 직렬화가 설계).
- **`ws/ledger.py` `LedgerBatcher`** — 프로세스 전역. `submit(row) -> Future`; 첫 행 후 50 ms 또는 500 행에 플러시.
  tenant 별 한 트랜잭션(`tenant_tx` + `INSERT … ON CONFLICT DO NOTHING` 다중 VALUES + `UPDATE sessions SET ack_seq =
  GREATEST(ack_seq, :v)`): `:v` 는 그 세션의 코어가 알려 준 연속 접두사(`ledger_seq`)만 쓴다(비연속 행은 `audio_chunks` 에만).
  실패 시 future 에 예외 → 셸이 4503. 메트릭 `ledger_flush_seconds`, `ledger_flush_rows`, `ledger_pending_rows`.
- **`ws/watch.py`** — 프로세스당 세션당 구독자 태스크 하나(`SubscriberManager`: refcount, 마지막 뷰어가 나가면
  UNSUBSCRIBE). 뷰어마다 `partial_q(256)`/`critical_q(1024)` + sender 태스크 하나. hello 순서: SUBSCRIBE → `WatchCore.on_hello`
  → `repo/segments.replay(after_seq)` 를 100행 배치로 `on_replay_batch` → `on_replay_done` → 라이브.
- **`ws/drain.py` `Drainer`** — SIGTERM → `/readyz` 503 → 모든 코어에 `on_drain(now)` → 20 s 내 배처 플러시 → 남은 연결
  1012 → 프로세스 종료. WP-G 의 worker drain 과 같은 시그널 훅을 공유한다.
- **Redis** — `session_state.SessionState`(해시 접근자 + `hello.lua`/`xadd_chunk.lua` 호출, `rehydrate`), `tickets.issue/consume`
  (`SET … EX 30` / `GETDEL`), 스크립트는 `redis/scripts/` 에 파일로 두고 `SCRIPT LOAD` + `EVALSHA`(NOSCRIPT 시 재로드).
- **e2e** — 실제 uvicorn 에 대해 3 세션 × 강제 kill 후 resume, 손실/중복 0; superseded 테스트. `CHARTWIRE_TEST_DB=chartwire_test_b`,
  `CHARTWIRE_TEST_REDIS_DB=2`.

## 6. 버그 저널 (테스트가 잡은 실제 결함)

- **재개 커서 교착** — `hello` 뒤 `contig_seq` 가 store 의 `ack_seq` 에서 시작해, 이미 durable 이라 재전송되지 않는 행에서
  기대 커서가 영원히 멈췄다(뒤의 청크는 재정렬 버퍼로 들어가고 `missing` 에도 잡히지 않음). 워크드 예시 테스트
  (`test_resume_welcome_acknowledges_rows_already_contiguous_in_the_ledger`)가 잡았고 `_advance()`(durable 행 건너뛰기)로 고쳤다.
- **4000 뒤 코어가 열린 채로 남음** — `IngestCore.on_tick` 이 heartbeat `Close` 를 내고도 `closed` 를 세우지 않았다.
- **drain 중 ping / 두 번째 bye** — 스펙 규칙 7 과 다르게 동작했다(§4.3 참조).
- **stateful 모델의 credit 클램프 오류** — 80 스텝 소크에서 준수 녹음기가 4009 를 받았다. 원인은 모델: 전송 중 프레임의
  "전송 당시 credit 태그" 최솟값으로 클램프했는데, 서버가 실제로 검사하는 것은 도착 시 `seq − ack_seq ≤ credit + 20` 이다.
  모델을 그 조건으로 바꾸고 태그를 없앴다(코어 변경 없음). 문서 §4.3 에 전제를 명시했다.

## 7. 다른 WP 에 요청

- **WP-A (`redis/keys.py`)**: 키 이름 함수 `sess(sid)`, `chunks(sid)`, `events(sid)`, `ctl(sid)`, `viewers(sid)`, `ticket(t)`,
  `stt_lag()`, `node(node_id)` 를 §5 이름 그대로 노출해 주면 Phase 1 셸이 그대로 쓴다. 이미 있으면 변경 없음.
- **WP-A (`db/repo/segments.py`, `db/repo/sessions.py`)**: `replay(session, session_id, after_seq: int, limit: int = 100)
  -> list[SegmentView]`(`seq > after_seq ORDER BY seq`)와 `ack_state(session, session_id) -> tuple[int, list[int]]`
  (`sessions.ack_seq` 와 그 뒤의 `audio_chunks.seq` 목록; `on_hello` 의 `ledgered_after_ack` 입력)가 필요하다.
- **WP-C (`stt/worker.py`)**: 발행하는 `transcript.final` 은 `docs/protocol.md` §3.4 필드 그대로(`segment_id` 정수, `committed_at`
  ISO 문자열). `WatchCore` 는 `seq` 로만 중복 제거하므로 세그먼트 seq 는 세션 안에서 0부터 빈틈없이 증가해야 한다.
- **WP-E**: `consent.gates.require_scope(scopes, "recording")` 를 hello 에서 호출하고 `ConsentScopeMissing.ws_code`(4011) 로
  닫는다 — WP-E 핸드오프의 요청과 일치.
- **WP-H (loadtest 클라이언트 / console JS)**: 녹음기 규칙은 `docs/protocol.md` §4.3–4.4, §4.7. 특히 `bye{drain}` 뒤에도
  `ack` 가 계속 오며, `welcome.ack_seq` 가 자신의 마지막 ack 보다 클 수 있다.

## 8. 최종 결과

(마지막 실행 후 갱신)
