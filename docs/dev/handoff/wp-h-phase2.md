# WP-H Phase 1/2 핸드오프 — loadtest 시나리오 · CI 마무리 · README 골격 · AGENTS.md

> 상태: **완료** (2026-09-04). 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. git 상태를 바꾸는 명령은 실행하지 않았다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a` + DB `chartwire_test_h`, Redis 8, 포트 8120(api)/8121(worker ops)/8122(stt-worker ops).
> 게이트: `pytest tests/unit` **736 passed**(신규 `test_loadtest_report.py` 18 포함) · `ruff check src tests scripts` clean · `ruff format --check` 311 files ·
> `mypy` strict 5 모듈 clean(+ `mypy src/chartwire/loadtest` clean) · `python infra/cdk/app.py` nag 0 · `npx --yes aws-cdk@2 synth --quiet` nag 0, 커밋된 템플릿 diff 0 ·
> `docker compose config -q` / `--profile drain` OK(도커 데몬은 이 박스에 없어 `docker build` 는 CI 에서만) · `sha256sum -c FROZEN.txt` OK ·
> `python scripts/readme_numbers.py --check` = exit 1(미측정 행 35개 삭제 대상, 등록되지 않은 키 0 — 측정 전의 **의도된** 상태).

## 0. 체크리스트

- [x] 문서/코드 읽기 (AGENT_ENV, 핸드오프 13개, 스펙 §0–3/§11/§12.3/§14/§15, `loadtest/client.py`, `scripts/readme_numbers.py`, `outbox/bench.py`, seed/repo 시그니처)
- [x] `loadtest/scenarios.py` — A/B/C/D 정의, `chaos_schedule`, `pick_kill_targets`, `slow_viewer_indexes` (순수)
- [x] `loadtest/runner.py` — 프로세스 3개 기동(taskset), 시드, psutil 샘플러, 클라이언트 구동, 카오스, DB 대조, 메트릭 스크레이프, 리포트
- [x] `loadtest/report.py` — 집계/백분위/RSS 기울기, A/B/C/D 본문(KEYS 모양), `alert_latency.json`, `results.md` 렌더
- [x] `loadtest/cli.py` — `chartwire loadtest A|B|C|D|H|results …`
- [x] `loadtest/client.py` 두 곳 수정 (§1 표)
- [x] `tests/unit/test_loadtest_report.py` (18, 순수 파이썬; KEYS 레지스트리로 실제 해석까지)
- [x] 스모크(scratch 에만): A N=10 20 s · H 2 K · 축약 D N=5 20 s — `docs/` 에는 JSON 을 남기지 않았다(`docs/eval/alert_latency.json` 스모크 산출물은 삭제)
- [x] `.github/workflows/ci.yml` — eval-smoke / load-smoke / cdk / docker / frozen-artifacts (통합자의 직렬 integration 잡 유지)
- [x] `Makefile` — `loadtest-a`(50/100/200 루프) `-b` `-c` `-d` `-h` `loadtest-smoke` `loadtest-results`, `cdk-synth` 는 python app.py + 테스트 + 템플릿 diff
- [x] `README.md` 골격 (§14 순서 12 절, 마커 `num`/`row` 만, 라이선스 절 없음, 한국어)
- [x] `docs/architecture.md` (+ REDI/마음편의점 재사용 문단), `docs/AGENTS.md`, `docs/limitations.md`, `docs/loadtest/results.md`(JSON 없는 상태의 렌더)
- [x] 이 문서

## 1. 무엇을 만들었나 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `loadtest/scenarios.py` (137) | `Scenario` frozen dataclass(세션 수, 길이, stt 프로바이더/지연, 느린 뷰어 비율·지연, kill 비율·주기, flush/sigstop 시각), `SCENARIOS["A".."D"]`, `A_SESSION_COUNTS=(50,100,200)`, `chaos_schedule()`(정렬된 `ChaosEvent` 목록: kill 10/20/…, flush 30, sigstop 40 → sigcont 55), `pick_kill_targets()`(ceil(n×비율), 결정론 RNG), `slow_viewer_indexes()` | §11.2 |
| `loadtest/runner.py` (≈760) | `RunConfig`; `ensure_schema()`(bootstrap-roles + upgrade head, 멱등); `seed_load_tenant()`(테넌트 `loadtest` 1회 생성, 임상의 `clinician@load.clinic`, 환자 `가상환자-5001…`(N개, 4 스코프 동의), 세션 N개 `created` + 래핑 DEK + `script_ref` s01..sNN, 스크립트는 실행별 workdir 에 `generate_set("demo", N, seed)` 로); `spawn_processes()`(api `taskset -c 0-1`, worker/stt-worker `-c 2-3`, 자식 env 로 오브젝트 스토어·스크립트 디렉터리·STT 프로바이더·ops 포트 주입) → `wait_ready()`(`/readyz`) → 클라이언트 프로세스 자신은 `sched_setaffinity({3})`; `Sampler`(psutil 1 s: api/worker/stt-worker/postgres(전 백엔드 합)/redis/client CPU %·RSS + 모든 세션 스트림 `XLEN` 최대); `RedisTicketSource`(프로세스 내 `tickets.issue`); `build_clients()`(세션당 `RecorderClient` + 뷰어, C 는 매 k번째 세션 뷰어에 200 ms 지연); `run_chaos()`(abort/`FLUSHDB`/SIGSTOP/SIGCONT, 실제 시각을 저널에); `_wait_recorders()`(기간 + 램프 + 30 s 안에 못 끝난 녹음기는 `stop("duration_exceeded")` — B 의 credit 0); `_wait_viewers_tail()`(모든 뷰어가 `session.state{transcribed}` 를 볼 때까지 ≤ `--tail-wait`); `scrape()`(Prometheus 텍스트 → dict; `ws_dropped_partials_total`, `ws_resume_total{result}`, `stt_rebuilds_total`, `stt_lag_chunks`); `db_check()`(세션별 `audio_chunks` 수 vs 전송 수 → `loss`, `sessions.state/final_seq`, `stt_offsets.last_chunk_seq == final_seq`, 세그먼트 seq 연속, `risk_events`); `stop_processes()`(SIGCONT → SIGTERM → 30 s → kill, 종료 코드 기록); `write_results_md()` | §11.2 |
| `loadtest/report.py` (≈420) | `ResourceSample`/`rss_slope_mb_per_min`(최소제곱, MB/min)/`summarize_resources`; `DbCheck`; `aggregate()`(풀링 백분위 ack p50/p95/p99, final/alert e2e, credit min·0 도달 시각·세션 수, pause, nack, reconnect/resume, 4409 수, loss_client, finals/dups, partials, alerts, lagged, outcomes); `a_run`/`merge_a_runs`(같은 n 은 교체, n 정렬), `b_body`, `c_body`(A 같은 n 의 ack p95 대비 delta), `d_body`(불변식 %, 카오스 저널), `alert_latency_body`; `finish()`(=`build_report` 헤더); `render_results_md()`/`load_reports()` | §11.2, §11.4 |
| `loadtest/cli.py` (≈160) | `chartwire loadtest <A|B|C|D|H|results> [--sessions N] [--duration 60] [--out docs/loadtest] [--seed 42] [--api-port 8120 --worker-port 8121 --stt-port 8122] [--cores-api 0-1 --cores-services 2-3 --cores-client 3] [--pin/--no-pin] [--ramp 2] [--tail-wait 20] [--migrate/--no-migrate] [--workdir] [--keep-workdir] [--eval-dir docs/eval] [H: --events 100000 --workers 2 --tenants 30 --kill-at 0.25]`. 종료 코드 0 = loss 0(H: dlq 0). 헤드라인 JSON 을 stdout 에 | §13.1 |
| `loadtest/client.py` (수정) | ① `SessionStats.credit_zero_at_s` + `observe_credit(credit, elapsed_s=)` (녹음기 시작 기준 경과 초; B 의 `credit_zero_at_s`), ② `ViewerConfig.chunk_ms` + `last_chunk_seq(t_end_ms, chunk_ms)` — `final_e2e` 를 **세그먼트 seq 가 아니라 발화의 마지막 청크 seq** 로 `sent_at` 조회(§11.2 정의, 콘솔과 같은 `t_end_ms → seq` 역산). 이전 코드는 세그먼트 seq(0부터)로 청크 `sent_at`(1부터)를 찾아 값이 무의미했다 | §11.2 |
| `tests/unit/test_loadtest_report.py` (18) | 시나리오 표, 카오스 스케줄 정렬/절단, kill 대상 결정론, `last_chunk_seq` 경계, 집계(풀링·null 정책·credit 0 1회 기록), RSS 기울기, A/B/C/D 본문 키, A 병합, `alert_latency`, **KEYS 레지스트리로 `load.*`/`eval.alert_latency.*` 전부 해석**(fixture 에서 정당하게 null 인 2개만 제외), results.md 렌더, CLI 오류/`results`/요약, 루트 CLI 마운트 | |
| `.github/workflows/ci.yml` | §2 | §12.3 |
| `Makefile` | 위 체크리스트 | §12.2 |
| `README.md` | §14 순서: 배너(합성·키 없음·임상 불가·STT/노트 미평가) + CI 배지 → 8 문제 표 → 실측 표 ①②③(마커만; 행 단위 `row` 로 감싸 미측정 시 삭제) → 아키텍처(mermaid)·프로토콜 요약 → 테넌시·검색 → 동의·파기·보존 → SOAP 어댑터(thin, hedged; coverage 1.0 구성상 당연; Anthropic 미실행) → 위험 경보 → 운영 → 5분 실행법 + 재현 명령 표 → AGENTS 요약 → 한계·채용공고 매핑·기존 저장소 | §14 |
| `docs/architecture.md` | 구성 요소·프로세스 표·데이터 흐름 6 단계·ADR 표·**REDI/마음편의점 재사용 문단**·관측/배포 | §14 |
| `docs/AGENTS.md` | 진행 방식, WP 표(§15), 계약(확장 포함), 게이트 표, 처음부터 알려 준 함정 13개(발견 아님으로 표기), **버그 저널**(핸드오프 인용, WP 별) | §14 |
| `docs/limitations.md` | 미평가(STT·노트·Anthropic·임상 정확도), 합성 상한, 클라이언트 혼입, 단일 리전/Render, 보존 자동화는 플래그까지, 갭 | §12.2, §14 |
| `docs/loadtest/results.md` | `chartwire loadtest results` 가 JSON 에서 렌더(현재는 전부 "미측정") — CI frozen-artifacts 가 재렌더 diff 로 검사 | §11.2 |

LOC(비공백, 신규): 구현 ≈ 1,480 / 테스트 ≈ 330. Phase 0(client 824, console 1,009, cdk ≈ 500)과 합치면 §0 예산(loadtest 는 F 와 합산 1,900; console 900; infra 600)을 넘는다 — 러너의 프로세스 감독·DB 대조·카오스가 대부분이며 줄이지 않고 보고한다.

## 2. CI (`.github/workflows/ci.yml`)

| 잡 | needs | 내용 | 현재 상태(이 박스 기준) |
|---|---|---|---|
| `eval-smoke` | unit | `sha256sum -c FROZEN.txt` → `chartwire eval all --seed 7 --n 20 --per-class 20 --check --out $RUNNER_TEMP/eval` → JSON 아티팩트 | **빨강 예상**: 이 박스에서 같은 명령이 `[임계값 미달] held-out recall 0.468 < 0.6` 로 실패(injection leaks 는 0, paraphrase 0.0147 통과). WP-C 의 재현율 문제(WP-F 요청 1)이지 하네스 문제가 아니다. 임계값을 낮추지 않았다 — 통합자 결정 사항(§4) |
| `load-smoke` | integration | PG16/Redis7 서비스, DB `chartwire_load`(러너가 생성·마이그레이션), `chartwire loadtest A --sessions 20 --duration 30 --no-pin --tail-wait 15 --out $RUNNER_TEMP/loadtest --eval-dir …` → 파이썬 임계값(ack p95 < 1,000 ms, loss 0, 20/20 ended) → JSON·results.md·프로세스 로그 아티팩트 | 이 박스 A N=10 20 s: ack p95 200 ms, loss 0 |
| `cdk` | lint | Node 22 + `.[dev,infra]`; `python app.py`(nag) → `pytest infra/cdk/tests` → `npx --yes aws-cdk@2 synth --quiet` → `git diff --exit-code -- infra/cdk/cdk.out/ChartwireStack.template.json` | 둘 다 이 박스에서 통과, diff 0 |
| `docker` | lint | `docker build -t chartwire:local .` → `docker run --rm chartwire:local chartwire --version` → `docker compose config -q` (+ `--profile drain`) | compose config 통과; build 는 데몬이 없어 **CI 에서만** 검증(스펙 §0 "Docker daemon uncertain") |
| `frozen-artifacts` | lint | `sha256sum -c FROZEN.txt` → `python scripts/readme_numbers.py --check`(stdlib) → `chartwire loadtest results` 재렌더와 `docs/loadtest/results.md` diff | `--check` 는 측정 전까지 exit 1(**의도**: README 에 미측정 행이 있다는 신호; Phase 2 의 `--write` 뒤 초록) |

WP-A 골격 주석의 `eval-smoke: chartwire eval all --seed 42 --smoke` 는 실제 CLI 옵션(`--seed 7 --n 20 --check`, WP-F 요청 7)으로 바꿨다. integration 잡의 그룹/순서/`tests/chaos` 는 통합자가 정한 그대로.

## 3. 스모크 결과 (하네스 검증용 — README 숫자 아님, scratch 에만 저장)

| 실행 | 결과 |
|---|---|
| `chartwire loadtest A --sessions 10 --duration 20` (핀 고정, DB test_h/Redis 8) | 1,000 청크, loss 0, dup 0, ended 10/10, `stt_offsets` 10/10, 세그먼트 연속 10/10, ack p50/p95/p99 ≈ 177/200/203 ms, final e2e p95 172 ms, alert 샘플 7(4 세션) p95 23 ms, credit min 50, api CPU 평균 17 %, 종료 코드 api −15(uvicorn 이 SIGTERM 을 되던짐 — WP-G §4-10) / worker 0 / stt 0 |
| `chartwire loadtest H --events 2000 --workers 2 --tenants 5` | 185 ev/s(2 K 는 리스 대기가 지배 — WP-G §6), dlq 0, worker 0 SIGKILL → reclaimed 200, claim p50/p95 4.5/7.2 ms |
| 축약 D (파이썬으로 `replace(D, kill_every_s=5, flush_redis_at_s=8, stt_sigstop_at_s=12, stt_sigstop_for_s=3)`, N=5, 20 s) | 소켓 kill 3회(8 재접속) → resume 8/8 = 100 %, FLUSHDB → 5 세션 4503 → 재수화(`ws_resume_total{ok}=8`), SIGSTOP/SIGCONT → 소비자 그룹 재생성 + 원장 rebuild(`stt_rebuilds_total=1`), loss 0, dup 0, `stt_offsets`/세그먼트 연속/ended 100 % |

## 4. 다른 WP / 통합자에 대한 요청 (정확한 diff)

1. **WP-B (`ws/core.py`, `ws/ingest.py`) — ack 100 ms 규칙이 지난 틱 시각으로 평가된다.** `on_ledgered` 가 `self.now_ms` 를 갱신하지 않아 `_maybe_ack` 의 `self.now_ms - self._last_ack_ms >= ACK_EVERY_MS` 가 마지막 `on_chunk`/`on_tick` 시각으로 계산된다. 5 chunk/s(청크 간격 200 ms) 에서는 원장 커밋(+50 ms) 시점에 규칙이 거짓이 되고 다음 200 ms 틱에서야 ack 가 나가 ack RTT 가 ≈ 180–200 ms 로 양자화된다(A 스모크 p50 177 ms; `simulate --speed 4` 는 8-청크 규칙이 먼저 걸려 81 ms). 스펙 §6.4 규칙 2 와 WP-B 편차 4("`on_ledgered` 에서도 100 ms 규칙을 평가")의 의도대로라면:
   ```python
   # ws/core.py
   -    def on_ledgered(self, seqs: Iterable[int]) -> list[Action]:
   -        self._ledger(seqs)
   +    def on_ledgered(self, seqs: Iterable[int], now_ms: int | None = None) -> list[Action]:
   +        if now_ms is not None:
   +            self.now_ms = now_ms
   +        self._ledger(seqs)
   # ws/ingest.py — 원장 future 콜백에서 코어를 부르는 곳
   -        actions = self.core.on_ledgered(seqs)
   +        actions = self.core.on_ledgered(seqs, now_ms=self._now_ms())
   ```
   (`tests/ws/test_ingest_core_props.py` 의 불변식은 `now_ms` 단조 가정만 지키면 그대로 통과할 것.) 반영 뒤 README 의 ack 수치는 **재측정으로만** 바뀐다.
2. **WP-C (`stt/consumer.py`)** — 축약 D 에서 stt-worker 로그에 `consumer group recreated; rebuilding from ledger` 4줄 + `rebuild` 1줄이 있는데 `stt_rebuilds_total` 은 1 이다. 그룹 재생성 경로의 재생이 카운터를 올리지 않는 것으로 보인다. `rebuild_count` 의 정의("원장 rebuild 횟수")를 지키려면 `_group_vanished` 복구 경로에서도 `STT_REBUILDS_TOTAL.inc()`. 반영 전까지 D.json 의 `rebuild_count` 는 갭 rebuild 만 센다(문서에 그렇게 적었다).
3. **통합자 — eval-smoke 빨강.** `chartwire eval all --seed 7 --n 20 --check` 가 held-out recall 0.468 < 0.6 으로 실패한다(WP-F 요청 1 미해결). 선택지는 (a) WP-C 가 방언·어간을 보강해 재현율을 올린다, (b) 스펙 소유자가 §11.1 의 smoke 임계값을 재정의한다. 나는 임계값을 건드리지 않았다(§0.2 정신). 현재 코드로 CI 를 초록으로 만들려면 (a) 가 유일한 경로다.
4. **통합자 — Phase 2 측정 순서와 명령**(스펙 §15, 유휴 박스, 직렬, 개발 DB 또는 전용 DB `chartwire_load` 를 `CHARTWIRE_DATABASE_URL` 로):
   ```bash
   set -a; . ./.env.example; set +a
   make bulk && make perf-study                     # WP-F
   make eval                                        # WP-F (var/eval/{purge,rls}.json 은 WP-E 스위트가 먼저 써야 집계됨)
   make loadtest-a                                  # A 50 → 100 → 200 (각 ≈ 2 분; A.json runs[] 병합, docs/eval/alert_latency.json 은 마지막 실행이 덮어씀)
   make loadtest-b loadtest-c loadtest-d loadtest-h # C 는 A.json 의 n=100 ack p95 를 참조하므로 A 뒤에
   make readme-numbers                              # README 마커 채움 + 미측정 행 삭제
   python scripts/readme_numbers.py --write --readme docs/perf/README.md   # WP-F 요청 6
   chartwire loadtest results --out docs/loadtest   # (loadtest 명령이 매번 갱신하지만 마지막에 한 번 더)
   ```
   `docs/loadtest/*.json` 과 `docs/eval/alert_latency.json` 은 러너가 직접 쓴다. 러너는 `CHARTWIRE_DATABASE_URL` 의 DB 를 `bootstrap-roles` + `upgrade head` 로 준비한다(`--no-migrate` 로 끌 수 있음). 실행마다 `var/loadtest/<S>-n<N>-<ts>/` 에 오브젝트/스크립트/로그가 남고 오브젝트는 끝나면 지운다(`--keep-workdir` 로 보존). 세션 200개 × 300 청크 = 60 K 오브젝트 파일(≈ 390 MB) 이 잠시 생긴다.
5. **통합자 — `docs/dev/AGENT_ENV.md`** 에 한 줄 추가를 부탁: "`chartwire loadtest …` 는 측정 창에서만; CI 와 같은 스모크는 `make loadtest-smoke`(var/loadtest/smoke, 핀 없음)".
6. **WP-E (선택)** — `POST /sessions/{id}/ws-ticket` 의 30/min 버킷 때문에 부하 러너는 티켓을 프로세스 안에서 만든다(`RedisTicketSource`). REST 티켓 경로까지 부하에 넣고 싶으면 `service` 역할이나 `X-Load-Test` 헤더로 버킷을 면제하는 옵션이 필요하다 — 요청은 아니고 기록.

## 5. 스펙과 다르게 한 점 (이유)

1. **`FLUSHALL` → `FLUSHDB`** (시나리오 D). 공유 박스 규칙(AGENT_ENV). 앱은 실행 인덱스만 쓰므로 앱 관점의 효과는 동일; 리포트 `chaos[]` 에 `FLUSHDB` 로 기록.
2. **티켓은 REST 가 아니라 `tickets.issue` 직접 호출** — §5 레이트리밋 30/min 과 200 세션 램프의 충돌. REST 티켓 경로는 통합 테스트가 고정한다(`docs/limitations.md` §3).
3. **`dup` = 뷰어에 같은 세그먼트 seq 가 두 번 배달된 수.** 스펙의 "stt 계층 재처리 seq" 는 `ConsumerStats.duplicates`(재전달 스킵) 가 로그 `extra` 로만 나가 파싱할 수 없고, DB 행 중복은 PK 로 불가능하다. 정의를 `results.md` 에 명시.
4. **`superseded_closes` = 클라이언트가 관찰한 4409 종료 수** — close 프레임 없이 끊긴 소켓은 서버 쪽 좀비 4409 를 받지 못한다(콘솔은 소켓 객체를 유지해 받는다). 스펙 기대값 0 과 일치하지만 정의를 명시. 펜싱 자체는 `ws_chunks_total{result="stale"}`.
5. **B 의 녹음기는 기간 + 램프 + 30 s 안에 못 끝나면 `stop("duration_exceeded")`** — credit 0 으로 300 청크를 60 s 에 보낼 수 없으므로 `end` 없이 끊는다(세션은 `recording` 으로 남고 reaper 가 정리). B 의 목적(credit 0, pause, 스트림 상한, RSS)에는 영향 없음; `loss` 는 DB 대조로 계산.
6. **A 의 `alert_latency.json` 은 마지막 A 실행(=N=200)이 덮어쓴다** — 스펙 "100 sessions with risk utterances" 는 세션 수가 아니라 표본 세션 수(`n_sessions`)로 기록한다. 60 s 안에 위험 발화가 나오는 세션만 표본이 된다(스크립트 타임라인 의존; N=10 스모크에서 4/10).
7. **`chartwire loadtest H` 는 `outbox bench` 위임** (`--sessions/--duration` 무시, `--events/--workers/--tenants/--kill-at`). Makefile 의 `loadtest-h` 를 그에 맞게 분리.
8. **`cdk-synth` Makefile 타깃은 `python app.py` + 테스트 + 템플릿 diff** — `npx aws-cdk@2 synth` 는 CI cdk 잡에서 추가로 돌린다(둘 다 같은 `python app.py` 를 호출).
9. **README 표 ② 는 Q1a/Q1b/Q2d 를 after 값끼리 비교하는 행** 으로 뒀다(before/after 가 아닌 변형 비교가 의미인 행). 키는 전부 등록된 것만 사용(`--check` 미등록 0).
10. `stt_rebuilds_total` 은 갭 rebuild 만 센다(§4 요청 2) — D.json 의 `rebuild_count` 정의에 반영.

## 6. 알려진 이슈

- CI `eval-smoke` 는 현재 코드에서 빨강(§4-3), `frozen-artifacts` 는 측정 전까지 빨강(의도). `docker` 잡은 이 박스에서 실행하지 못했다(데몬 없음).
- 러너의 `postgres` 자원 행은 모든 백엔드 프로세스 RSS 합이라 공유 버퍼가 중복 계산된다(추세 비교용, `limitations.md` §3).
- `ws_resume_total{ok}` 는 클라이언트 `resumes_ok` 와 같아야 하며 축약 D 에서 일치(8=8). 200 세션 D 에서 리스 30 s 안에 SIGSTOP 15 s 가 끝나므로 XAUTOCLAIM 인계는 일어나지 않는다(WP-C 요청 8 — 두 워커 인계는 통합 테스트가 증명).
- api 종료 코드 −15 는 uvicorn 표준(시그널 재발생). 러너는 종료 코드를 기록만 하고 판정하지 않는다.
- `hypothesis_examples`(`eval.protocol.*`)는 `chartwire eval protocol --run-tests` 가 `tests/ws` 를 실제로 돌려 센다 — README 표 ① 마지막 행은 그 뒤에만 채워진다.
- README 배지 URL 은 `sokldjs554/chartwire`(git remote). 라이브 데모 링크는 HTML 주석으로 자리만.

## 7. 진행 로그
- 06:10 환경/핸드오프 13개/스펙 읽기. `readme_numbers.py` KEYS 와 `report.py` 모양 확인.
- 06:35 scenarios/report/runner/cli 작성, `client.py` 수정, ruff/mypy clean.
- 06:48 A N=10 20 s 스모크 통과: loss 0, 10/10 ended, stt_offsets 10/10, segments contiguous 10/10, alert 샘플 7 (4 세션). api exit −15 (uvicorn 이 SIGTERM 을 되던짐, WP-G §4-10 과 일치).
- 06:49 H 2 K × 2 워커 × 5 테넌트 스모크 통과: dlq 0, reclaimed 200, worker 0 SIGKILL.
- 관찰: ack p50 ≈ 177 ms — `IngestCore.on_ledgered` 가 `now_ms` 를 갱신하지 않아 100 ms 규칙이 지난 틱 시각으로 평가됨 (→ §4 WP-B 요청).
- 06:55 단위 테스트 18개, CI 5 잡, Makefile, README 골격(--check: 미등록 키 0), architecture/limitations/AGENTS.
- 07:05 축약 D 스모크 통과(resume 8/8, FLUSHDB 재수화, SIGSTOP rebuild, loss 0). results.md 정의 보강. 최종 게이트 전부 통과, 이 문서.
