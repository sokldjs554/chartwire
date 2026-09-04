# AI 도구·에이전트로 개발한 방식

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 스펙 §14 가 요구하는 네 가지를 담는다: 작업 패키지 표,
> 바뀌면 여기 적어야 하는 계약, 병합 전 통과해야 하는 게이트, **처음부터 알려 준 함정**(발견이 아니라 지시), 그리고 세션 동안 테스트가
> **실제로 잡은 결함의 버그 저널**(각 작업 패키지 핸드오프 `docs/dev/handoff/*.md` 에서 그대로 인용; 지어낸 항목 없음).

## 1. 진행 방식

한 스펙([`design/SPEC.md`](design/SPEC.md), 모든 결정이 내려진 상태)을 8개 작업 패키지(WP-A…H)로 나누고, 코딩 에이전트 8개가 **동결된
계약**(§15)에 대해 병렬로 작업했다. 단계는 셋이다: Phase 0 — WP-A 가 골격·마이그레이션·픽스처를 먼저 내고 나머지는 sans-I/O 부분을
계약만 보고 짓는다 → Phase 1 — 통합(티켓→hello→청크→원장→ack → STT → 초안 → 동의/파기 → 시드/평가 → 콘솔/부하) + CI 녹색 →
Phase 2 — 유휴 박스에서 **직렬** 측정(bulk → perf → eval → load A/B/C/D/H → `readme-numbers --write`). 에이전트 간 규칙:

- 자기 소유 경로만 고친다. 남의 파일이 필요하면 핸드오프에 **정확한 diff** 를 적고 계약대로 코드를 쓴다 — 통합자가 적용/거절을 표로 남긴다(`integrator.md` §1, 40여 건).
- 테스트는 자기 DB(`chartwire_test_<wp>`)와 Redis 인덱스(A=1 … H=8)에서만 돈다. 남의 DB 를 지우거나 `FLUSHALL` 하지 않는다. 측정은 아무도 없는 창에서 한 번에 하나.
- 핸드오프 문서를 **먼저** 체크리스트로 만들고 20분마다 갱신한다 — 중단된 실행을 다른 에이전트가 이어받는다(실제로 여러 WP 가 2–3차 실행에서 마무리됐다).
- 문서의 숫자는 코드가 만든 JSON 에서만 온다(§11.4). 손으로 적은 측정치는 없다. `eval/data/` 는 WP-F 외에 열지 않는다(누출 통제).
- git 상태를 바꾸는 명령은 통합자만 실행한다.

## 2. 작업 패키지 표 (스펙 §15)

| WP | 소유 경로 | 산출물 | 의존 | 게이트 | 핸드오프 |
|---|---|---|---|---|---|
| **A Foundation** | `core/`, `db/`, `migrations/`, `redis/{client,keys}.py`, `objectstore/`, `cli.py`, `tests/conftest.py`, `Makefile`, compose, Dockerfile, CI 골격, `scripts/dev_up.sh` | DDL 7 리비전, `bootstrap-roles`, `tenant_tx`, 모델·repo, `ensure_segment_partition`, 스키마 덤프, 픽스처, `audit.service.record`, `outbox.writer.emit` | — | 마이그레이션 왕복; `test_rls_leak`, `test_partition_direct`, `test_superuser_leaks`, `guc_leak` | [`wp-a.md`](dev/handoff/wp-a.md) |
| **B Ingest/Watch** | `ws/`, `redis/session_state.py`, `redis/scripts/*.lua`, `redis/tickets.py`, ws-ticket, `docs/protocol.md` | codec, messages, `IngestCore`/`WatchCore`, ingest/watch 셸, ledger, credit, drain, hypothesis 스위트, uvicorn e2e | A | hypothesis ≥ 2,000 예제; 3 세션 강제 kill → resume 손실/중복 0; superseded | [`wp-b.md`](dev/handoff/wp-b.md), [`wp-b-phase1.md`](dev/handoff/wp-b-phase1.md) |
| **C STT & risk** | `stt/`, `risk/`, `worker/handlers/alert_sla.py`, `docs/risk-detection.md` | 어댑터·시뮬레이터·`SlowStt`·Transcribe 매핑, stt-worker(consumer group, XAUTOCLAIM, 갭 rebuild, inline risk tx), 사전 + 범위 FSM, SLA 티커 | A; B 의 스트림 계약 | 범위 종류 단위 테스트(in-grammar 만); FLUSHALL + SIGSTOP 복구; **`eval/data/` 열지 않음** | [`wp-c.md`](dev/handoff/wp-c.md), [`wp-c-phase1.md`](dev/handoff/wp-c-phase1.md) |
| **D Notes/AI** | `notes/`, `worker/handlers/note_draft.py`, `api/routers/notes.py`, `tests/fixtures/anthropic/`, `scripts/eval_anthropic.py`, `docs/grounding.md` | 스키마·정규화·검증기·정책, 추출형/변이/패러프레이즈/녹음/anthropic 프로바이더, 서비스(초안/서명), 노트 REST | A; `SegmentView` | 검증기 8 규칙 단위 테스트; 픽스처 경로; 서명은 평가 필수 | [`wp-d.md`](dev/handoff/wp-d.md), [`wp-d-phase1.md`](dev/handoff/wp-d-phase1.md) |
| **E Security/REST** | `auth/`, `crypto/`, `consent/`, `purge/`, `audit/`, `api/`(notes/ws-ticket 제외), `worker/handlers/purge_*.py`, `docs/consent-purge.md`, `docs/security/threat-model.md` | JWT/RBAC 매트릭스, 봉투 + KEK + 블라인드 인덱스 + KeyCache, 동의 게이트, 파기 파이프라인·검증·영수증, 감사, REST, 레이트리밋, 멱등성, PHI 로그 테스트 | A | RBAC 매트릭스; 파기 잔존 0; `verify-decrypt` 데모; 로그 grep | [`wp-e.md`](dev/handoff/wp-e.md), [`wp-e-phase1.md`](dev/handoff/wp-e-phase1.md) |
| **F Synthetic/Eval/Perf** | `synth/`, `eval/`, `perf/`, `scripts/readme_numbers.py`, `docs/eval/`, `docs/perf/` | **첫 산출물: held-out 세트 + FROZEN.txt**(`risk/` 접근 없이), 어휘/문법/스크립트/골드, 시드, bulk COPY 로더, 평가 리포트, 성능 연구, README 숫자 스크립트 | A; 평가는 C/D | 시드 결정론; bulk < 6 분; 모든 리포트에 헤더 | [`wp-f.md`](dev/handoff/wp-f.md), [`wp-f-phase1.md`](dev/handoff/wp-f-phase1.md) |
| **G Worker/Outbox** | `outbox/`, `worker/main.py`, `partition_ensure`/`outbox_prune`/`session_reaper`, `ops/`, `docs/ops/runbook.md` | 테넌트별 폴러, 리스, 재시도/DLQ/replay, 티커, 메트릭, health/readyz, drain, SIGKILL 카오스, 시나리오 H 벤치 | A | 독약 메시지; SIGKILL; H 수치 | [`wp-g.md`](dev/handoff/wp-g.md), [`wp-g-phase1.md`](dev/handoff/wp-g-phase1.md) |
| **H Loadtest/Console/Infra** | `loadtest/`, `console/index.html`, `infra/cdk/`, `render.yaml`, CI 마무리, `docs/loadtest/`, `docs/aws.md`, `docs/AGENTS.md`, `docs/limitations.md`, README 조립 | 프로토콜 클라이언트(콘솔 JS 와 같은 §6 의미론), 시나리오 A–D 러너·리포트, 콘솔 3 탭 + 패널, CDK 스택·테스트·템플릿·nag, README 마커 | B; e2e 는 C/D/E | CI load-smoke; cdk diff 0; 콘솔 스모크(`node --check`) | [`wp-h.md`](dev/handoff/wp-h.md), [`wp-h-phase2.md`](dev/handoff/wp-h-phase2.md) |
| **통합자** | 크로스 WP 요청 적용, 앱 조립, 스위트 직렬 녹색, e2e 데모, 스크린샷, 하우스키핑 | [`integrator.md`](dev/handoff/integrator.md), [`e2e.md`](dev/e2e.md) | 전부 | 전체 스위트 + ruff/mypy/pip check | |

## 3. 계약 (바꾸면 이 문서에 적는다)

- **DB** — §4 DDL; `db.tenant.tenant_tx(engine, TenantCtx)`; repo 시그니처(§4.5). 확장된 것: `segments.replay(..., started_at=None)` 은 상한 `< started_at + 1 day` 도 건다(WP-F 요청 4); `sessions.upsert_stt_offset` 는 `GREATEST` 로 단조(WP-C 요청 4); `segments.last_seq`, `ops.list_segment_partitions`, `outbox.mark_done_many` 추가.
- **Redis** — 키 이름은 `redis/keys.py` 만; 스트림 필드 `seq,key,len,off,fl,ep,ts`; 엔드 마커 `{"end":"1","ep":n}`; `hello.lua` 가 그룹을 `0` 으로 만든다(스펙 `$` 대신 — 워커가 그룹 생성 전 항목을 놓치지 않도록, WP-B). `sess:{sid}` 해시에 `tenant` 필드(WP-C 요청 1).
- **WS** — §6.2–6.5 메시지와 `IngestCore`/`WatchCore`. 확장: `on_hello(..., ledgered_after_ack=, hot_state_present=)`, `WatchCore.on_hello(from_seq, *, state, last_final_seq)`, `on_consent_revoked`, `on_drain`, `on_replay_done`; `welcome.ack_seq` 가 store 값보다 클 수 있음; `welcome.missing`/`nack.missing` 은 닫힌 구간 `[[from,to],…]`; `risk_event_id` 는 정수. ack 는 원장 배치당 최대 하나.
- **STT** — `Chunk/Partial/Final/SttStream/SttAdapter`; final 트랜잭션 내용(§7.4); 발행 페이로드 `transcript.final{seq, speaker, t_start_ms, t_end_ms, text, confidence, segment_id, committed_at}`, `risk.alert{risk_event_id, category, severity, segment_seq, span, sla_deadline_at, committed_at}` — **커밋 뒤** 발행. 세그먼트 AAD = `ws.watch.segment_aad(tenant, sid, seq)`.
- **Risk** — `detector.scan(text, speaker) -> list[RiskHit]`(0 또는 1개), `DETECTOR_VERSION="lex-1"`. `alerts.on_final_segment` 는 `create_event`(tx 안) + `after_commit`(커밋 뒤) 두 조각.
- **Notes** — `NoteDraftOut`, `DraftContext`, `RawDraft`, `NoteProvider.draft`, `verifier.verify`, `policy.decide -> Decision{status, reason}`(스펙 `NoteStatus` 의 상위 집합), `service.draft_for_session(ctx, session_id, *, tenant_id=None, provider=None) -> NoteOutcome`.
- **Outbox** — `writer.emit(...)`, `@registry.handler(event_type, lease_s, max_attempts, name=)`, `HandlerContext(+tenant_tx())`; 페이로드 `session.transcribed{session_id, patient_id}`, `consent.revoked{patient_id, consent_id, purge_job_id}`(환자 단위 작업 하나 — WP-E §4-1), `purge.requested{purge_job_id}`, `purge.completed{purge_job_id}`. `mark_done` 은 50개 배치.
- **Crypto** — `KekProvider`, `Envelope`, `aad(...)`, `blind_index(...)`, `KeyCache`, `DekDestroyedError`. `AwsKmsKek` 는 KMS `Encrypt/Decrypt`(스펙 문구 `GenerateDataKey` 대신 — 로컬 DEK 를 래핑하므로).
- **Consent** — `gates.require_scope(scopes, scope)`(순수) + `consent.service.require_scope_for_patient(session, patient_id, scope)`; WS 4011 / REST `CW-4031`.
- **Audit** — `service.record(...)`; `detail` 에 `{text, quote, name, phone}` 금지(코드가 거부). 추가 액션: `user.created`, `patient.created`, `note.assessment`, `note.draft_requested`, `session.drafted/signed`, `outbox.replayed`, `purge.verify_decrypt`.
- **REST 응답 상위 집합** — `SessionOut.script_ref`, `MeOut{sub, tenant_id, role, user_id, exp}`, `ConsentRevokedOut.purge_job_id`, `NoteOut{…, legal_hold, retention_until, signed_at}`.
- **리포트 키** — `scripts/readme_numbers.py::KEYS` (234개). 부하 리포트 모양은 [`loadtest/results.md`](loadtest/results.md) 와 `loadtest/report.py` 도크스트링; 성능 요약은 `docs/perf/summary.json`(Phase 0 의 `study.json` 에서 이름 변경).
- **CLI** — `simulate` 와 `loadtest` 는 별개 모듈(WP-A 요청); `serve all` 은 항상 embedded; `chartwire loadtest H` 는 `outbox bench` 위임.

## 4. 병합 전 게이트

| 영역 | 게이트 | 명령 |
|---|---|---|
| ws | 단위 + hypothesis(2,100 예제, 32 스텝) + 큐 격리 | `pytest tests/ws -q` |
| db | RLS 스위트(앱 역할) + superuser 누출 대조 + 마이그레이션 왕복 + 스키마 덤프 동일 | `pytest tests/rls tests/integration/test_migrations.py -p no:xdist` |
| REST | 실제 `create_app` 의 모든 라우트 == `rbac.MATRIX`; 37 라우트 × 5 역할 + 익명 | `pytest tests/integration/test_rbac_matrix.py` |
| notes | 검증기 8 규칙 양성/음성, 녹음 픽스처 7개, 변이 7 × 200, 패러프레이즈 300 세션 | `pytest tests/unit/test_notes_*.py` |
| infra | `Template.from_stack` 단정 13개, cdk-nag ERROR 0, 결정론 | `pytest infra/cdk/tests -q; cd infra/cdk && python app.py` |
| stt/risk | 범위 종류 7종, FLUSHDB → 그룹 재생성 → 원장 재생, 서브프로세스 SIGSTOP/SIGCONT 인계 | `pytest tests/integration/test_stt_worker*.py tests/integration/test_alerts.py -p no:xdist` |
| outbox | 독약 8회 → DLQ 1행 → replay, 두 폴러 중복 0, SIGKILL → reclaim → 정확히 1회 | `pytest tests/integration/test_outbox_*.py tests/chaos -p no:xdist` |
| purge | e2e 6 단계 + 검증 + 영수증 해시 + `verify-decrypt` 실패, 검증 실패 → DLQ → replay | `pytest tests/integration/test_purge_pipeline.py -p no:xdist` |
| 로그 | 전사·이름·전화·JWT·티켓 0건(응답에는 실제로 있었음을 함께 단정) | `pytest tests/integration/test_phi_logs.py` |
| 전체 | ruff, ruff format, mypy strict 5 모듈, pip check, 그룹별 직렬 스위트 | [`dev/AGENT_ENV.md`](dev/AGENT_ENV.md) "Test run" |

## 5. 처음부터 알려 준 함정 (발견이 아님)

스펙과 환경 문서가 에이전트에게 **시작 전에** 준 것들이다. 아래는 그래서 코드에 처음부터 반영돼 있고, 버그 저널에는 없다.

1. **PostgreSQL 파티션 부모의 identity·unique** — 파티션 테이블의 unique/PK 는 파티션 키를 포함해야 하고, `CREATE INDEX CONCURRENTLY` 는 부모에 못 쓴다(파티션별 인덱스 + `ATTACH`). identity 시퀀스는 파티션 간 공유. (`db/schema.md` 함정 표)
2. **RLS 와 비-leakproof 연산자** — 이 PG 에서 `texticlike/textlike/textregexeq/similarity/word_similarity/arraycontains/ts_match_vq` 는 leakproof 가 아니다 → RLS 아래에서 트라이그램/GIN 인덱스가 무시된다. 해결은 `SECURITY DEFINER` 함수(§4.6, ADR-0005). 정책은 `NULLIF(current_setting(...), '')::uuid` 형태.
3. **2음절 한국어와 `pg_trgm`** — 3-gram 이라 2음절 질의는 이득이 작다 → `terms[]` 인덱스.
4. **ack = durable** — Redis 에 쓴 것을 ack 로 치면 안 된다(§0.3, ADR-0002). Redis 는 `FLUSHALL` 뒤 원장에서 재구축된다.
5. **LLM 을 안전 경로에 두지 않는다**, 스키마에 Assessment/진단/판정 필드 없음(§0.4–0.5, ADR-0003).
6. **로그에 전사·이름·토큰 금지**(§0.9) — `redact_phi` 프로세서 + 접근 로그에 쿼리스트링 없음 + grep 테스트.
7. **Python 3.11 에는 `uuid7` 이 없다** → 직접 구현(RFC 9562, 프로세스 내 단조).
8. **`Dockerfile CMD` exec 형식은 `${CHARTWIRE_ROLE}` 을 확장하지 않는다** → `sh -c "exec chartwire serve ..."`.
9. **테스트 격리** — WP 별 DB/Redis 인덱스, 통합 스위트는 `-p no:xdist` 직렬, `FLUSHDB` 는 자기 인덱스만.
10. **측정 규율** — 숫자는 JSON 에서만, 미측정 행은 삭제, 측정은 유휴 박스에서 직렬(§0.2, §11.4).
11. **`eval/data/` 누출 통제** — held-out 세트는 `risk/` 를 열기 전에 쓰고 동결(FROZEN.txt); WP-C 는 그 디렉터리를 열지 않는다.
12. **`CHARTWIRE_STT_SCRIPTS_DIR`** — 시드와 stt-worker 가 같은 디렉터리를 봐야 한다(compose 볼륨).
13. **부하 페이로드는 비압축이어야 한다** — permessage-deflate 가 켜져 있어 반복 바이트는 수십 바이트로 줄고 백프레셔가 측정되지 않는다(WP-B → WP-H 인계).

## 6. 버그 저널 — 테스트가 실제로 잡은 결함

각 항목은 해당 핸드오프의 문장을 인용한다(요약·의역은 괄호). 전부 이 세션의 테스트·실행이 드러낸 것이다.

### WP-A ([`wp-a.md`](dev/handoff/wp-a.md) §4)
- `test_audit_immutable::*` 3건 — "`INSERT … RETURNING` 은 SELECT 정책을 탄다 → 임상의 컨텍스트에서 감사 기록 자체가 실패. 또 0006 이 audit 에 INSERT/SELECT 정책만 두어 owner 의 UPDATE 가 0행(트리거 미도달)". 수정: `tenant_isolation`(ALL) + RESTRICTIVE `audit_read_gate`, `inline()` INSERT + `currval`.
- `test_outbox_*` 2건 — "`next_attempt_at DEFAULT now()`(DB 시각)인데 `FakeClock` 이 2026-09-01 고정 → 'due' 가 아님". 테스트가 트랜잭션의 `SELECT now()` 로 클록을 앵커링.
- `test_purge_*` — "`to_jsonb($1)` 에 dict 파라미터 → asyncpg 다형 타입 오류". `bindparam(type_=JSONB)`.
- `test_replay_prunes_to_started_at_partition` — "`created_at >= started_at` 은 **과거** 파티션만 잘라낸다(미래 파티션·DEFAULT 는 남음)". (뒤에 WP-F 가 상한을 추가해 1 파티션으로 만든다.)
- `test_redacts_phi_keys_*` — "`02-123-4567`(서울 국번 2자리)이 `\d{3}-…` 에 안 걸림" → 정규식을 `\d{2,4}-\d{3,4}-\d{4}` 로.
- `test_uuid7_layout_and_timestamp` — "다른 테스트가 먼저 `uuid7()` 을 호출하면 단조 클램프가 고정 `now_ms` 를 앞으로 민다".

### WP-B ([`wp-b.md`](dev/handoff/wp-b.md) §6, [`wp-b-phase1.md`](dev/handoff/wp-b-phase1.md) §3)
- **재개 커서 교착** — "`hello` 뒤 `contig_seq` 가 store 의 `ack_seq` 에서 시작해, 이미 durable 이라 재전송되지 않는 행에서 기대 커서가 영원히 멈췄다 … 워크드 예시 테스트가 잡았고 `_advance()`(durable 행 건너뛰기)로 고쳤다."
- "**4000 뒤 코어가 열린 채로 남음** — `IngestCore.on_tick` 이 heartbeat `Close` 를 내고도 `closed` 를 세우지 않았다."
- "**stateful 모델의 credit 클램프 오류** — 80 스텝 소크에서 준수 녹음기가 4009 를 받았다. 원인은 모델: … 서버가 실제로 검사하는 것은 도착 시 `seq − ack_seq ≤ credit + 20` 이다."
- "`test_ws_state` 배처 테스트가 영원히 멈춤 — `LedgerBatcher.submit()` 이 행 상한(500)에서만 배처를 깨웠다. 배처가 한 번 유휴 대기에 들어가면 그 뒤의 행은 500개가 쌓일 때까지 커밋되지 않았다(실서비스라면 첫 연결 뒤 ack 가 끊긴다)."
- "이론상 데이터 손실: 실패한 배치 뒤 새 hello 가 억제를 풀면 아직 대기 중이던 옛 연결의 힌트가 구멍 위로 `sessions.ack_seq` 를 올릴 수 있었다(`welcome.ack_seq` 부풀림 → 녹음기가 링 버퍼를 버림)". 수정: "`_raise_ack_stmt` 가 같은 트랜잭션에서 `(ack_seq, hint]` 행 수를 세어 일치할 때만 갱신".
- "정지된 뷰어 e2e 가 '격리 실패' 처럼 보임 — 테스트 결함: `'가' * 65536` 은 permessage-deflate 로 수십 바이트가 되어 커널 버퍼가 39 MB 를 삼켰다".
- "뷰어 큐 초과 시 `ws.close()` 가 막힌 소켓 뒤에서 무기한 대기 가능 — close 프레임도 같은 전송 버퍼를 탄다" → sender 취소 후 `wait_for(close, 5 s)`.

### WP-C ([`wp-c-phase1.md`](dev/handoff/wp-c-phase1.md) §3)
- "동의 `transcription` 이 없어도 엔드 마커의 `flush()` 가 시뮬레이터의 미전송 발화 5개를 전부 세그먼트로 만들었다" (`test_consent_gates[no-transcription]`).
- "`FLUSHDB` 가 블로킹 `XREADGROUP` 중에 오면 `NOGROUP` 이 아니라 `UNBLOCKED the stream key no longer exists`".
- "세션을 끝낸 워커가 리스를 DEL 하자, SIGSTOP 에서 깨어난 옛 소유자가 '키 없음 = 재취득' 규칙으로 리스를 다시 잡고 `stt_offsets` 를 (9, 0) 으로 되돌렸다" → 사라진 리스는 `stt:active` 에 남아 있는 세션에서만 재취득.
- "깨진 `alerts:sla` 멤버가 매 틱 경고만 찍고 남았다" → `parse_members` 가 `(parsed, malformed)` 를 돌려주고 ZREM.

### WP-D ([`wp-d-phase1.md`](dev/handoff/wp-d-phase1.md) §3)
- "녹음 픽스처가 서비스 경로에서 전부 `unsupported` — 테스트 시드가 세그먼트 seq 를 0부터 다시 매겨 픽스처의 evidence seq(1부터)와 어긋남" (서비스 결함 아님).
- "추출형 기대 6문장 vs 실제 5 — `에스시탈로프람 10mg 먹고 있어요` 에는 §9.2 cue(`약|복용…`)가 없다" (뒤에 WP-F 평가가 같은 원인을 fact recall 0.23 으로 계량하고 `SYMPTOM_CUES` 확장을 요청).
- Phase 0 하네스: "`tests/unit/test_loadtest_client.py` 의 이전 실행본은 한 번도 통과한 적이 없었다(가상 시계가 이벤트 루프를 1턴만 양보해 ack 처리가 시계 전진 뒤로 밀림…)" ([`wp-h.md`](dev/handoff/wp-h.md) §4).

### WP-E ([`wp-e-phase1.md`](dev/handoff/wp-e-phase1.md) §3)
- "`pipeline.finalize` 가 `self.job.steps` 를 읽음 — `append_step` 의 UPDATE 가 매핑 인스턴스의 컬럼을 만료시켜 async 밖 lazy load(`MissingGreenlet`)" — purge e2e 5건 전부.
- "파기된 세션의 `GET /segments` 가 행이 없으면 `200 []` (DEK 검사 전에 조기 반환)" → DEK 언래핑을 앞으로, 항상 `410 CW-4100`.
- "라우터가 매칭되지 않는 경로의 404 가 Starlette 기본 `{"detail":"Not Found"}`" → 기반 `HTTPException` 에 핸들러 등록.
- "record key 를 못 여는 테넌트에서 `GET /users` 가 트레이스백 500" → `CryptoError` → `500 CW-5001`.

### WP-F ([`wp-f-phase1.md`](dev/handoff/wp-f-phase1.md) §4)
- "**PostgreSQL 은 RLS 가 적용되는 테이블에 `COPY FROM` 을 거부한다** (`FeatureNotSupportedError: COPY FROM not supported with row-level security`). 스펙 §4.6 의 'owner 로 COPY, FORCE RLS 가 owner 에도 적용' 은 그대로는 불가능" → temp 스테이지 테이블 + `INSERT … SELECT`(ADR-0005).
- "문법 렌더링으로 만들 수 있는 서로 다른 문장이 ≈150개뿐이라 '400개 풀' 루프가 끝나지 않음" (첫 bulk 테스트 hang).
- "`created_at >= started_at` 하한만으로는 **과거** 파티션만 잘린다 … 스펙 Q1a 의 'Partitions removed: 23' 은 세션이 현재 달일 때만 참" → `Q1a_bounded` 변형 + repo 상한.
- 평가가 드러낸 것(기준을 바꾸지 않음): "프롬프트 주입 4건 누출, 추출형 fact_recall 0.23, held-out recall 0.47" → 소유 WP 에 diff 로 전달; 주입 누출은 `INJECTION_RE` 확장 뒤 0.

### WP-G ([`wp-g.md`](dev/handoff/wp-g.md) §3, [`wp-g-phase1.md`](dev/handoff/wp-g-phase1.md) §3)
- "`test_poison_message_dies_on_eighth_failure_only` — `OutboxEvent`가 `slots=True` 데이터클래스라 `__dict__`가 없어 테스트가 죽었습니다(구현 로직은 정확했음)".
- "라벨 메트릭이 첫 관측 전에는 자식 시계열이 없다는 실제 운영 문제" → `KNOWN_LABEL_VALUES` 선생성.
- "드레인 후 이전 시그널 핸들러를 **모든** 시그널에 대해 재호출 → 독립 워커(`asyncio.run`)에서는 … 깨끗한 드레인이 `KeyboardInterrupt`(exit -2)로 끝남" — chaos 테스트 `exit 0` 단정이 잡음.
- "`log.info(..., extra={"module": …})` — `module` 은 LogRecord 예약 필드 → 핸들러 모듈이 하나라도 없으면 워커 기동 시 `KeyError`".
- "uvicorn 0.52 에서 `Server.startup()` 직접 호출 불가 … ops HTTP 서버가 `AttributeError`".
- "벤치 '워커'가 한 프로세스 안의 태스크 → 워커 수를 늘려도 처리량 불변(CPU 1개, ~230 ev/s)" → 워커를 프로세스로, 크래시는 실제 SIGKILL.

### WP-H ([`wp-h.md`](dev/handoff/wp-h.md), [`wp-h-phase2.md`](dev/handoff/wp-h-phase2.md))
- "`cdk-nag 3.0.2` 는 `aws-cdk-lib 2.267` 의 jsii 런타임에서 `aspect.visit is not a function` 으로 실패해 `2.38.2` 로 고정".
- 통합자 e2e 가 잡음: "`loadtest/client.py::SessionStats.last_final_seq` 기본값 0 → −1 (세그먼트 seq 는 0부터라 첫 final 이 중복으로 세어짐)" ([`integrator.md`](dev/handoff/integrator.md) §4).
- Phase 2 스모크(A N=10): 뷰어의 `final_e2e` 가 세그먼트 seq 로 `sent_at` 을 찾고 있었다 — 청크 seq 와 다른 축이라 값이 무의미했다 → `t_end_ms → 청크 seq` 역산(콘솔과 같은 규칙), 단위 테스트 갱신.
- Phase 2 스모크가 관찰한 코어 결함(수정은 WP-B 몫): `IngestCore.on_ledgered` 가 `now_ms` 를 갱신하지 않아 100 ms ack 규칙이 지난 틱 시각으로 평가되고, 5 chunk/s 에서 ack RTT 가 200 ms 틱 근처로 양자화된다 — 요청 diff 는 [`wp-h-phase2.md`](dev/handoff/wp-h-phase2.md) §4.

### 통합자 ([`integrator.md`](dev/handoff/integrator.md) §2)
- "`serve all --embedded` 의 stt-worker 가 **스레드 + 자체 이벤트 루프**로 돌아 자체 풀·자체 드레이너·자체 ops 포트(9002)를 가짐 → SIGTERM 에 api/worker 만 드레인되고 프로세스가 살아남음(좀비)" — 데모 서버 재시작이 드러냄.
- "`POST /sessions/{id}/end` 가 500 — `routers/sessions.py::_announce_end` 가 `SessionState(scripts_dir=settings.scripts_dir)` 로 Lua 를 STT 스크립트 디렉터리에서 찾음".
- "로그인 500 `permission denied for sequence audit_events_id_seq` — 코드 결함 아님: 개발 DB 가 0005 에 GRANT 가 추가되기 전에 만들어짐".

## 7. 스펙과 다르게 한 결정의 기록 위치

각 핸드오프의 "스펙과 다르게 한 점" 절이 권위다(A §5, B §4, C §4, D §4, E §4, F §6, G §4, H §4/§3). 통합자가 거절한 요청은 `integrator.md` §1 표에 이유와 함께 있다(예: 벤치의 벌크 INSERT 를 repo 로 옮기는 요청 — 측정 하네스 전용이라 거절).
