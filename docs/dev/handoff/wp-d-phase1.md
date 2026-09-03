# WP-D Phase 1 핸드오프 — Notes 서비스 · note_draft 핸들러 · REST (스펙 §9, §6.9, §7.2, §8.4)

> 상태: **완료** (2026-09-03, 2차 실행에서 마무리). 게이트: `pytest tests/integration/test_notes_service.py tests/integration/test_notes_rest.py -p no:xdist`
> **28 passed / 0 failed** (≈9 s, 실제 PG/Redis) · `pytest tests/unit/test_notes_*.py` 140 passed (Phase 0 회귀 없음) ·
> `ruff check` + `ruff format --check` clean · `mypy src/chartwire/notes src/chartwire/api/routers/notes.py src/chartwire/worker/handlers/note_draft.py` clean ·
> `mypy --strict src/chartwire/notes/verifier.py src/chartwire/outbox` clean.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4`
> 라이브 포트 8103 은 쓰지 않았다 — REST 테스트는 in-process ASGI(`httpx.ASGITransport`)다. git 상태를 바꾸는 명령은 실행하지 않았다.

## 0. 체크리스트 (재개용)

- [x] `notes/repo_adapter.py` — `SegmentRow` → `SegmentView` (세션 DEK 복호화, AAD = `ws.watch.segment_aad`), `load_segment_views`, `encrypt_segment_text`
- [x] `notes/service.py` — `draft_for_session`, `provider_from_settings`, `read_note`/`get_note`/`latest_note`, `request_draft`, `decide_statement`, `put_assessment`, `sign`, `build_signed_content`, `retention_until`
- [x] `worker/handlers/note_draft.py` — `@handler("session.transcribed", lease_s=120)`
- [x] `api/routers/notes.py` — §6.9 노트 라우트 6개, 감사, auditor 메타데이터 전용, problem+json 핸들러
- [x] `tests/integration/test_notes_service.py` (17) · `tests/integration/test_notes_rest.py` (11) · `tests/integration/notes_support.py`(공용 시드)
- [x] `docs/grounding.md` §8 (초안 → 검토 → 서명 → 파기 생존)
- [x] ruff / ruff format / mypy / 최종 테스트, 이 문서

## 1. 만든 것 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `notes/repo_adapter.py` (55) | `to_view(row, dek)` — `Envelope.decrypt(dek, text_enc, segment_aad(tenant, sid, seq))`; `load_segment_views(session, session_id=, dek=, started_at=)` — `repo.segments.replay` 를 500행 keyset 배치로(파티션 프루닝 유지); `encrypt_segment_text` (테스트·픽스처의 역함수). 복호화 실패는 예외 — 보증할 수 없는 세그먼트 위에 초안을 쓰지 않는다 | §9.2, §8.2 |
| `notes/service.py` (855) | **`draft_for_session(ctx, session_id, *, tenant_id=None, provider=None) -> NoteOutcome`**: 읽기 tx(세션·테넌트·**현재** 동의 `ai_drafting` 게이트·세그먼트 복호화) → 프로바이더(`parse_draft` 실패 시 1회 재호출, `ProviderError` → `provider_error`, 대체 없음) → `verify` → `decide` → 쓰기 tx(`notes` version=이전+1, `raw_draft_enc`(세션 DEK), `prompt_hash`, coverage/카운트 + `note_statements` `text_enc`/evidence `{seq, quote_hash, start, end, method}`/verdict + `sessions.state='drafted'` + 감사 `note.drafted`·`session.drafted`) → PUBLISH `note.status{note_id,status,coverage,unsupported_count}` → 메트릭 4종. `tenant_id` 가 없으면 활성 테넌트를 순회해 찾는다(ADR-0001). 파기된 세션은 `skipped`. **`read_note`**: 서명 노트는 `signed_content_enc`(기록 키)에서만, 초안은 문장 복호화 + `raw_draft_enc` 재파싱으로 verbatim 인용 복원. **`decide_statement`**(edit 는 `edited_text_enc`, 세션 DEK), **`put_assessment`**(`note_assessments` 유일 쓰기 경로, 기록 키), **`sign`**(평가 필수 + unsupported 전부 reject/edit → 자기완결 문서를 기록 키로 봉인, `legal_hold`, `retention_until=+10년`, `sessions.state='signed'`, 감사 `note.signed`·`session.signed`). 오류 코드 `CW-4041/4091/4092/4093/4094/4221` | §9, §7.2, §8.4, ADR-0004 |
| `worker/handlers/note_draft.py` (29) | import 만으로 등록. payload `session_id`(없으면 `aggregate_id`) + `event.tenant_id` 로 서비스 호출. 로그는 id·status·reason 만 | §7.2 |
| `api/routers/notes.py` (283) | `GET /v1/sessions/{id}/notes/latest`, `GET /v1/notes/{id}` (clinician own / auditor 메타데이터, 감사 `note.read{version,status,redacted}`), `POST /v1/sessions/{id}/notes/draft` → 202 `{session_id, event_id, queued}` (outbox `session.transcribed` + `outbox:wake`), `POST /v1/notes/{id}/statements/{sid}/decision`, `PUT /v1/notes/{id}/assessment`, `POST /v1/notes/{id}/sign` — 전부 `NoteOut` 을 돌려준다. `require(...)` 역할은 `rbac.MATRIX` 와 동일. `install_error_handlers(app)` = `AppError` → problem+json | §6.9, §8.1 |
| `tests/integration/notes_support.py` | 실제 래핑 기록 키·세션 DEK·동의·암호화 세그먼트 시드(`seed`), `purge_like`(§8.4 4–6단계 흉내), `build_app`(`create_app` 이 있으면 그것, 없으면 라우터만 있는 앱; `app.state.deps` 주입), JWT 발급 | §4.4 |
| `docs/grounding.md` §8 | 초안 표, 검토, 서명 문서 구성, 파기 생존 | §14 |

LOC(비공백): 구현 1,222 (service 855 · router 283 · adapter 55 · handler 29) / 테스트 889. Phase 0 과 합쳐 notes 영역 ≈ 2,800 / 2,050 — §0 예산(1,000/600)을 넘는다. 큰 몫은 `service.py` 의 서명 문서·읽기 모델(`read_note`/`build_signed_content`)과 REST 스키마다. 줄이지 않고 그대로 보고한다.

## 2. 테스트 (28 passed)

```bash
source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a
export CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4
pytest tests/integration/test_notes_service.py tests/integration/test_notes_rest.py -q -p no:xdist   # 28, 실제 PG/Redis
pytest tests/unit/test_notes_*.py -q                                                                  # 140, Phase 0
ruff check src/chartwire/notes src/chartwire/api/routers/notes.py src/chartwire/worker/handlers/note_draft.py tests/integration/test_notes_*.py tests/integration/notes_support.py
mypy src/chartwire/notes src/chartwire/api/routers/notes.py src/chartwire/worker/handlers/note_draft.py
```

| 파일 | 검증 내용 |
|---|---|
| `test_notes_service.py` (17) | 추출형 초안 → `verified`, coverage 1.0, S3/O1/P2 · `raw_draft_enc` 와 문장 `text_enc` 가 세션 DEK + AAD 로 왕복 · evidence JSONB 에 인용문 없음(해시·오프셋만, 오프셋 슬라이스 == 인용) · `read_note` 가 verbatim 인용 복원 · `sessions.state='drafted'`, 감사 2건, `note.status` pub/sub 페이로드 정확히 일치 · 재초안 version 2 · tenant_id 없이 호출(테넌트 탐색) · 동의 없음/철회 → `consent_scope_missing`(프로바이더 호출 0, DEK 미언랩) · 실패 프로바이더 → `provider_error`(호출 1, 대체 없음) · `assessment` 키 → 2회 호출 뒤 `schema`(거부 출력 암호화 보관) · anthropic 선택 + 모델 없음 → `provider_error` · 파기 세션 → skipped · **녹음 픽스처 7개**가 서비스 전체 경로에서 기대 상태·사유·지지 수 일치 · 핸들러 등록(lease 120) + **실제 `Poller.run_once`** 로 초안 생성 + `processed_events` + 재전달 skip · caplog 에 전사 텍스트·거부 출력 0건 · 보존 기간 10년(윤일 포함) |
| `test_notes_rest.py` (11) | 소유 임상의 latest/by-id 200 + `note.read` 감사(request_id 포함) · 404 problem+json · 타 임상의 403 `CW-4030`, staff/admin/recorder 403, 토큰 없음 401 · auditor 200 메타데이터만(응답 본문에 전사 텍스트 0, text/quote/assessment null, 결정은 403) · 수동 초안 202 → outbox `session.transcribed{manual}` → 폴러 → version 2 · **검토 흐름**: 평가 없이 서명 409 `CW-4092` → 평가 저장(기록 키로 복호화 확인, 공백만 422) → unsupported 미결 409 `CW-4093` → accept 로는 해제 안 됨 → edit 무텍스트 422 · 추가 키 422 · 없는 문장 404 → reject/edit/accept → 서명 200(`legal_hold`, `retention_until=+10년`, 거부 문장 제외, 수정 문장 반영, 세션 `signed`, 봉인 문서 기록 키 복호화, 인용 임베딩, 감사 2건) → 서명 뒤 sign/assessment/decision 전부 409 `CW-4091` · 기권 노트는 검토·서명 불가 `CW-4094` · **파기 뒤 서명 노트 생존**: 세그먼트 삭제 + DEK 파기 뒤 `GET /notes/{id}` 가 서명 전과 동일한 문장·인용·평가를 돌려주고, 세션 DEK 로는 봉인 문서가 열리지 않음 · 파기 뒤 미서명 초안 404, 초안 요청 409 |

## 3. 이번 실행에서 잡은 결함 (테스트가 발견)

| 증상 | 원인 | 수정 |
|---|---|---|
| 녹음 픽스처가 서비스 경로에서 전부 `unsupported` | 테스트 시드가 세그먼트 seq 를 0부터 다시 매겨 픽스처의 evidence seq(1부터)와 어긋남 | `seed(script=[(seq, speaker, text)])` 형태 지원 — 서비스 결함 아님 |
| 추출형 기대 6문장 vs 실제 5 | `에스시탈로프람 10mg 먹고 있어요` 에는 §9.2 cue(`약|복용…`)가 없다 | 시드 발화를 `…10mg 약을 먹고 있어요` 로 |
| `list_statements` 순서 가정 | CHAR 정렬은 `O < P < S` | 테스트가 정렬 기준을 명시 |

## 4. 계약·스펙과 다르게 한 점 (이유)

1. **`draft_for_session(ctx, session_id, *, tenant_id=None, provider=None)`** — 계약의 2인자 형태를 유지하되 핸들러는 `tenant_id=event.tenant_id` 를 넘긴다(RLS GUC 가 필요). 없으면 활성 테넌트 순회(ADR-0001).
2. **`NoteOutcome`** 은 dataclass `{session_id, note_id, version, status, abstain_reason, provider, coverage, statement_count, unsupported_count, published}`; `note_id=None`/`status="skipped"` = 파기된 세션.
3. **`note.status` 발행 시점** — 폴러 아래서는 핸들러 tx 커밋 *직전*이다(서비스의 쓰기 tx 가 핸들러 tx 에 참여하므로). 콘솔은 토스트 뒤 `notes/latest` 를 GET 하므로 실질 영향 없음; 커밋 뒤 발행이 꼭 필요하면 WP-G 폴러의 post-commit 훅으로 옮기면 된다(§5 요청).
4. **기권 노트에도 문장을 저장** (`low_coverage`/`provider_abstain`): 검증 결과가 있으면 암호화해 남긴다(왜 기권했는지 보이도록). `consent_scope_missing`/`provider_error`/`schema` 는 문장 없음. 기권 노트는 검토·서명 불가(`CW-4094`).
5. **서명 문서에 포함되는 문장** — `reject` 제외, `edit` 는 수정문(+`original_text`), 결정 없는 supported 문장은 수락으로 간주(스펙은 unsupported 문장에만 결정을 요구). 수정된 unsupported 문장은 매치된 근거만 붙는다.
6. **감사 액션 추가** — `note.assessment`, `note.draft_requested`, `session.drafted`/`session.signed`(§8.5 목록의 세션 액션). `note.read.detail={version,status,redacted}`.
7. **REST 응답 형태** — `NoteOut` 에 `session_id, version, provider, model, statement_count, legal_hold, retention_until, signed_at, signed_by, created_at` 추가(§3.2 의 상위 집합; 콘솔이 `legal_hold`/`retention_until` 을 읽는다). auditor 는 `text/edited_text/evidence.quote/assessment.text` 가 `null`(키는 남김 — 콘솔 렌더러가 같은 모양을 기대). decision/assessment/sign 도 `NoteOut` 을 돌려준다(콘솔이 재로드 없이 그릴 수 있게).
8. **`evidence` JSONB 에 인용문 없음** — 스펙 §4.2 그대로 `{seq, quote_hash, start, end, method}`. 인용문은 `raw_draft_enc` 재파싱(세션 DEK)으로 복원하고, 서명 문서에 verbatim 으로 박힌다.
9. **문제 코드** — `CW-4041`(노트/문장 없음), `CW-4091`(이미 서명), `CW-4092`(평가 없음), `CW-4093`(unsupported 미결), `CW-4094`(기권 노트/파기 세션), `CW-4221`(edit 텍스트/평가 본문 없음). 역할 거부는 WP-E 의 `CW-4030`.
10. **`anthropic` 프로바이더 생성 실패**(SDK 없음, 키 없음, 모델 미설정)도 `abstained(provider_error)` — 핸들러 예외로 DLQ 에 가지 않는다.

## 5. 다른 WP 에 요청

- **WP-E (`api/app.py`)** — 계약대로 `include_router(chartwire.api.routers.notes.router)` 를 try/except ImportError 로. `AppError` → problem+json 핸들러가 없다면 `chartwire.api.routers.notes.install_error_handlers(app)` 를 호출하면 된다(이미 있으면 no-op). `get_deps(request)` 는 `chartwire.api.deps` 에서 import 하고, 없으면 `request.app.state.deps` 를 읽는다. `AppDeps` 는 `engine, keycache, clock, redis` 를 이 라우터가 쓴다. `consent.service.active_scopes_for_patient(session, patient_id)` 가 생기면 서비스가 자동으로 그것을 쓴다(없으면 `patients.list_consents` + `gates.active_scopes`).
- **WP-E (purge)** — `purge_run` 은 §8.4 대로 `notes`/`note_statements` 를 `legal_hold IS NULL` 인 것만 지우면 된다. 서명 노트는 `signed_content_enc` 만으로 읽히므로 문장 행이 남아도(DEK 없이는 열리지 않음) 문제없다. `keys:invalidate` 페이로드가 세션 id 문자열이면 `keycache.invalidate(sid)` 로 이 서비스의 캐시도 비워진다. 서명 문서의 `purge_receipt_id` 는 `null` 로 두었다 — 파기 뒤 채우려면 `notes.signed_content_enc` 를 다시 봉인해야 하므로 넣지 않았다(감사 이벤트로 연결 가능).
- **WP-G (`outbox/poller.py`)** — 선택: 핸들러가 "커밋 뒤 실행" 콜백을 등록할 수 있는 훅이 있으면 `note.status` 발행을 거기로 옮긴다. 지금은 커밋 직전 발행(위 §4-3).
- **WP-C (`stt/worker.py`)** — `session.transcribed` payload `{session_id, patient_id}` 그대로. 세그먼트 AAD 는 `ws.watch.segment_aad` 여야 이 서비스가 복호화한다(테스트 시드는 같은 함수를 쓴다).
- **WP-H (console)** — 서명 응답에 `legal_hold`/`retention_until`/`signed_at` 이 있고, decision/assessment 응답은 갱신된 `NoteOut` 이다. auditor 응답은 텍스트 필드가 `null`.
- **통합자** — `tests/integration/notes_support.py` 는 두 스위트의 공용 시드 모듈(테스트 파일 아님). `pytest tests/integration` 을 한 번에 돌릴 때 이 WP 의 두 파일은 다른 스위트와 같은 DB 를 쓰므로 직렬(`-p no:xdist`)로.

## 6. 알려진 이슈 / 남은 일

- `create_app` 이 아직 없어 REST 테스트는 라우터만 있는 앱으로 돌았다. `build_app` 은 `create_app` 이 생기면 그것을 쓰도록 되어 있지만(lifespan 없이 deps 주입), WP-E 통합 뒤 한 번 확인이 필요하다 — 특히 `create_app` 이 자체 `AppError` 핸들러를 갖는 경우 헤더/본문 모양이 이 테스트의 기대(`application/problem+json`, `code`)와 같아야 한다.
- 인용문 복원은 `raw_draft_enc` 파싱에 의존한다. 스키마는 통과했지만 문장 수가 저장 행과 다른 비정상 상태(있을 수 없지만)에서는 인용문이 빈 문자열이 된다 — 실패가 아니라 빈 값.
- `read_note` 는 GET 마다 `raw_draft_enc` 를 복호화·파싱한다(수 KB, 무시 가능). 서명 뒤에는 봉인 문서 하나만 연다.
- `session.transcribed` 수동 요청은 세션 상태를 검사하지 않는다(파기만 거부). 녹음 중인 세션에 요청하면 그 시점의 세그먼트로 초안이 만들어진다 — 콘솔은 종료 뒤에만 버튼을 켠다.
