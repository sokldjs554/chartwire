# WP-C Phase 1 핸드오프 — stt-worker · 위험 경보 · SLA 티커

> 상태: **완료** (2026-09-03). 게이트: `pytest tests/integration/test_alerts.py tests/integration/test_stt_worker.py tests/integration/test_stt_worker_chaos.py -q -p no:xdist` = **14 passed / 0 failed** (51 s) · `tests/unit` 690 passed · `ruff check` + `ruff format --check` clean · `mypy src/chartwire/stt src/chartwire/risk src/chartwire/worker/handlers/alert_sla.py` clean (strict 의무 아님; `outbox`/`crypto` strict 도 그대로 clean).
> 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_c CHARTWIRE_TEST_REDIS_DB=3`
> 라이브 포트 8102 는 쓰지 않았다(HTTP 서버 없음; 카오스 테스트는 서브프로세스 워커 2개, ops 포트 0). 소유 경로 밖은 건드리지 않았고 git 상태를 바꾸는 명령은 실행하지 않았다. 트리의 다른 변경(`notes/*`, `api/routers/notes.py`, `wp-d-phase1.md`, `docs/grounding.md`)은 동시 작업 중인 WP-D 의 것이다.

## 0. 체크리스트

- [x] `risk/alerts.py`
- [x] `worker/handlers/alert_sla.py`
- [x] `stt/consumer.py`
- [x] `stt/worker.py`
- [x] `tests/integration/test_alerts.py` (5)
- [x] `tests/integration/test_stt_worker.py` (8)
- [x] `tests/integration/test_stt_worker_chaos.py` (1, 실제 서브프로세스 SIGSTOP/SIGCONT)
- [x] `docs/stt-providers.md` (신규, 한국어), `docs/risk-detection.md` §8 (경보 수명주기)
- [x] ruff / ruff format / mypy, 최종 테스트, 이 문서

## 1. 무엇을 만들었나 (모듈 지도)

| 경로 | 역할 | 스펙 |
|---|---|---|
| `stt/worker.py` (411줄) | `SttWorkerConfig.from_env(settings)` (`CHARTWIRE_STT_*`), `build_adapter` (simulator · slow · aws), `SessionResolver`(`stt:active` 의 sid → 테넌트: 해시 `tenant` 필드 우선, 없으면 활성 테넌트 순회 30 s 캐시), `SttWorker`(1 s 발견, `SET stt:owner NX PX`, Lua compare-and-expire 갱신(리스/3 마다), 세션당 태스크, 드레인 시 소비자 정지 + 리스 해제), `run(settings, …)`/`main(settings)` (WP-G `serve stt-worker` 계약), `python -m chartwire.stt.worker`, ops HTTP(`standalone_app`, `CHARTWIRE_STT_WORKER_PORT` 기본 9002, 0 = 없음) | §7.4, §5 |
| `stt/consumer.py` (615줄) | `SessionConsumer`: `_load`(stt_offsets + 마지막 세그먼트 t_end + DEK) → `XAUTOCLAIM idle>60 s` → `XREADGROUP COUNT 32 BLOCK 1000` 루프. 엔트리: dedup(`seq ≤ last_chunk_seq` → XACK) · 갭 rebuild(`audio_chunks` + 오브젝트 스토어 복호화, sha256 대조, 미원장 행 200 ms 대기) · 동의 `transcription` 게이트(1 s 캐시; 없으면 어댑터 호출 없이 오프셋 전진) · `feed` → Partial PUBLISH / Final → **한 트랜잭션**(`insert_final` created_at = started_at + t_start_ms, AAD `tenant:session:sid:segment:{seq}`; `search_index` 면 `segment_search` + `lexicon_tag`; `detector.scan` → `alerts.create_event`; `stt_offsets` upsert) → XACK → ZADD/PUBLISH(`transcript.final`/`risk.alert` 에 `committed_at`) → `HSET stt:lag`(XINFO GROUPS pending+lag). `NOGROUP`/`UNBLOCKED` → `XGROUP CREATE $ MKSTREAM` + `SADD stt:active` + 원장 재생. 엔드 마커 → (게이트 통과 시) `flush` → `state='transcribed'` + outbox `session.transcribed` + 감사 한 tx → XACK → `session.state` 발행 → `outbox:wake` → `SREM stt:active`, `HDEL stt:lag`. `ConsumerStats` 카운터 | §7.4, §8.3 |
| `risk/alerts.py` (282줄) | `sla_deadline`, `alert_message`, `create_event`(tx 안: `risk_events` + 감사 `alert.created`), `after_commit`(ZADD `alerts:sla` + PUBLISH `risk.alert{…, committed_at}`, 메트릭), `acknowledge_in_tx` + `after_ack`(호출자 tx 용) / `ack(ctx, *, tenant_id, risk_event_id, by, actor_role, via)`(행위자 역할 tx + 감사 `alert.acked` + ZREM + `risk.ack{by}`), `parse_members`, `escalate_due(ctx, now)`(테넌트별 한 tx, `escalation_level=1`, 감사 `alert.escalated`, 커밋 뒤 세션 채널 + `tenant:{tid}:alerts` 발행, 마감 지난 멤버·깨진 멤버 ZREM, 한 단계만), `over_sla_count`(PostgreSQL 기준 `risk_unacked_over_sla`) | §7.2, §7.4, §8.5 |
| `worker/handlers/alert_sla.py` (22줄) | `INTERVAL_S = 1.0`, `tick(ctx) -> list[int]`: `escalate_due` 매 틱 + 게이지 10 s 마다 | §7.2 |
| `docs/stt-providers.md` | 어댑터 계약, 시뮬레이터, `SlowStt`, Transcribe 매핑과 **미검증 항목**, 워커 구조·설정·엔트리 처리·최종 트랜잭션·복구 경로 표 | §14 |
| `docs/risk-detection.md` §8 | 경보 수명주기 표(생성/커밋 뒤/확인/에스컬레이션), 게이지 출처 | §14 |

LOC(비공백): Phase 1 구현 1,330 / 테스트 823. Phase 0 포함 stt+risk+alerts 구현 ≈ 2,540, 테스트 ≈ 1,420 — §0 예산(1,000/500)을 크게 넘는다. 사전 데이터(≈330)와 복구 경로(rebuild·NOGROUP·리스·드레인, ≈400)가 대부분이며 통합자가 자를 후보는 `SessionResolver` 의 테넌트 순회(§5 요청 1 이 반영되면 ≈40줄 삭제)와 `worker.py` 의 ops HTTP 서버(≈30줄, WP-G 의 것을 재사용 가능) 정도다.

## 2. 테스트 (14 passed)

```bash
source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_c CHARTWIRE_TEST_REDIS_DB=3
pytest tests/integration/test_alerts.py -q -p no:xdist              # 5   ≈3 s
pytest tests/integration/test_stt_worker.py -q -p no:xdist          # 8   ≈35 s
pytest tests/integration/test_stt_worker_chaos.py -q -p no:xdist    # 1   ≈12 s  (서브프로세스 2개, SIGSTOP/SIGCONT/SIGTERM)
pytest tests/unit -q                                                 # 690, 서비스 불필요
ruff check src/chartwire/stt src/chartwire/risk src/chartwire/worker/handlers/alert_sla.py tests/integration/test_alerts.py tests/integration/test_stt_worker*.py
mypy src/chartwire/stt src/chartwire/risk src/chartwire/worker/handlers/alert_sla.py
```

| 테스트 | 검증 |
|---|---|
| `test_finals_persisted_with_seq_created_at_alert_and_end_marker` | t01 42청크 + 마커 → 세그먼트 seq 0..4, `created_at == started_at + t_start_ms`, 복호화 텍스트 == 스크립트, `stt_offsets == (42, 4)`, `risk_events` 1행(2등급, segment_seq 3, `sla = detected_at + 300 s`), ZSET 점수 == 마감 ms, `segment_search` 5행 + `terms`(`불면`, `자살사고`), `transcribed` + outbox `session.transcribed{session_id, patient_id}` + 멱등 키, 감사 `alert.created`/`session.transcribed`, 발행 순서(final 0..4 `committed_at`, `risk.alert`, partial, 마지막 `session.state`), `stt:active`/리스/`stt:lag` 정리 |
| `test_redelivered_entries_are_deduplicated_by_offsets` | 20청크 처리 → 워커 드레인(리스 해제) → 같은 20개 재전달 + 나머지 → 두 번째 워커: 세그먼트 5, `duplicates == 20`, rebuild 0 |
| `test_gap_is_rebuilt_from_ledger_and_waits_for_late_rows` | 스트림에 11..30 없음, 원장에 21..30 은 0.8 s 뒤 삽입 → rebuild 1회, 20청크 재구성(복호화·sha256), 세그먼트 5 |
| `test_flushdb_recreates_group_and_rebuilds_from_ledger` | 15청크 처리 중 `FLUSHDB` → `UNBLOCKED` → 그룹 `$` 재생성, `stt:active` 재추가, 리스 재취득(`w1`), 원장 16..42 재생 → 마커 → transcribed, 중복 0 |
| `test_consent_gates[no-transcription / no-search-index / all]` | `recording` 만: 세그먼트 0·오프셋 42·`skipped_no_consent == 42`·final 발행 0·그래도 transcribed + outbox(ai_drafting 게이트는 노트 핸들러 몫); `search_index` 없음: 세그먼트 5·`segment_search` 0·경보 1 |
| `test_ownership_lease_is_exclusive_and_released` | NX 배타, 갱신 compare-and-expire, 만료 뒤 타 워커 취득, 해제, **활성 세션의 사라진 리스만 재취득**, 발견이 어느 테넌트에도 없는 sid 를 제거 |
| `test_sigstop_worker_is_taken_over_via_xautoclaim` (chaos) | `slow` 150 ms 워커 1 을 SIGSTOP(미확인 엔트리 보유) → 2 s 리스 만료 → 워커 2 취득 + `XAUTOCLAIM`(로그 `entries autoclaimed`) → **워커 1 이 멈춘 채로** 세그먼트 0..4, `stt_offsets (42,4)`, pending 0, transcribed → SIGCONT → 워커 1 이 소유권 상실을 보고 물러남(중복 0, 오프셋 회귀 0, `session transcribed` 로그 없음) → 둘 다 SIGTERM exit 0 |
| `test_alerts.py` | 생성(3등급 +60 s ZSET, 1등급 타이머 없음, 페이로드 키 정확, 억제 히트 거부), 확인(타 테넌트 `None`, 멱등, 감사 행위자 역할, ZREM, `risk.ack{by}`/`NOBODY`), 에스컬레이션(`FakeClock` 61 s, 두 채널 발행, 확인된 stale 멤버·깨진 멤버 제거, 한 단계만, 게이지 1 → 2), `parse_members`, `sla_deadline` |

## 3. 이번 실행에서 테스트가 잡은 결함

| 결함 | 어떻게 드러났나 | 수정 |
|---|---|---|
| 동의 `transcription` 이 없어도 엔드 마커의 `flush()` 가 시뮬레이터의 미전송 발화 5개를 전부 세그먼트로 만들었다 | `test_consent_gates[no-transcription]` 세그먼트 5 | `_on_end` 도 같은 게이트(신선한 재조회)를 지난 뒤에만 `flush` |
| `FLUSHDB` 가 블로킹 `XREADGROUP` 중에 오면 `NOGROUP` 이 아니라 `UNBLOCKED the stream key no longer exists` | FLUSHDB 테스트 크래시 | 두 오류를 "그룹 소실" 로 통일(`_group_vanished`) |
| 세션을 끝낸 워커가 리스를 DEL 하자, SIGSTOP 에서 깨어난 옛 소유자가 "키 없음 = 재취득" 규칙으로 리스를 다시 잡고 `stt_offsets` 를 (9, 0) 으로 되돌렸다 | 카오스 테스트 최종 불변식 | 사라진 리스는 **`stt:active` 에 남아 있는 세션에서만** 재취득(`owns()` + 갱신 Lua 모두) |
| 깨진 `alerts:sla` 멤버가 매 틱 경고만 찍고 남았다 | `test_escalate_due` | `parse_members` 가 `(parsed, malformed)` 를 돌려주고 티커가 ZREM |

## 4. 스펙·계약과 다르게 한 점 (이유)

1. **STT 설정은 환경변수 직접 읽기.** `Settings`(WP-A) 에 STT 필드가 없고 `Settings.scripts_dir` 는 WP-A 가 Redis Lua 디렉터리로 해석했다(스펙 §3.1/§10.4 는 STT 스크립트 디렉터리). 충돌을 피하려고 `CHARTWIRE_STT_SCRIPTS_DIR`(기본 `var/scripts`) 등 `CHARTWIRE_STT_*` 를 `SttWorkerConfig.from_env` 가 읽는다(`docs/stt-providers.md` §3.2). §5 요청 3.
2. **per-chunk 동의 게이트 1 s 캐시.** §8.3 "gates always re-check the live consent" 를 청크마다 DB 조회로 구현하면 200 세션 × 5/s = 1,000 q/s 라 세션당 1 s 캐시를 두었고, 최종 세그먼트 트랜잭션과 엔드 마커 flush 는 항상 다시 읽는다. 철회 뒤 최대 1 s(≤5청크)의 어댑터 호출이 있을 수 있으나 세그먼트는 커밋되지 않는다.
3. **정상 경로는 오디오 바이트를 읽지 않는다**(`fetch_audio`, 시뮬레이터/`slow` 기본 false, `aws` 는 true). 갭 rebuild 는 항상 원장 + 오브젝트 스토어 복호화 + sha256 대조로 재구성한다(§7.4 문장 그대로).
4. **재시작 뒤 중복 Final 의 시간 규칙**(`t_end_ms ≤ 마지막 저장 세그먼트`). 시뮬레이터는 새 스트림의 첫 청크에서 지난 발화를 모두 다시 내므로 어댑터 무관 규칙이 필요했다. 스펙은 침묵.
5. **엔드 마커 → `transcribed` 전에 `state='ended'` 를 ≤10 s 기다린다.** WP-B 셸은 마커를 XADD 한 뒤 `ended` 를 커밋하므로 그대로 두면 `transcribed` 가 `ended` 에 덮일 수 있다. §5 요청 2.
6. **NOGROUP 복구가 `stt:active` 를 스스로 되살린다**(런북 §3-4 는 운영자가 SADD). 세션 상태가 종료 상태면 소비자를 끝낸다.
7. **리스 재취득 규칙**: 키가 없고 세션이 `stt:active` 에 있으면 재취득(Redis 손실), 아니면 소유권 상실. `owns()` 는 최종 트랜잭션·오프셋 기록 직전마다 GET 한 번(세그먼트당 1회 + 초당 ≤1회).
8. **`stt:lag` = `XINFO GROUPS` 의 `pending + lag`**(배치마다 1회). 스펙 "pending chunks" 의 구체화.
9. **유휴 종료**: 스트림이 10 s 비어 있고 세션이 `stt:active` 에 없으면 소비자를 끝낸다(파기·마커 없는 종료 뒤 좀비 태스크 방지).
10. `escalate_due(ctx, now) -> list[int]`, `ack(ctx, *, …) -> RiskEvent | None`, `acknowledge_in_tx(session, …)`/`after_ack(redis, …)` — 스펙 §3.1 의 `alerts.on_final_segment(session, segment, hits)` 는 `create_event`(tx 안) + `after_commit`(커밋 뒤) 두 조각으로 나눴다: 한 함수로는 "커밋 뒤에만 발행" 을 지킬 수 없다.
11. `session.transcribed` 멱등 키 = `session.transcribed:{sid}:{epoch}`.
12. ops HTTP 포트 9002 — 스펙이 정하지 않음(WP-G 워커 9001 옆자리).
13. LOC 초과 — §1.

## 5. 다른 WP 에 요청 (정확한 diff)

1. **WP-B (`ws/ingest.py`)** — `sess:{sid}` 해시에 `tenant` 필드. `SessionResolver` 는 이미 이 필드를 먼저 읽는다(있으면 테넌트 순회 없음). hello 뒤와 rehydrate 뒤 한 줄씩:
   ```python
   # ws/ingest.py, hello.lua 호출 직후 (그리고 rehydrate 분기 뒤)
   -        hot = await self.rt.state.hello(sid, self.rt.node_id, self.conn_id, now=self.rt.clock.now())
   +        hot = await self.rt.state.hello(sid, self.rt.node_id, self.conn_id, now=self.rt.clock.now())
   +        await self.rt.state.set_fields(sid, tenant=str(sess.tenant_id))
   ```
   또는 `hello.lua` 의 `HSET` 에 `'tenant', ARGV[6]` 를 추가하고 `SessionState.hello(..., tenant_id=)` 로 넘겨도 된다.
2. **WP-B (`ws/ingest.py`, `Transition('ended')`)** — 마커 XADD 보다 `sessions.state='ended'` 커밋을 먼저(순서 교환). 그러면 소비자의 10 s 대기(`END_WAIT_S`)는 즉시 통과하고, 극단적으로 늦은 커밋이 `transcribed` 를 덮는 창이 사라진다.
3. **WP-A (`core/config.py`)** — 스펙 §3.1 대로 `scripts_dir` 를 STT 스크립트 디렉터리로 쓰고 Lua 는 패키지 리소스만 쓰도록(WP-B `ws/routes.py` 의 `scripts_dir=settings.scripts_dir` 제거). 그때 아래 필드를 추가하면 `SttWorkerConfig.from_env` 를 `from_settings` 로 바꾼다:
   ```python
   # core/config.py
   +    # --- stt-worker ----------------------------------------------------------
   +    stt_provider: Literal["simulator", "slow", "aws"] = "simulator"
   +    stt_scripts_dir: Path = Path("var/scripts")   # 또는 scripts_dir 의 의미를 이것으로
   +    stt_slow_delay_ms: int = 400
   +    stt_sim_seed: int = 0
   +    stt_worker_port: int = 9002
   ```
4. **WP-A (`db/repo/sessions.py`)** — `upsert_stt_offset` 를 단조로:
   ```python
   -            "last_chunk_seq": stmt.excluded.last_chunk_seq,
   -            "last_segment_seq": stmt.excluded.last_segment_seq,
   +            "last_chunk_seq": func.greatest(SttOffset.last_chunk_seq, stmt.excluded.last_chunk_seq),
   +            "last_segment_seq": func.greatest(SttOffset.last_segment_seq, stmt.excluded.last_segment_seq),
   ```
   워커는 소유권 검사로 회귀를 막지만(카오스 테스트) SQL 에 불변식이 있는 편이 낫다.
5. **WP-E (`api/routers/alerts.py`)** — `POST /v1/alerts/{id}/ack`:
   ```python
   from chartwire.risk import alerts
   event = await alerts.ack(deps, tenant_id=p.tenant_id, risk_event_id=id, by=p.user_id, actor_role=p.role, via="rest")
   if event is None: raise AppError("CW-4040", 404, "경보를 찾을 수 없습니다")
   ```
   (`deps` 는 `engine, redis, clock` 만 쓴다.) **WP-B (`ws/watch.py::_ack_alert`)** 는 `alerts.acknowledge_in_tx(s, tenant_id=…, risk_event_id=…, by=…, actor_role=…, now=…, via="ws")` + `alerts.after_ack(redis, event, by=…)` 로 바꾸면 REST/WS 가 한 구현을 쓴다(지금 동작은 동일).
6. **WP-G (`ops/metrics.py`)** — 시나리오 D 의 `rebuild_count` 용 카운터. 소비자는 `getattr(metrics, "STT_REBUILDS_TOTAL", None)` 로 이미 호출한다:
   ```python
   +STT_REBUILDS_TOTAL = Counter("stt_rebuilds_total", "Ledger rebuilds after a stream gap / lost group", registry=REGISTRY)
   ```
   `ALL_NAMES` 에 `"stt_rebuilds_total"` 추가. 워커 `main.py` 는 변경 없음(`alert_sla.tick`/`INTERVAL_S` 계약대로).
7. **WP-F (`synth/seed.py`) / WP-A (`docker-compose.yml`)** — 데모 스크립트를 `var/scripts/`(또는 `CHARTWIRE_STT_SCRIPTS_DIR`) 에 두고 compose 의 `stt-worker` 에 `CHARTWIRE_STT_SCRIPTS_DIR`, `CHARTWIRE_STT_WORKER_PORT` 를 노출.
8. **WP-H (loadtest D)** — stt-worker SIGSTOP 15 s 는 리스 30 s 보다 짧아 같은 워커가 이어받는다(XAUTOCLAIM 은 60 s). 두 워커 인계를 재려면 `CHARTWIRE_STT_LEASE_MS`/`_AUTOCLAIM_IDLE_MS` 를 줄이거나 SIGSTOP 을 ≥60 s 로. `rebuild_count` 는 요청 6 의 카운터 또는 워커 로그 `rebuild` 줄 수.

## 6. 알려진 이슈

- `over_sla_count` 는 테넌트당 열린 경보 1,000개까지만 훑는다(`OPEN_SCAN_LIMIT`).
- SIGSTOP 에서 깨어난 옛 소유자는 첫 오프셋 기록/최종 트랜잭션 전(≤1 s)에는 `transcript.partial` 을 발행할 수 있다(뷰어 표시만 영향, DB 영향 없음).
- 카오스 테스트는 타이밍 기반(150 ms/청크, 리스 2 s, autoclaim 0.5 s)이며 이 박스에서 3회 연속 통과했다. 느린 CI 에서는 `wait_for` 상한을 늘리면 된다.
- `AwsTranscribeStreaming` 경로(`fetch_audio=True`, `start_time` 오프셋)는 실행하지 않았다 — `docs/stt-providers.md` §2.3.
- 세션이 `purged` 되면 소비자는 `SessionGone`/유휴 종료로 빠지지만, 파기 파이프라인(WP-E)이 `stt:owner` 를 지우는 시점과 겹치면 최대 10 s 동안 태스크가 남을 수 있다.
