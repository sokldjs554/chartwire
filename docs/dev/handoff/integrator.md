# 통합자(integrator) 핸드오프 — 전체 시스템 end-to-end 통합

> 상태: **진행 중** (2026-09-04). 중단되면 §0 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a`
> 자체 테스트 DB `chartwire_test_i` / Redis 9. 데모 서버 포트 8000, 임시 서버 8110.
> 이전 실행(커밋 `0d045ca`)이 §1 의 요청 대부분을 이미 적용했고 체크리스트는 비어 있었다 — 이 문서는 그 상태에서 다시 채운 것이다.

## 0. 체크리스트 (재개용)

- [x] 1. 크로스 WP 요청 수집·적용 (§1 표) + `loadtest/simulate_cli.py` 작성
- [x] 2. 앱 조립 확인 (§2) — 임베디드 stt-worker 결함 수정
- [x] 3. 전체 스위트 직렬 그린 (§3 표) + ruff / ruff format / mypy strict / pip check — 셈 정리 뒤 재실행 결과는 §3 표 갱신
- [x] 4. e2e 데모 흐름 (§4) + `docs/dev/e2e.md` + `scripts/e2e_check.sh` + `make demo`
- [~] 5. 콘솔 Playwright 검증 + `docs/images/*.png` — 01/02/03 저장, 04/05 재실행 중 (§5)
- [x] 6. 하우스키핑: 셈(shim) 제거, pyproject(`console` extra), `cdk-nag==2.38.2` 확인, ci.yml(직렬 그룹 + tests/chaos), AGENT_ENV.md

## 1. 크로스 WP 요청 처리 표

| 요청 | 출처 | 처리 | 이유 / 위치 |
|---|---|---|---|
| `redis/keys.py` 키 함수(`sess, chunks, events, ctl, viewers, ticket, stt_lag, node`) | B→A | 적용됨(이전) | `redis/keys.py` 에 전부 존재 |
| `segments.replay(after_seq)`, `sessions.ack_state` | B→A | 적용됨(이전) | `repo/segments.py`, `repo/sessions.py` |
| `segments.last_seq` 로 `watch._load_session` 의 Core 쿼리 교체 | B→A | 적용됨(`0d045ca`) | `repo/segments.py::last_seq`, `ws/watch.py:219` |
| `create_app` 에 ws 라우터 + `on_startup/on_shutdown` | B→E | 적용됨 | `api/app.py` lifespan |
| ws-ticket `tickets.issue(...)` | B→E | 적용됨 | `routers/sessions.py` |
| revoke → `ctl consent_revoked`, purge → `ctl purge` | B→E | 적용됨 | `consent/service.py::notify_revoked`, `purge/pipeline.py` |
| `watch._ack_alert` → `alerts.acknowledge_in_tx + after_ack` | B/C/E | 적용됨(`0d045ca`) | `ws/watch.py:285-302` |
| `sess:{sid}` 해시 `tenant` 필드(hello 뒤) | C→B | 적용됨(`0d045ca`) | `ws/ingest.py:273` |
| `state='ended'` 커밋 → 엔드 마커 순서 | C→B | 적용됨(`0d045ca`) | `ws/ingest.py::_transition` (마커 XADD 가 tx 뒤) |
| `Settings` STT 필드 5개, `scripts_dir` = STT 스크립트 디렉터리 | C→A | 적용됨 | `core/config.py`; `SttWorkerConfig.from_env` 가 Settings 기본값 사용 |
| `upsert_stt_offset` GREATEST | C→A | 적용됨 | `repo/sessions.py:173` |
| `POST /alerts/{id}/ack` → `risk.alerts.ack` (폴백 제거) | C→E | 적용됨(`0d045ca`) | `routers/alerts.py` |
| `STT_REBUILDS_TOTAL` 카운터 + `ALL_NAMES` | C→G | 적용됨(`0d045ca`) | `ops/metrics.py:140` |
| compose 에 `CHARTWIRE_SCRIPTS_DIR` 볼륨/변수, worker/stt 포트 | C/F→A | 적용됨(`0d045ca`) | `docker-compose.yml` |
| notes 라우터 include + `install_error_handlers` | D→E | 적용됨 | `api/app.py` (`_optional`), 앱 자체 problem+json 핸들러 |
| `routers/notes.py:43` 조건부 `get_deps` 제거 | E→D | 적용됨(`0d045ca`) | 직접 import |
| `consent.service.active_scopes_for_patient` 사용 | D→E | 적용됨(`0d045ca`) | `notes/service.py::_consent_scopes` |
| `INJECTION_RE` 확장 | F→D | 적용됨(`0d045ca`) | `notes/verifier.py:35` |
| `SYMPTOM_CUES` 확장 | F→D | 적용됨(`0d045ca`) | `notes/extractive.py:30` |
| `replay` 상한 `< started_at + 1 day` | F→A | 적용됨(`0d045ca`) | `repo/segments.py::_bounded` |
| `Makefile bulk --out docs/perf/bulk.json`, `perf-study` 두 상태 | F→A | 적용됨 | `Makefile` |
| `past` 해석을 두 문서에 같은 문장으로 | F→C | 적용됨(`0d045ca`) | `docs/risk-detection.md` §9 |
| `outbox` repo 백오프를 `outbox.backoff` 에 위임, `locked_at` NULL, `mark_done_many` | G→A | 적용됨(`0d045ca`) | `repo/outbox.py`; 폴러가 `mark_done_many` 사용 |
| `insert_events` 벌크 삽입을 repo 로 | G→A | **거절** | 측정 하네스(`outbox/bench.py`) 전용 Core INSERT — 런타임 경로가 아니라 repo 계약을 넓힐 이유가 없음 |
| `LAZY_SUBAPPS["outbox"]`, `serve` 마운트 | G→A | 적용됨 | `cli.py` |
| `/metrics`·`/healthz`·`/readyz` 를 ops 라우터로 | G→E | 적용됨 | `ops/routes.py` + `create_app` |
| ws 가 `app.state.drainer` 재사용 | G→B | 적용됨 | `ws/routes.py::on_startup` |
| `token` sub-app 마운트 | E→A | 적용됨 | `cli.py` |
| `Settings.cors_origins/console_dir` | E→A | 적용됨 | `core/config.py`, `api/app.py` 가 Settings 사용 |
| `repo/ops.list_segment_partitions` | E→A | 적용됨(`0d045ca`) | `db/repo/ops.py` |
| `types-redis` 제거 + `type: ignore` 제거 | E→통합자 | 적용됨(`0d045ca`) | `pyproject.toml`, `Redis` 비제네릭 |
| `SessionOut.script_ref` | H→E | 적용됨 | `api/schemas.py:138` |
| `/v1/me` 평면 `{sub, tenant_id, role}` | H→E | 적용됨 | `MeOut` (+ `user_id`, `exp` 상위 집합) |
| revoke 202 에 `purge_job_id` | H→E | 적용됨 | `ConsentRevokedOut` |
| `/console` 서빙 | H→E | 적용됨 | `api/app.py::console_path` |
| 데모 계정 `<role>@demo.clinic` / `demo1234!` / slug `demo` | H→F | 적용됨 | `synth/seed.py::DEMO_USERS` |
| `simulate` 와 `loadtest` 는 별개 모듈 | A→H | **이번 실행에서 작성** | `loadtest/simulate_cli.py` (아래 §4) |
| `var/eval/purge.json`·`rls.json` 기록 | F→E | 미적용(측정 단계) | Phase 2 측정 창의 일 — `chartwire eval` 집계형이 없으면 "건너뜀" 으로 동작 |
| `docs/eval/alert_latency.json`, `docs/loadtest/*.json` | F→H | 미적용(측정 단계) | WP-H Phase 1 (`loadtest/scenarios.py`) 미작성 — 이 실행 범위 밖 |
| `cdk-nag==2.38.2` 고정 | H→통합자 | 적용됨(`0d045ca`) | `pyproject.toml` infra extra |
| CI integration 잡에 `tests/chaos` | G→H | **이번 실행** | `.github/workflows/ci.yml` |

## 2. 앱 조립

`create_app()` 은 이미 ws 라우트(`on_startup/on_shutdown`), notes 라우터, ops 라우터, `/console`, problem+json 핸들러 5종, 미들웨어
(RequestId → SecurityHeaders → CORS → BodyLimit → RateLimit → Idempotency)를 조립하고 있었다. 이번 실행에서 고친 결함:

| 결함 | 어떻게 드러났나 | 수정 |
|---|---|---|
| `serve all --embedded` 의 stt-worker 가 **스레드 + 자체 이벤트 루프**(`asyncio.to_thread(stt_worker.main)`)로 돌아 자체 풀·자체 드레이너·자체 ops 포트(9002)를 가짐 → SIGTERM 에 api/worker 만 드레인되고 프로세스가 살아남음(좀비), 재기동 시 9002 충돌로 exit 3 | 데모 서버 재시작 | `worker/main.py::_run_stt` 가 `stt_worker.run(settings, cfg=replace(cfg, http_port=0), ctx=ctx, drainer=drainer, install_signals=False)` 를 같은 루프의 태스크로; `run_all` 의 `FIRST_COMPLETED` 집합에 stt 태스크 포함, 종료 코드는 worker·stt 둘 다 0 일 때만 0 |
| `POST /sessions/{id}/end` 가 500 | `test_api_sessions::test_end_sets_final_seq_and_end_marker` | `routers/sessions.py::_announce_end` 가 `SessionState(scripts_dir=settings.scripts_dir)` 로 Lua 를 STT 스크립트 디렉터리(`var/scripts`)에서 찾음 — WP-C 요청 3 반영 때 `ws/routes.py` 만 고치고 이 호출부를 놓침. 인자 제거(Lua 는 패키지 리소스) |
| 로그인 500 `permission denied for sequence audit_events_id_seq` | 데모 서버 첫 로그인 | 코드 결함 아님: 개발 DB 가 0005 에 GRANT 가 추가되기 전에 만들어짐. `db downgrade base && db upgrade && seed --demo` 로 재생성(e2e.md·AGENT_ENV.md 에 기록) |

기동 확인: `chartwire serve all --embedded --host 127.0.0.1 --port 8000` → `api started` · `worker started` · `stt-worker started` ·
`partitions ensured`; `/healthz` 200, `/readyz` `{"postgres":"ok","redis":"ok"}`, `/console` 200, `/metrics` 28 계열; SIGTERM →
`drain started` → `ws drain started` → `worker stopped` → 프로세스 종료.

## 3. 스위트 결과

첫 전체 실행(셈 정리 **전**, `scratchpad/run_suites.sh` — 그룹별 DB/Redis 인덱스는 AGENT_ENV.md "Test run" 과 동일):

| 그룹 | DB / Redis | 결과 |
|---|---|---|
| tests/unit | – | 706/708 → 낡은 기대치 2건 수정 후 **718 passed** (신규 `test_simulate_cli.py` 10 포함) |
| tests/ws | – | **149 passed** |
| rls + test_migrations (A) | a / 1 | **38 passed** |
| ws e2e state/ingest/watch/drain (B) | b / 2 | **35 passed** |
| alerts + stt_worker + stt chaos (C) | c / 3 | **14 passed** |
| notes service + rest (D) | d / 4 | **28 passed** |
| rbac/api/phi/purge (E) | e / 5 | 1 failed → `sessions.py` Lua 경로 결함 수정 → 파일 재실행 **7 passed** (그룹 29) |
| seed + bulk (F) | f / 6 | **8 passed** |
| outbox poller/tickers/cli + ops routes (G) | g / 7 | **21 passed** |
| tests/chaos (G) | g / 7 | **1 passed** |
| infra/cdk/tests (H) | – | **13 passed** |

게이트: `ruff check src tests scripts` clean · `ruff format --check` 306 files formatted · `mypy` strict 모듈(core/codec/verifier/crypto/outbox) clean · `python -m pip check` clean.
셈 정리(§6) **뒤** 재실행(진행 중, 이 문서 작성 시점): unit 718 passed · ws_e2e 35 passed · stt_alerts 14 passed; api_purge / seed_bulk / outbox_ops / chaos / notes 는 실행 중 —
`scratchpad` 가 사라졌으면 AGENT_ENV.md 의 명령으로 다시 돌린다(수정 모듈: `purge/pipeline.py`, `synth/seed.py`, `worker/handlers/session_reaper.py`, `ws/routes.py`, `ws/ledger.py`, `ws/ingest.py`, `stt/consumer.py` 는 ruff/mypy clean).

## 4. e2e 데모

명령 순서 전체는 `docs/dev/e2e.md`; `scripts/e2e_check.sh <session>` 이 REST 확인을 그대로 실행한다. 이 박스에서의 실행(개발 DB
`chartwire`, Redis 0, 포트 8000):

- `chartwire simulate --script s01 --speed 4` (신규 `loadtest/simulate_cli.py`: `loadtest/client.py` 의 RecorderClient + ViewerClient, REST 로그인, script_ref 로 created 세션 선택, `--drop-at`): `outcome=ended, sent=757, ack_seq=757, loss=0, nacks=0, credit_min=49, finals=50, final_dups=0, alerts=1, note_status=verified` (ack p50/p95 ≈ 81/175 ms — README 용 아님)
- REST: 세그먼트 50(seq 0..49) 복호화 · 열린 경보 1(harm_to_others sev 2, `sla_deadline_at` = detected + 300 s) → ack · 노트 `verified` extractive coverage 1.0 14 문장 0 unsupported · 평가 없이 서명 409 `CW-4092` → reject/edit/assessment → 서명 `signed`, `legal_hold=medical_record`, `retention_until=+10년` · 동의 철회 202 → 환자 단위 purge job 이 2 s 안에 `verified`, `receipt_hash_valid=true`, steps capture/redis/objectstore/rows/crypto_shred/patient_shred · `verify-decrypt` = `{unwrap: failed:dek_destroyed, decrypt_sample: failed:invalid_tag}` · 세그먼트 410 `CW-4100` · 서명 노트는 인용·평가 그대로 읽힘 · 환자 `name=null, consent_state=purged`
- 이 과정에서 고친 것: `loadtest/client.py::SessionStats.last_final_seq` 기본값 0 → −1 (세그먼트 seq 는 0부터라 첫 final 이 중복으로 세어짐; 재접속 `from_seq` 는 콘솔과 같은 `last+1`), `synth/seed.py` 데모 환자 이름 = 가명(`가상환자-NNNN`, 스펙 §0.1; 콘솔의 정확 일치 조회가 동작)
- `make demo` = `db bootstrap-roles && db upgrade && seed --demo --if-empty` → `serve all --embedded --port 8000` (Makefile 에 이미 있었음, e2e.md 를 가리킴)

## 5. 콘솔

`scripts/console_screenshots.py`(Playwright, `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`, Noto Sans CJK KR) 가 실제 api 에 대해
로그인 → 세션 생성(정확 일치 환자 조회 + s01) → 뷰어 연결 → 녹음 ×4 → 라이브 전사(partial 회색 → final, e2e ms) → 위험 배너(harm_to_others sev 2, SLA 카운트다운)
→ ACK → 종료 → SOAP 초안(verified, coverage 1.0, 근거 하이라이트, note.status 토스트) 까지 브라우저 콘솔 오류 0 으로 통과했고
`docs/images/01_recorder.png`, `02_live_alert.png`, `03_soap_draft.png` 을 저장했다. 04(파기 영수증)/05(Ops) 는 드라이버 결함 2건
(뷰어는 `session.state{drafted}` 가 아니라 `note.status` 를 받음; 콘솔 CSP 가 `unsafe-eval` 을 막아 `wait_for_function` 문자열 조건 불가 → 드라이버 쪽 폴링)을 고친 뒤
`--patient 가상환자-0003` 으로 재실행 중이었다. 콘솔/API 불일치는 발견하지 못했다(콘솔 코드 변경 없음).

## 6. 하우스키핑

- `ws/_nometrics.py` 는 이미 삭제됨(pyc 만 남아 있었음). 실제 모듈이 있는데 남아 있던 "아직 안 만들어짐" 셈 제거: `session_reaper`(SessionState 폴백 XADD), `ws/ledger`(metrics None), `ws/ingest`·`stt/consumer`(`active_scopes_for_patient` 로컬 폴백 → `consent.service` 직접), `stt/consumer`(`getattr(metrics, "STT_REBUILDS_TOTAL")`), `ws/routes`(Drainer import 가드), `purge/pipeline`(`_observe` 지연 import), `synth/seed`(`policy_hash` 폴백). 남긴 것: `api/app.py::_optional`(스펙 계약), `worker/main.py::load_handlers`(스펙 계약), `cli.py LAZY_SUBAPPS`, 선택 SDK(anthropic/boto3/amazon-transcribe).
- pyproject: 임포트 ↔ 의존성 대조 일치; `cdk-nag==2.38.2` 고정 확인; `console = ["playwright>=1.45"]` extra 추가(스크린샷 스크립트).
- `.github/workflows/ci.yml` integration 잡: 그룹별 `CHARTWIRE_TEST_DB`/`_REDIS_DB` 로 직렬 실행 + `tests/chaos` 포함, junit `junit-*.xml`.
- `docs/dev/AGENT_ENV.md` "Test run" 절 추가.

## 7. 알려진 이슈

- `mypy src/chartwire` 전체는 37건(비-strict 모듈, 예: `ws/ingest.py:443` 람다 추론) — 요구 범위(strict 5 모듈)는 clean. HEAD 에도 있던 것.
- 측정 단계 산출물(`docs/loadtest/*.json`, `docs/eval/*.json`, `docs/perf/summary.json`, `var/eval/{purge,rls}.json`)과 WP-H Phase 1(`loadtest/scenarios.py`·`cli.py`, `docs/AGENTS.md`, `docs/limitations.md`, README 마커)은 이 실행 범위 밖.
- 개발 DB 는 마이그레이션 파일 수정 이전 상태일 수 있다(e2e.md §1).
- 데모 서버(`serve all --embedded`, PID `scratchpad/logs/serve_all.pid`)는 스크린샷 재실행이 끝나면 SIGTERM 으로 내린다 — 임베디드 stt-worker 수정 뒤 SIGTERM 종료 경로는 `api_exit`/시그널 모두 같은 드레이너를 타지만 exit 0 까지는 이 문서 작성 시점에 재확인하지 못했다.
