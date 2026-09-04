# loadfix 핸드오프 — A n=200 붕괴 · C 무산출 근본원인 조사

> 상태: **완료** (2026-09-04). 중단되면 §0 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a`
> 규칙: JSON 손편집 금지(숫자는 재실행에서만), 박스는 부하측정 중 조용히, git 상태 변경 없음.
> 모든 데이터는 합성(SYNTHETIC).

## 0. 체크리스트 (재개용)

- [x] 1. 문서/스펙 읽기 (AGENT_ENV, integrator, wp-h-phase2, wp-f-phase1, SPEC §0/§5/§7.4/§9.3-9.4/§10.3/§11/§14)
- [x] 2. A n=200 / C 저장 로그·메트릭·DB 대조 분석 (§1)
- [x] 3. 근본원인 확증 — Redis 풀 상한 100 마이크로 재현 (§2)
- [x] 4. 코드 수정 (§3) — Redis 풀 상한, stt-worker 세션 상한·격리, 로그 `extra`, UUID 오탐, 러너 잔해 제거·드레인 대기
- [x] 5. 게이트: ruff / ruff format / mypy(strict 5 + loadtest) / `pytest tests/unit` 736 passed
- [x] 6. 재측정: A n=200 (runs[] 병합) → C (`make loadtest-c`) → `chartwire loadtest results`
- [x] 7. `stt_offsets_complete` 의미 판정 기록 (§5)
- [x] 8. 이 문서 마무리 + `docs/AGENTS.md` 버그 저널용 문단

## 1. 저장물에서 읽어낸 사실 (재실행 없이)

측정 로그: `var/loadtest/{A-n200-1788521076,C-n100-1788521309,A-n100-1788520987}/logs/{api,worker,stt-worker}.log`.

### 1.1 A n=200 (`var/loadtest/A-n200-1788521076`)

| 관측 | 값 |
|---|---|
| stt-worker `session acquired` | **104** (세션은 200개) |
| stt-worker `session consumer crashed` | 3 — 전부 `redis.exceptions.MaxConnectionsError: Too many connections` (`redis/asyncio/connection.py::get_available_connection`) |
| api 로그 `store path failed` | **600** (= 클라이언트 `reconnects 651`, `ws_resume_total{ok} 600`) |
| api 로그 그 외 | ingest accept 851, watch accept 200, `connection open` 1051 — 4009/4503 외 다른 오류 메시지는 없음 |
| A n=100 api 로그 | `store path failed` **0**, accept 100+100 |

`db.segment_rows 0`·`stt_lag_chunks 0`·`stt_rebuilds_total 0` 은 **컨슈머가 거의 살아 있지 않았다**는 뜻이다
(`STT_LAG_CHUNKS.set(sum(살아있는 컨슈머의 lag))` 이므로 컨슈머가 없으면 0).

클라이언트 `errors 651` 은 예외 클래스별 분류가 아니라 **재접속 횟수**다(`errors == reconnects == 651`,
그중 600은 서버가 `4503 store path failed` 로 닫은 것, 나머지 51은 램프 중 접속 실패/타임아웃).
즉 4009(superseded)나 프로토콜 오류는 0 이다 — `superseded_closes 0`, `nacks 0`, `loss 0` 과 일관.

### 1.2 C (`var/loadtest/C-n100-1788521309`)

| 관측 | 값 |
|---|---|
| stt-worker `session acquired` / `consumer start` | **416** (세션은 100개) |
| `session consumer crashed` | **269** = `ScriptError: cannot read script sNNN.json` 199 + `FileNotFoundError … /objects/…/00000278.bin` 70 |
| 크래시에 등장한 스크립트 이름 | `s101.json` … `s200.json` — **100개 전부**, C 가 만든 스크립트는 s01…s100 뿐 |
| FileNotFoundError 의 세션 id | 예: `01a06c29-d7d5-775d-…` → uuid7 타임스탬프 **11:24:36** = **A n=200 실행** |
| api 로그 | 정상(accept 100+100, `store path failed` 0) |

**C 의 stt-worker 는 C 의 세션이 아니라 A n=200 이 남긴 세션 ~170개를 물고 크래시 루프를 돌았다.**
- `script_ref` s101–s200 은 A n=200 의 스크립트 세트이고 C 의 workdir 에는 없다 → `ScriptError`.
- 남은 세션의 `audio_chunks.storage_key` 는 **상대 경로**라 C 의 objectstore 루트(`C-n100-…/objects`)에 붙어 해석되는데,
  A n=200 의 objects 는 그 실행이 끝나며 삭제됐다 → 갭 rebuild 의 `objectstore.get` 이 `FileNotFoundError`.
  `stt_rebuilds_total 70` 은 이 70번의 rebuild 시도와 정확히 일치한다 — C 의 세션이 아니라 **A 의 잔해**를 센 숫자였다.
- 잔해가 남은 이유: 스펙 §7.4 상 `stt:active` 에서 세션을 지우는 것은 **엔드 마커 flush 성공 경로뿐**이다.
  A n=200 에서 stt-worker 가 200 중 104 만 잡았으므로 나머지는 영원히 `stt:active` 에 남는다.
  세션 id 는 uuid7 이라 정렬하면 **오래된 잔해가 항상 앞**이고, `run_once` 는 정렬 순회라 잔해부터 집는다.

DB 로 교차 확인 (`postgresql://app:app@localhost:5432/chartwire`, 테넌트 `loadtest`, 생성 분 단위):

| 생성 시각 | 세션 | 현재 세그먼트 | JSON 이 기록한 `segment_rows` |
|---|---|---|---|
| 11:21 (A n=50) | 50 | 2,578 | 2,578 |
| 11:23 (A n=100) | 100 | 5,164 | 5,164 |
| 11:24 (**A n=200**) | 200 | 3,855 | **0** |
| 11:28 (**C**) | 100 | 726 | **0** |
| 11:29 (D) | 100 | 4,116 | 3,890 |

A n=200 과 C 의 세그먼트는 **그 실행이 끝난 뒤**에 만들어졌다 — 다음 실행(B, C, D)의 stt-worker 가 `stt:active` 에
남은 잔해를 뒤늦게 처리한 결과다. 오염이 실행에서 실행으로 전파됐다.

## 2. 근본원인

### RC1 (시스템 결함) — redis-py 의 기본 풀 상한 100

`redis/client.py::get_redis` 와 `api/app.py::build_deps` 가 `Redis.from_url(...)` 을 `max_connections` 없이 호출한다.
설치된 **redis 8.1.0** 의 `ConnectionPool.__init__` 은 `max_connections = max_connections or 100` 이고,
이 값은 큐가 아니라 **하드 상한**이다 — 101번째 동시 명령은 즉시 `MaxConnectionsError` 로 실패한다.

마이크로 재현(부하 없이, Redis index 15):

```
pool max_connections = 100
concurrent blocking XREADGROUP n= 99 -> errors   0
concurrent blocking XREADGROUP n=101 -> errors   1 {'MaxConnectionsError'}
concurrent blocking XREADGROUP n=150 -> errors  50 {'MaxConnectionsError'}
concurrent blocking XREADGROUP n=200 -> errors 100 {'MaxConnectionsError'}
```

- **stt-worker**: 세션 하나당 컨슈머 태스크 하나가 `XREADGROUP … BLOCK 1000` 으로 **연결 하나를 1초 동안 점유**한다(§7.4).
  100 세션을 넘는 순간 풀이 마르고, 그 다음부터는 컨슈머뿐 아니라 **디스커버리(SMEMBERS)·리스 취득(SET NX)·갱신(Lua)까지**
  같은 풀에서 실패한다. → `session acquired` 가 104에서 멈추고, 세그먼트가 하나도 안 나온 이유.
- **api**: ingest 200 + watch 200 소켓이 동시에 명령을 발행한다. 이벤트 루프가 포화되면 in-flight 명령 수가 쌓여
  풀 상한에 닿고, `_xadd_with_retry` 가 3회 재시도 후 `DependencyFailure` → `4503 store path failed` 로 닫는다.
  → `store path failed` 600, 재접속 651, resume 600.

n=50/100 이 깨끗하고 n=200 만 붕괴한 이유가 이것이다. CPU/DB 한계가 아니라 **세션 수에 선형인 연결 수요가 상수 100에 부딪힌 것**.

### RC2 (하네스 위생 + 견고성) — `stt:active` 잔해의 크래시 루프

RC1 이 만든 잔해가 이후 실행을 오염시켰다(§1.2). 두 갈래로 고친다.
- 하네스: 실행 시작 시 이번 실행에 속하지 않는 **load 테넌트** 세션을 `stt:active` 와 핫 키에서 제거.
- 시스템: 같은 세션에서 연속으로 크래시하는 컨슈머를 무한 재취득하지 않도록 격리(quarantine).

### RC3 (관측성) — `extra=` 가 로그에 찍히지 않았다

`core/logging.py::configure` 가 `logging.basicConfig(format="%(message)s")` 로 stdlib 로거를 설정한다.
런타임 모듈은 `logging.getLogger(__name__)` 를 쓰므로 `log.warning("store path failed", extra={"error": …})` 의
**`extra` 가 통째로 사라진다**. 600건의 실패 원인을 저장 로그만으로 확정할 수 없었던 이유이고, 운영에서도 같은 일이 난다.

## 3. 코드 수정

| 파일 | 변경 |
|---|---|
| `core/config.py` | `Settings.redis_max_connections: int = 512` (`CHARTWIRE_REDIS_MAX_CONNECTIONS`) |
| `redis/client.py` | `get_redis(..., max_connections=None)` — 기본값을 설정에서 명시적으로 주입 |
| `api/app.py` | `build_deps` 의 `Redis.from_url(..., max_connections=settings.redis_max_connections)` |
| `stt/worker.py` | `SttWorkerConfig.max_sessions`(기본 = 풀 상한 − `RESERVED_CONNECTIONS` 32, `CHARTWIRE_STT_MAX_SESSIONS`) — 상한을 넘으면 **세션을 잡지 않고 남겨 둔다**(잡았다가 떨어뜨리는 대신). 세션별 연속 크래시 `QUARANTINE_AFTER`=3 회면 `QUARANTINE_S`=60 s 동안 재취득 금지 + `session quarantined after repeated crashes` 로그 |
| `core/logging.py` | `ExtraFormatter` — `extra` 를 `redact_dict` 를 통과시켜 메시지 뒤에 JSON 으로 붙인다(트레이스백 앞). PHI 규칙(§0.9) 유지 |
| `loadtest/runner.py` | `clear_stale_state()` — 실행 시작 시 이번 실행에 속하지 않는 load 테넌트 세션을 `stt:active`/`sess:*`/`stt:owner:*`/`stt:lag` 에서 제거하고 개수를 리포트에 `stale_sessions_evicted` 로 기록. `db_check_at_s` 기록. `db_check` 가 `sessions_transcribed` 도 센다 |
| `loadtest/report.py` | `DbCheck.sessions_transcribed` + `as_dict` 노출, `stt_offsets_complete` 의 "검사 시점" 의미를 독스트링에 명시 |

## 4. 재측정

명령(박스는 다른 부하 없이 유휴 상태, 직렬):

```bash
source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a
export CHARTWIRE_DATABASE_URL=postgresql+asyncpg://chartwire_app:chartwire_app@localhost:5432/chartwire_load
export CHARTWIRE_DATABASE_OWNER_URL=postgresql+psycopg://chartwire_owner:chartwire_owner@localhost:5432/chartwire_load
psql postgresql://app:app@localhost:5432/postgres -c 'DROP DATABASE IF EXISTS chartwire_load;'
for n in 50 100 200; do chartwire loadtest A --sessions $n --duration 60 --out docs/loadtest; done
chartwire loadtest B --sessions 50  --duration 60 --out docs/loadtest
chartwire loadtest C --sessions 100 --duration 60 --out docs/loadtest
chartwire loadtest D --sessions 100 --duration 60 --out docs/loadtest
chartwire loadtest results --out docs/loadtest
```

### 4.1 왜 전용 DB(`chartwire_load`)인가 — 세 번째 오염원

A/B/C/D 를 세 번 돌리는 동안 같은 n 이 실행마다 달라졌다(A n=100 ack p50 163 → 288 ms, postgres CPU 평균 4 % → 16 %).
개발 DB `chartwire` 에는 **벌크 로더 코퍼스**(세션 20,000 · `transcript_segments` 208만 · `segment_search` 128만 · `patients` 5만)와
부하 실행마다 쌓인 행·블로트(`outbox_events` 는 행 6,178개에 **608 MB**)가 있었다. 부하 실행은 그 위에서 돌면서 오토배큠과
경합했고, 실행을 거듭할수록 느려졌다 — **재현 불가능한 측정**이다.

전용 DB 로 옮기니 postgres CPU 가 평균 1–2 % 로 떨어졌고 같은 n 의 재현성이 회복됐다. 역할 분담을 이렇게 정리한다:
**코퍼스 규모에서의 쿼리 성능은 `docs/perf/`(WP-F, 벌크 DB)가 재고, 부하 시나리오는 정의된 빈 DB에서 잰다.**
각 리포트의 `database` 필드에 어느 DB 였는지 남긴다(신규). 이전 A/B/C/D JSON 은 개발 DB 에서 측정된 것이라 비교 대상이 아니다.

### 4.2 A — 재측정 결과 (모두 같은 빌드, `chartwire_load`, 잔여 세션 0)

| N | chunks/s | ack p50 | ack p95 | final e2e p95 | alert e2e p95 | credit min | ended | `stt_offsets` 완결 | 세그먼트 | 클라 오류 | drain 대기 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 250 | 81 ms | 223 ms | 208 ms | 57 ms | 50 | 50/50 | **50/50** | 2,578 | 0 | 10.1 s |
| 100 | 500 | 182 ms | 449 ms | 893 ms | 65 ms | 46 | 100/100 | **100/100** | 5,164 | 0 | 18.1 s |
| 200 | 790 | 4,512 ms | 9,036 ms | 8,879 ms | 230 ms | 17 | 158/200 | **158/158** | 8,099 | 0 | 30.1 s |

붕괴 전/후 대비 (n=200):

| | 수정 전 | 수정 후 |
|---|---|---|
| `db.segment_rows` | **0** | 8,099 |
| 뷰어가 받은 final | **0** | 3,300+ (표본), `final_dups` 0 |
| `stt_offsets_complete` | **0/200** | **158/158**(종료된 세션 전부) |
| 클라이언트 `errors` / 재접속 | **651 / 651** | **0 / 0** |
| api `store path failed` | **600** | **0** |
| stt-worker 크래시 | 3 (`MaxConnectionsError`) | **0** |
| stt-worker 가 잡은 세션 | 104/200 | **200/200** |
| loss / dup | 0 / 0 | 0 / 0 |

### 4.3 A n=200 은 여전히 무릎(knee)이다 — 증거

수정 뒤에도 n=200 은 **의도한 1,000 chunk/s 를 못 낸다(790)**. 60 s 안에 300 청크를 다 보낸 녹음기는 158/200 이고
ack p50 이 4.5 s 다. 이것은 결함이 아니라 **프로세스 단위 포화**다.

1. **stt-worker 는 코어 하나에 못 박혀 있다.** CPU 평균/최대: n=50 57.1/100.7 % → n=100 90.0/100.6 % → n=200 80.7/101.4 %.
   최대가 세 번 모두 **101 % 근처에서 멈춘다** — 단일 이벤트 루프(CPython 한 프로세스)의 천장이다.
2. **api 도 프로세스 천장에 있다.** psutil 프로세스 CPU 최대 117 %(코어 0–1 에 핀 = 200 % 가용).
   진단 실행에서 **스레드 단위**로 재보니(`scratchpad/thread_probe.py`, 91 표본):
   `loop_thread_cpu_pct_p50 81 %` · `max 83 %` · **`process_cpu_pct_p50 100 %` · `max 110 %`** · 스레드 14개.
   즉 이벤트 루프 스레드가 81 %, 나머지는 `asyncio.to_thread` 오브젝트스토어 쓰기이고, **프로세스 합이 GIL 상한인 1 코어**에 붙어 있다.
3. **핀을 풀어도 좋아지지 않는다.** 같은 시나리오를 `--no-pin` 으로 돌린 진단 실행: 690 chunk/s, api 77.5/111.3 %, stt 93.4/101.5 %.
   코어 배분 문제가 아니라 **프로세스 하나가 코어 하나 이상 못 쓰는 문제**다.
4. **아래층은 한가하다.** postgres CPU 평균 1–2 %(최대 7 %), redis 평균 6–8 %(최대 13 %), DB 풀 대기·Redis 지연으로 인한 실패 0,
   `loss` 0, `dup` 0, nack 0. 병목은 저장소가 아니라 api·stt-worker 프로세스의 파이썬 실행이다.
5. **백프레셔는 설계대로 동작한다.** credit 최소 17(0 이 아님), `pause` 0, 스트림 상한 유지, 원장 손실 0 —
   과부하에서 **떨어뜨리지 않고 느려진다**.

결론: 이 박스(4 vCPU)의 무릎은 **100 세션과 200 세션 사이**, 실효 처리량 상한은 **≈ 800 chunk/s(≈ 160 세션 상당)**.
그 위로 가려면 코어를 더 주는 게 아니라 **api 를 여러 uvicorn 프로세스로, stt-worker 를 여러 개로** 늘려야 한다
(스펙 §11.2 의 시나리오 E 가 그 방향, 미실행). 스펙 §11.2 의 "N=200 → 1,000 chunk/s, ack p95 80–200 ms" 기대는
**단일 프로세스 토폴로지에서는 이 박스에서 달성되지 않는다** — 기대치를 낮추지 말고 사실대로 기록한다.

### 4.4 B / C / D

| 시나리오 | 결과 | 스펙 §11.2 기대 |
|---|---|---|
| B (SlowStt 400 ms, N=50) | credit 0 도달 39.5 s · credit 0 세션 50/50 · `pause` 90 · 스트림 최대 **301 < 2000** · api RSS 기울기 2.473 MB/min · loss 0 · 소화 후 세그먼트 2,578(= A n=50 과 동일) | credit 0 도달, 유한 큐, 평평한 RSS ✅ |
| C (느린 뷰어 20 %, N=100) | **배달된 final 5,164 = A n=100 세그먼트 수와 정확히 일치** · `final_dups` 0 · `dropped_partials` 0 · `lagged_dropped` 0 · 녹음기 ack p95 355.5 vs A n=100 449.4 → **−20.9 %** · loss 0 | 느린 뷰어가 녹음기를 막지 않는다 ✅ |
| D (카오스, N=100) | resume **140/140 = 100 %** · superseded(4409) **0** · rebuild 57 · **`stt_offsets_complete` 100 %** · 세그먼트 연속 100 % · ended 100 % · final 5,164(전량) · loss 0 · dup 0 | 100 % / 0 / 0 ✅ |

C 에 대한 정직한 단서: **−20.9 %** 는 "느린 뷰어가 녹음기를 20 % 빠르게 했다"는 뜻이 아니다.
이 박스의 n=100 ack p95 는 실행 간 편차가 크다(같은 빌드에서 342.7 / 449.4 ms 관측 = ±25 %).
따라서 말할 수 있는 것은 **"측정된 차이가 실행 간 편차 안에 있다 = 느린 뷰어의 결합이 관측되지 않는다"** 까지이고,
스펙의 "변화 < 10 %" 판정은 이 편차보다 촘촘해서 단일 실행 쌍으로는 결론지을 수 없다. `docs/limitations.md` 에 넣을 문장이다.

## 5. `stt_offsets_complete` 판정 — 메트릭도 시스템도 아니고 **하네스가 너무 일찍 읽었다**

- B 0 / C 0 / D 70 / A n=50·100 만점이라는 이상한 분포의 원인은 셋이 섞인 것이었다:
  1. **C 와 A n=200 의 0** = §2 의 진짜 결함(풀 상한 → 컨슈머가 죽음, 잔해가 다음 실행을 잡아먹음). 시스템 결함.
  2. **D 의 70** = 40 s 에 15 s SIGSTOP 을 건 직후, **고정 20 s** 꼬리 대기 안에 밀린 7,500 청크를 다 소화하지 못한 상태를 찍은 값.
  3. **B 의 0** = `SlowStt(400 ms)` 는 **설계상** 청크당 400 ms 다. 파이프라인은 정상이고 아직 안 끝났을 뿐이다.
- 스펙 §11.2 는 이것을 "**final** invariant" 라고 부른다. 그런데 러너는 마지막 녹음기 종료 + 고정 20 s 에 읽었다.
  → **불변식을 stt-worker 백로그와의 경주로 바꿔 놓은 것은 하네스**다. 메트릭 정의(`last_chunk_seq == final_seq`)도,
  시스템 동작도 옳았다.
- 조치: `wait_stt_drain()` 을 db 대조 **앞**에 넣었다(뷰어는 아직 붙어 있어 이때 오는 final 도 측정된다).
  상한 `--drain-wait`(기본 120 s) + 무진전 30 s 조기 종료. 진전 신호는 세션 수가 아니라 **`sum(stt_offsets.last_chunk_seq)`** —
  B 는 50 세션이 동시에 끝나 세션 수가 계단처럼 떨어지므로 세션 수로는 "정체"로 오판된다(실제로 첫 시도에서 오판했다).
- 결과: A 50/100/200 · B · C · D **전부** `stt_offsets_complete = 종료된 세션 수`(= 100 %). 리포트에 `stt_drained`,
  `stt_drain_wait_s`, `db_check_at_s`, `sessions_transcribed` 를 함께 남겨 "언제 읽은 값인지"가 숫자와 같이 다니게 했다.
  `results.md` 에도 시나리오마다 "DB 대조" 표로 렌더한다.

## 6. 게이트

- `ruff check src` clean · `ruff format` clean · `mypy` strict 5 모듈 + `src/chartwire/loadtest` clean
- `pytest tests/unit` **736 passed** · `pytest tests/ws` **149 passed**
- `pytest tests/integration/test_stt_worker.py test_stt_worker_chaos.py test_alerts.py` (DB c/Redis 3) **14 passed**
- `pytest tests/integration/test_ws_{state,ingest,watch,drain}.py` (DB b/Redis 2) **35 passed**
- `python scripts/readme_numbers.py --check`: 등록되지 않은 키 **0**, load 키 전부 해석됨(README 는 아직 `--write` 전이라 stale — 통합자 몫)
- `chartwire loadtest results --out docs/loadtest` 로 `docs/loadtest/results.md` 재생성 완료

## 7. 알려진 이슈 / 남긴 것

- **A n=200 은 무릎으로 기록**했다(§4.3). README 에 쓸 때 "1,000 chunk/s 목표 대비 790" 을 숨기지 말 것.
  n=200 의 ack p50 은 실행마다 0.65 s ~ 4.5 s 로 크게 흔들린다(무릎 위에서는 정상). 대표값 하나로 쓰지 말고 무릎이라고 쓰는 게 정직하다.
- **C 의 delta 는 실행 간 편차보다 작다**(§4.4). "< 10 %" 판정은 반복 측정 없이는 못 한다.
- `stt_lag_chunks` 는 실행이 끝난 뒤 스크레이프한 게이지라 약간 뒤처진 값이다(B 976, D 308). 추세용.
- 이전 실행의 잔여 세션은 이제 러너가 지우지만, **개발 DB(`chartwire`)에는 아직 `recording` 세션과 그 `stt:active` 멤버가 남아 있다**.
  데모(`serve all`)의 stt-worker 가 그걸 집으면 다른 DB 라 세션을 못 찾고 `active session not found in any tenant; dropping` 로
  스스로 `SREM` 한다(자가 치유). 문제는 없지만 첫 기동 로그에 경고가 몇 줄 나온다.
- `objectstore/localfs.py::_put_sync` 는 청크마다 `mkdir(exist_ok=True)` + `.tmp` 쓰기 + `os.replace` 로 syscall 3회를 쓴다.
  api 프로세스 CPU 의 약 20 %가 이 스레드풀이다(§4.3-2). 디렉터리 캐시로 줄일 수 있지만 원자성 계약을 건드리므로 손대지 않았다 — 관찰만 기록.
- **`docs/eval/alert_latency.json` 은 마지막 A 실행(=n=200)이 덮어쓴다**(WP-H 편차 6, 의도된 설계). 그런데 n=200 은 이제
  무릎 위 실행이라 이 파일은 **포화 상태의 값**이다: `p50 47.4 / p95 229.8 ms`(표본 세션 83, 경보 192).
  같은 빌드의 A **n=100** 은 `alert_e2e_p95 65.3 ms` 다 — 3.5배 차이. 스펙 §11.1 의 표본은 "100 sessions" 이므로
  통합자가 n=100 쪽을 쓰고 싶다면 **A 를 200 → 50 → 100 순서로 돌려 마지막을 n=100 으로 만들고 C 를 다시 돌리면 된다**
  (C 는 A.json 의 n=100 ack p95 를 참조하므로 A 뒤에 와야 한다). 나는 스펙의 임계값(p95 < 300 ms)을 둘 다 통과하므로
  보수적인 쪽(현 상태, 포화 실행)을 그대로 뒀다.
- `docs/loadtest/H.json` 은 이번 조사와 무관해 재측정하지 않았다(아웃박스 벤치는 ws/stt 경로를 타지 않는다).
  다만 H 는 **개발 DB `chartwire`** 에서 측정된 값이고 러너를 거치지 않아 `database` 출처 필드가 없다.
- `chartwire_load` DB 를 남겨 뒀다(다음 측정도 여기서). 지우려면 `DROP DATABASE chartwire_load`.
- git 상태는 바꾸지 않았다(커밋/스테이징 없음).

## 8. `docs/AGENTS.md` 버그 저널에 넣을 문단 (초안)

> **부하 n=200 붕괴의 진범은 redis-py 의 기본값이었다.** A n=200 은 세그먼트 0개, 녹음기 재접속 651회로 무너졌고
> "4 vCPU 의 한계" 처럼 보였다. 저장된 stt-worker 로그에는 `MaxConnectionsError` 가 세 줄 있었다. redis-py 8.1 의
> `ConnectionPool` 기본 `max_connections` 는 **100 이고 큐가 아니라 하드 상한**이다. stt-worker 는 세션마다
> `XREADGROUP … BLOCK 1000` 으로 연결 하나를 1초씩 점유하므로 101번째 세션부터 컨슈머는 물론 디스커버리와 리스 갱신까지
> 같이 죽는다. 세션 수에 선형인 연결 수요가 상수 100 에 부딪힌 것이라 n=50/100 은 멀쩡하고 n=200 만 전멸했다.
> 풀 상한을 설정값으로 노출하고(`CHARTWIRE_REDIS_MAX_CONNECTIONS`), stt-worker 가 그 상한 이상으로 세션을 **잡지 않도록**
> 했다(잡았다가 떨어뜨리는 대신 다음 워커에 남긴다). 교훈 셋: (1) 라이브러리 기본값은 스펙이 아니다 — 연결 수요가
> 동시성에 선형이면 상한을 직접 정해라. (2) 진단이 안 되는 로그는 로그가 아니다: `logging.basicConfig(format="%(message)s")`
> 때문에 `extra=` 가 전부 사라져 600건의 실패 원인을 저장물로 확정할 수 없었다. (3) 한 번의 붕괴는 다음 실행을 오염시킨다 —
> 끝나지 못한 세션이 `stt:active` 에 남아 다음 시나리오의 워커를 크래시 루프에 빠뜨렸고, 그래서 시나리오 C 의
> "느린 뷰어 격리 2.4 %" 라는 **아무것도 배달되지 않은 상태에서 계산된 숫자**가 나왔다. 실패한 실행의 잔해를 지우는 것은
> 하네스의 일이고, 같은 세션에서 반복 크래시하는 컨슈머를 무한 재취득하지 않는 것은 워커의 일이다.
