# WP-D 핸드오프 — Notes/AI (스키마 · 검증기 · 정책 · 프로바이더), Phase 0

> 상태: **완료** (2026-09-03, 재개 실행에서 마무리). 게이트: `pytest tests/unit/test_notes_*.py` **140 passed / 0 failed** ·
> `mypy --strict src/chartwire/notes` clean (스펙 요구는 `verifier.py` 만; 패키지 전체가 통과) ·
> `ruff check` + `ruff format --check` clean (`src/chartwire/notes`, `tests/unit/test_notes_*.py`, `scripts/eval_anthropic.py`).
>
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4`
> (이 WP 의 테스트는 DB/Redis 를 쓰지 않는다 — 순수 함수 검증이다.)
>
> Phase 1 (D1) 항목 — `notes/service.py`(`draft_for_session`, `sign`) 와 `worker/handlers/note_draft.py` — 는 이 실행의 범위가 아니다 (§7 아래 "다음 단계").

## 1. 무엇을 만들었나 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `notes/schema.py` | `Evidence`/`Statement`/`NoteDraftOut` (전부 `extra="forbid"`, frozen; assessment·diagnosis·verdict 필드 **없음**), `parse_draft(text)` → `DraftSchemaError`(값 없이 위치·유형만), `SegmentView`, `DraftContext`, `RawDraft`, `NoteProvider` Protocol, 검증기 출력 `VerifiedEvidence{start,end,method}`/`VerifiedStatement{verdict,verdict_reason}`/`VerifiedDraft{coverage,unsupported_count,abstain_requested}`, `NoteStatus`(= `notes.status` CHECK), `VerdictReason`/`AbstainReason` Literal | §9.1, §9.2, §9.3 |
| `notes/normalize.py` | `nfc`, `normalize`(NFC → 구두점 제거 → 공백 제거), `normalize_with_offsets`(정규화 문자 → NFC 원문 오프셋 역매핑), `normalize_numbers_ko`(한/두/세…열, 스물…쉰, 열두=12, 반=.5), `numeric_tokens`(`㎎`=`mg`, `2.0주`=`2주`) | §9.3 규칙 2·3 |
| `notes/lexicon_drugs.py` (56) · `notes/lexicon_diagnoses.py` (50) | 일반명/진단명 frozenset + 최장 우선 튜플 | §9.3 규칙 4·7 |
| `notes/verifier.py` (**mypy --strict**) | `verify(draft, segments) -> VerifiedDraft`: 8 규칙 순서대로, 첫 실패가 사유, 유사도 완화 없음, 예외 없음. `VERIFIER_VERSION="rules-8.v1"`. `NEGATION_RE`/`VERDICT_LANGUAGE_RE`/`INJECTION_RE` 공개(추출형·변이가 재사용) | §9.3 |
| `notes/policy.py` | `decide(verified, *, schema_failures=0, provider_error=False) -> Decision{status, reason}`; 상수 `MAX_SCHEMA_FAILURES=2`, `REVIEW_MAX_UNSUPPORTED=3`, `REVIEW_MIN_COVERAGE=0.85` | §9.3 |
| `notes/extractive.py` | `ExtractiveProvider`(기본, 키 없음, 결정론) + 순수 코어 `build_draft`/`classify`: 스펙 cue 정규식 그대로, 임상가 **질문** 제외, 규칙 8 에 걸리는 발화는 초안에서 제외(→ coverage 1.0 이 구성상 참), 200자 초과 발화는 190자 접두 인용, 섹션당 ≤12, seq 순 | §9.2 |
| `notes/korean.py` | `report_form`(`…요` → `…다고 함`), `transform_ending(utt, "report"\|"plain"\|"formal")` — 자모 산술 + 소형 표; 모르는 어미는 `“…”라고 함` 직접 인용형 | §9.2 |
| `notes/providers/base.py` | `ProviderError`, `Stopwatch`, `raw_from_draft`(타입 초안 → JSON 텍스트: 모든 프로바이더가 같은 `parse → verify → decide` 경로) | §9.2 |
| `notes/providers/recorded.py` | `Fixture` 모델(`raw` 는 untyped — 스키마 거부도 녹음 가능), `RecordedProvider`, `iter_fixtures` | §9.2 |
| `notes/providers/mutation.py` | `MutationProvider(base, classes, seed)` + `mutate()`: 7 변이 클래스, `EXPECTED_REASON` 표, `last_applied`(문장 인덱스 → 평가가 문장 단위로 판정) | §9.2, §11.1 |
| `notes/providers/paraphrase.py` | `ParaphrasingMockProvider(seed)` + `paraphrase()`: 어미 plain/formal, 문두 조사 교체, 동의어 5쌍, 인접 병합(근거 2개), 수사↔숫자, 존대 제거 — **인용은 verbatim 유지**, `last_applied` | §9.2 |
| `notes/providers/anthropic.py` | `AnthropicProvider(model, api_key, timeout_s=30, client=)`: SDK import-guard, 도구 1개(`input_schema=NoteDraftOut.model_json_schema()`) 강제 호출, `<segment seq speaker>` 데이터 블록(XML 이스케이프), 시스템 프롬프트(verbatim 인용·세그먼트 안 지시문 무시·평가 없음), `PROMPT_HASH`; 타임아웃/SDK 오류(클래스명만)/refusal/tool_use 없음 → `ProviderError`. `build_request()` 는 순수 함수라 테스트가 요청 모양을 검사 | §9.2 |
| `tests/fixtures/anthropic/*.json` (7) | `01_assessment_key`(스키마 거부) · `02_hallucinated_quote` · `03_numeric_mismatch`(10→20mg) · `04_valid_paraphrase`(verified) · `05_injection` · `06_provider_abstain` · `07_speaker_mismatch` | §9.2 |
| `scripts/eval_anthropic.py` | 키 없이 import·실행 가능(픽스처 재생 모드 → `docs/eval/anthropic.json` 같은 모양); 키+모델이 있으면 §10.2 스크립트를 라이브 프로바이더로. 보고서는 카운트만, 전사 텍스트 없음 | §9.2, §11.4 |
| `docs/grounding.md` (한국어) · `docs/adr/0003-…md` | 파이프라인 한 장 요약, 규칙표, 정책표, 프로바이더표, 검증기가 증명하지 않는 것, 의도된 한계 / 결정·결과 | §14 |

## 2. 테스트 (140, 전부 `tests/unit/`, DB 불필요)

| 파일 | 내용 |
|---|---|
| `test_notes_schema.py` | extra=forbid(assessment/diagnosis/verdict 키 거부), 길이·개수 한계, `parse_draft` 오류 메시지에 값 없음 |
| `test_notes_normalize.py` | 정규화·오프셋 역매핑·수사 변환·숫자 토큰 |
| `test_notes_korean.py` | 어미 변환 15 케이스 × 3 형, 모르는 어미 폴백, 어미 앞 본문 불변 |
| `test_notes_verifier.py` | 규칙 1–8 각각 양성/음성, 규칙 순서(첫 실패), `unknown` 화자 fail-closed, 인용 안 진단어 허용, 오프셋, coverage 집계 |
| `test_notes_policy.py` | 임계값 상수, 경계값(0.857/0.833, 2 vs 3), schema/provider_error/abstain 우선순위 |
| `test_notes_extractive.py` | cue·섹션·질문 제외·주입 제외·≤12·seq 순·긴 발화·결정론·**coverage 1.0** |
| `test_notes_mutation.py` | 7 클래스 × 200 탐지율(아래 표), 시드 결정론, 문장당 1변이, 비변이 문장 supported 유지 |
| `test_notes_paraphrase.py` | 변형별 거짓기각률(아래 표), 인용 verbatim, 병합=근거 2개, 수사↔숫자 양방향 |
| `test_notes_recorded.py` | 픽스처 7개 전부 `parse → verify → decide` 로 기대 상태·사유·지지 수 일치, assessment 키는 검증기에 도달하지 않음, 주입 픽스처 누출 0 |
| `test_notes_anthropic.py` | 가짜 클라이언트: 요청 모양(도구 스키마·강제 tool_choice·temperature 없음·태그 이스케이프), tool input → 텍스트 → 검증, 추가 키 → 스키마 거부, 타임아웃/refusal/tool_use 없음/SDK 오류(본문 미포함) → `ProviderError`, 무관 예외 전파 |
| `test_notes_eval_script.py` | 키 없이 import, 라이브 모드는 키+모델 요구, 녹음 모드 보고서 헤더·카운트만 |

실행: `pytest tests/unit/test_notes_*.py -q` · 표 출력: `pytest tests/unit/test_notes_mutation.py tests/unit/test_notes_paraphrase.py -s`

## 3. 측정 (단위 테스트 코퍼스, `pytest -s` 출력 그대로 — README 용 숫자 아님)

아래 숫자는 `tests/unit/test_notes_mutation.py` 의 손으로 쓴 발화 20+11개로 만든 무작위 세션(시드 42)에서 나온 것이다.
README 에 들어갈 **권위 있는** 숫자는 WP-F 의 `chartwire eval` 이 합성 코퍼스에서 `inject.json` / `paraphrase.json` 으로 만든다 (§11.4).

변이 탐지 (클래스당 200):

| class | detected | reasons |
|---|---|---|
| number_change | 1.000 | {'numeric_mismatch': 200} |
| drug_swap | 1.000 | {'entity_mismatch': 200} |
| fabricated_statement | 1.000 | {'quote_mismatch': 200} |
| evidence_seq_wrong | 1.000 | {'fabricated_segment': 200} |
| diagnosis_insert | 1.000 | {'verdict_language': 200} |
| negation_flip | 1.000 | {'negation_mismatch': 200} |
| speaker_swap | 1.000 | {'numeric_mismatch': 79, 'speaker_mismatch': 76, 'negation_mismatch': 45} |

`speaker_swap` 은 S 문장이 임상가 세그먼트를 인용하게 바꾸는데, 규칙 3·5 가 규칙 6 보다 먼저라 사유가 `speaker_mismatch` 가 아닐 수 있다. 탐지(unsupported)는 100 % 다.

바꿔쓰기 거짓 기각 (300 세션, 변형은 문장마다 무작위):

| transform | rejected/total | rate | reasons |
|---|---|---|---|
| ending_plain | 0/290 | 0.000 | {} |
| ending_formal | 0/255 | 0.000 | {} |
| particle_swap | 0/101 | 0.000 | {} |
| synonym | 14/36 | 0.389 | {'negation_mismatch': 14} |
| merge | 0/207 | 0.000 | {} |
| number_word | 0/57 | 0.000 | {} |
| honorific_drop | 0/282 | 0.000 | {} |
| **all** | 14/1228 | 0.011 | |

동의어 거부는 전부 부정을 명사로 흡수하는 3쌍(`잠을 못 자요→수면 곤란`, `입맛이 없어요→식욕 저하`, `기운이 없어요→무기력감`)이고
규칙 5 (fail-closed) 가 잡는다. 규칙을 느슨하게 해서 숫자를 좋게 만들지 않는다 (ADR-0003). 부정이 없는 2쌍(`가슴이 두근거려요→심계항진 호소`, `걱정이 많아요→과도한 걱정`)은 통과한다.

## 4. 계약 편차 (전부 상위 집합, 호출부는 스펙대로 써도 됨)

- `policy.decide(verified) -> Decision{status, reason}` — 스펙의 `-> NoteStatus` 대신 `.status` 를 가진 데이터클래스. `NoteOut.abstain_reason` 을 채우려면 사유가 같이 나와야 한다. 키워드 `schema_failures`, `provider_error` 추가.
- `verify()` 는 `VerifiedDraft.abstain_requested`(프로바이더 `abstain=true`)를 함께 돌려주고, `decide` 가 `provider_abstain` 으로 매핑한다 (스펙 §9.3 의 abstain 사유 목록에 없던 값; `AbstainReason` 에 `provider_abstain`, `provider_error`, `consent_scope_missing` 포함).
- `notes/korean.py` 는 §3 레이아웃에 없는 모듈이다 — `report_form` 과 바꿔쓰기 어미 변환을 한곳에 두기 위해 분리했다.
- `AnthropicProvider` 는 `temperature` 를 보내지 않는다 — 현재 모델은 샘플링 파라미터를 400 으로 거부한다(`claude-api` 스킬). 강제 `tool_choice` 는 Opus 계열(`claude-opus-5`)에서 동작하고 강제 도구 호출을 제거한 모델에서는 400 이다; `CHARTWIRE_ANTHROPIC_MODEL` 은 기본값이 없으므로 키 소지자가 Opus 계열을 지정한다. `DEFAULT_MAX_TOKENS=16000`(비스트리밍 기본).
- 추출형은 규칙 8 정규식에 걸리는 발화를 **초안에 넣지 않는다** (스펙은 "coverage 1.0 by construction" 만 말함). 이것이 그 문장을 실제로 참으로 만든다.
- 규칙 6: `speaker == "unknown"` 세그먼트를 인용하면 항상 `speaker_mismatch` (fail-closed).
- LOC: impl 1,572 / tests 1,167 (예산 1,000 / 600). 초과분의 대부분은 사전 2개(144), `korean.py`(170), 픽스처 재생·평가 스크립트 경로다. 줄이지 않고 그대로 보고한다.

## 5. 알려진 한계 (의도된 것, `docs/grounding.md §7` 과 동일)

- 규칙 5 의 `안␣` 는 `불안 증상` 에도 걸린다(문장·인용 양쪽에 있으면 상쇄). 규칙 8 의 `적어`·`지시` 는 일상 표현에도 걸린다 — 안전 쪽으로 틀린다.
- 숫자 규칙은 단위가 붙은 숫자만 본다(`새벽 4시` 는 비교 안 함). 약물 사전은 일반명 56개뿐.
- 검증기는 의미적 함의를 증명하지 않는다("기분이 좋다고 함" 을 "기분이 계속 가라앉아요" 위에 쓰면 통과). 이 계층은 없는 말을 지어내는 것을 막고, 나머지는 임상가가 근거 인용을 옆에 두고 읽는다.
- `AnthropicProvider` 는 빌드에서 실행되지 않았다. README 는 "미실행" 으로 적는다.

## 6. 다른 WP 에 요청

- **WP-F (eval)** — `inject.json` 은 `MutationProvider(ExtractiveProvider(), MUTATION_CLASSES, seed)` 를 세션마다 돌리고 `provider.last_applied[i].index` 의 문장만 판정하면 된다 (`tests/unit/test_notes_mutation.py::detection_rates` 가 참조 구현). `paraphrase.json` 은 `ParaphrasingMockProvider(seed)` + `last_applied[i].transform` 로 변형별 집계 (`test_notes_paraphrase.py::false_rejections`). 보고서에 `verifier_version=VERIFIER_VERSION` 을 넣어 달라. 코드 변경 요청 없음.
- **WP-E / WP-G** — 계약대로 `chartwire.notes.service.draft_for_session(ctx, session_id) -> NoteOutcome` 과 `worker/handlers/note_draft.py`(`session.transcribed`, `ConsentScopeMissing` → `abstained(consent_scope_missing)`)는 Phase 1 D1 에서 이 WP 가 만든다. 그때까지 `create_app`/worker main 의 import 는 try/except ImportError 로 감싸 달라(계약 그대로).
- **WP-A** — 없음. `SegmentView` 는 `chartwire.notes.schema` 에 있고 WP-B 의 `repo.segments.replay -> list[SegmentView]` 와 필드가 같다.

## 7. 다음 단계 (Phase 1 D1, 이 WP)

`notes/service.py`: 동의 게이트(`ai_drafting`) → `repo.segments.replay` → 프로바이더 선택(`settings.note_provider`, 대체 없음) → `parse_draft`(≤2회) → `verify` → `decide` → `repo.notes` 저장(문장·근거·verdict) → `note_status_total`/`note_coverage`/`note_verify_reason_total`/`note_draft_seconds` 메트릭 → 감사 기록; `sign(...)`(ADR-0004: 기록 키, `retention_until`, 인용 임베딩). `worker/handlers/note_draft.py` 등록. `api/routers/notes.py`(WP-E 계약).
