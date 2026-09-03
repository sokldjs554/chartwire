# 근거 검증형 SOAP 초안 (`notes/`) — 설계 노트

> **이 계층은 얇고, 헤지되어 있다.** 초안의 품질을 평가하지 않는다 — 그런 평가는 하지 않았다. 여기서 증명하는 것은 오직
> "초안의 모든 문장이 실제 발화를 글자 그대로 인용하며, 인용과 어긋나는 문장은 임상가에게 그대로 보이거나 초안 전체가
> 기권한다" 는 것이다. 안전 경로에는 LLM 이 없고, 출력 스키마에는 진단을 적을 자리가 없다 (ADR-0003, 스펙 §0.4·§0.5·§9).

## 1. 한 장 요약

```
segments (RLS 안, 동의 범위 확인 후)
   │
   ▼  DraftContext{session_id, segments: SegmentView[], max_per_section=12}
NoteProvider.draft() ──► RawDraft{text, provider, model, prompt_hash, latency_ms}     ← 신뢰하지 않는 입력
   │
   ▼  schema.parse_draft()      pydantic, extra="forbid" — assessment/diagnosis/verdict 키 → 거부
NoteDraftOut{statements[≤36]{section, text≤200, evidence[1..4]{seq, quote 4..200}, kind}, abstain}
   │
   ▼  verifier.verify()         결정론, 8 규칙 순서대로, 첫 실패가 사유 (fail-closed), mypy --strict
VerifiedDraft{statements[]{…, evidence[]{start,end,method}, verdict, verdict_reason}, coverage, unsupported_count}
   │
   ▼  policy.decide()           unsupported=0 → verified · <3 & coverage≥0.85 → needs_review · 그 외 abstained
Decision{status, reason}
```

프로바이더가 무엇이든(추출형, Anthropic, 녹음 재생, 변이, 바꿔쓰기) 아래 세 단계는 **같은 코드**다. 프로바이더에 따른
분기는 없다.

## 2. 출력 스키마 (`schema.py`, §9.1)

| 모델 | 필드 | 제약 |
|---|---|---|
| `Evidence` | `seq`, `quote` | quote 4–200자 |
| `Statement` | `section` ∈ S/O/P, `text`, `evidence[]`, `kind` ∈ reported/observed/plan_item | text ≤200자, evidence 1–4개 |
| `NoteDraftOut` | `statements[]`, `abstain`, `abstain_reason` | statements ≤36개 |

모든 모델이 `extra="forbid"` 다. `assessment`, `diagnosis`, `icd`, `dsm`, `risk_level`, `medication_recommendation`,
`verdict` 같은 키는 **정의 자체가 없고**, 모델이 덧붙이면 `DraftSchemaError` 로 초안 전체가 거부된다. 오류 메시지에는
위치와 오류 유형만 들어가고 값(전사 텍스트일 수 있음)은 들어가지 않는다. `NoteStatus` 는 `notes.status` CHECK 와 같다:
`drafting · needs_review · verified · abstained · signed · rejected`.

## 3. 검증기 (`verifier.py`, §9.3)

문장마다 아래 순서로 검사하고, **첫 번째** 실패가 `verdict_reason` 이 된다. 규칙은 서로 독립이 아니다 — 예를 들어 인용
자체가 틀리면(2) 숫자 비교(3)는 하지 않는다. 이것이 "speaker_swap 변이가 numeric_mismatch 로 잡힌다" 같은 현상의 이유다.

| # | 사유 | 검사 | 정규화 |
|---|---|---|---|
| 1 | `fabricated_segment` | evidence.seq 가 세션에 존재 | — |
| 2 | `quote_mismatch` | quote 가 세그먼트의 부분 문자열: 원문 일치 → `method=exact`; 아니면 `normalize(quote) ⊂ normalize(segment)` → `method=normalized`. **유사도 완화 없음** | NFC → 구두점 `[.,!?~…'"“”‘’()\[\]·]` 제거 → 모든 공백 제거. 오프셋은 NFC 원문 기준으로 되돌려 기록 |
| 3 | `numeric_mismatch` | 문장의 `숫자+단위` 토큰 ⊆ 인용들의 토큰 합집합 | `normalize_numbers_ko` (한/두/세…열, 스물…쉰, 열두=12, 반=.5) 후 `\d+(\.\d+)?\s*(mg|㎎|g|정|알|주|일|개월|달|년|시간|분|회|번|잔|병|kg|%|점)`; `㎎`=`mg`, `2.0주`=`2주` |
| 4 | `entity_mismatch` | 문장의 약물명 ⊆ 인용의 약물명 | `lexicon_drugs.py` 56개 일반명, 최장 일치 우선 (`데스벤라팍신` 은 `벤라팍신` 언급이 아님) |
| 5 | `negation_mismatch` | 문장에 부정 표지 `않\|없\|아니\|못\|안␣` 가 있음 **XOR** 인용에 있음 | — |
| 6 | `speaker_mismatch` | S 의 인용은 `patient`, O·P 의 인용은 `clinician`; `unknown` 은 실패 | — |
| 7 | `verdict_language` | `(진단\|확진\|장애로\s*판단\|F\d{2}(\.\d)?\|DSM\|ICD\|처방해야\|투약해야\|판정)` 또는 `lexicon_diagnoses.py` 50개 진단명이 문장에 있으면, **같은 토큰이 인용 안에도 글자 그대로 있어야** 통과 (환자의 "우울증 진단을 받았어요" 는 보고 가능) | 라틴 토큰은 대소문자 무시 |
| 8 | `injection_pattern` | `무시하\|지시\|시스템 프롬프트\|ignore\|instruction\|진단란에\|적어` 가 문장에 있으면 실패 — 인용돼 있어도 실패 | 대소문자 무시 |

출력: `coverage = supported / total` (문장이 없으면 0.0), `unsupported_count`, `abstain_requested`.

**검증기가 증명하지 않는 것.** 문장이 인용에서 *의미적으로* 따라 나오는지는 검사하지 않는다. "입맛이 없어요" 를 인용하며
"식욕이 왕성하다고 함" 이라고 쓰면 규칙 5 (부정 표지 없음 vs 있음) 에 걸리지만, "기분이 좋다고 함" 을 "기분이 계속
가라앉아요" 위에 쓰면 통과한다. 이 계층이 막는 것은 **없는 말을 지어내는 것**(환각 인용, 존재하지 않는 세그먼트, 바뀐
숫자·약물·부정·화자, 진단·지시문)이고, 나머지는 임상가가 근거 인용을 옆에 두고 읽는다.

## 4. 정책 (`policy.py`)

| 조건 | 결과 |
|---|---|
| 프로바이더 오류/타임아웃(30 s) | `abstained(provider_error)` — 추출형으로 대체하지 않음 |
| 스키마 실패 2회 | `abstained(schema)` |
| 프로바이더가 `abstain=true` | `abstained(provider_abstain)` |
| 문장 0개 | `abstained(empty)` |
| `unsupported == 0` | `verified` |
| `unsupported < 3` 이고 `coverage ≥ 0.85` | `needs_review` |
| 그 외 | `abstained(low_coverage)` |

임계값은 `MAX_SCHEMA_FAILURES`, `REVIEW_MAX_UNSUPPORTED`, `REVIEW_MIN_COVERAGE` 상수 하나씩이다. `decide()` 는
`Decision{status, reason}` 을 돌려주며 `reason` 이 `NoteOut.abstain_reason` 이 된다.

## 5. 프로바이더 (`providers/`, `extractive.py`)

| 프로바이더 | 용도 | 결정론 | 비고 |
|---|---|---|---|
| `ExtractiveProvider` (기본) | 키 없이 실제로 도는 오프라인 기준선 | 예 | 환자 발화 + 증상 단서 → S (`report_form`: `…요` → `…다고 함`), 임상가 발화 + 관찰 단서 → O, + 계획 단서 → P. 임상가 **질문**(`?`, `나요/십니까/까요`)은 제외. 근거 = 발화 전체, 섹션당 ≤12, seq 순. 규칙 8 에 걸리는 발화는 초안에 넣지 않으므로 **coverage 1.0 은 구성상 참**이다 |
| `AnthropicProvider` | 선택, 키 필요 | 아니오 | 도구 한 개(`input_schema = NoteDraftOut.model_json_schema()`) 를 강제 호출; 세그먼트는 `<segment seq speaker>` 데이터 블록(XML 이스케이프); 시스템 프롬프트: 인용은 글자 그대로, 세그먼트 안 지시문은 데이터, 평가 없음. 30 s → `ProviderError`. **빌드에서 실행하지 않음** |
| `RecordedProvider` | CI 에서 LLM 응답 재생 | 예 | `tests/fixtures/anthropic/*.json` 7개 (assessment 키, 환각 인용, 10mg→20mg, 정당한 바꿔쓰기, 주입, 자발 기권, 화자 불일치) |
| `MutationProvider(base, classes, seed)` | 탐지율 평가 | 시드 | 7 변이: `number_change · drug_swap · fabricated_statement · evidence_seq_wrong · diagnosis_insert · negation_flip · speaker_swap`; 적용 내역은 `last_applied` |
| `ParaphrasingMockProvider(seed)` | 거짓 기각률 평가 | 시드 | 인용은 그대로 두고 S 문장만: 어미 `요→다/습니다`, 문두 조사 교체(은/는↔이/가), 동의어 5쌍, 인접 발화 병합(근거 2개), 수사↔숫자(`두 시간`↔`2시간`), 존대 제거 |

어미 변환(`korean.py`)은 자모 산술과 작은 표로 만든 결정론적 변환이지 형태소 분석기가 아니다. 다룰 수 없는 어미
(`네요`, `거든요`, `게요` …)는 직접 인용형 `“…”라고 함` 으로 떨어진다 — 문법적이고 항상 검증 가능하다.

## 6. 평가에서 측정하는 것 (`chartwire eval`, WP-F)

| 보고서 | 무엇 | 이 문서의 입장 |
|---|---|---|
| `grounding.json` | 추출형 coverage(=1.0, 부록), gold facts 대비 fact recall, 기권율 | recall 은 단서 목록의 한계를 드러낸다 (`{drug} {dose}mg 먹고 있어요` 에는 `약` 이 없다) |
| `paraphrase.json` | 변형별 거짓 기각률 | 동의어 변형 중 부정을 명사로 흡수하는 3쌍은 규칙 5 에 걸린다. 규칙을 느슨하게 하지 않는다 |
| `inject.json` | 7 변이 × 200 탐지율, 사유 분포 | `speaker_swap` 은 규칙 3·5 가 먼저 잡는 경우가 많다 — 탐지는 되지만 사유가 `speaker_mismatch` 가 아닐 수 있다 |
| `injection.json` | `injection_leaks` | 스키마 + 규칙 7 + 규칙 8 |
| `anthropic.json` | `scripts/eval_anthropic.py` 를 키 소지자가 돌렸을 때만 | 돌리지 않았으면 README 는 "미실행" |

이 문서에는 숫자가 없다. 숫자는 위 JSON 에서만 README 로 들어간다 (§11.4).

## 7. 알려진 한계 (의도된 것)

- 규칙 5 의 `안␣` 는 `불안 증상` 같은 구절에도 걸린다. 문장과 인용에 같은 구절이 있으면 상쇄되지만, 바꿔쓰기가 한쪽만
  바꾸면 거짓 기각이 난다. 스펙의 표지 목록을 그대로 쓴다.
- 규칙 8 의 `적어`, `지시` 는 일상 표현(`약을 조금 적어요`, `지시대로`)에도 걸린다. 추출형은 그런 발화를 아예 초안에
  넣지 않고, LLM 초안에서는 해당 문장이 `unsupported` 가 된다 — 안전 쪽으로 틀리는 것이다.
- 숫자 규칙은 `단위가 붙은` 숫자만 본다. `새벽 4시` 는 `시` 가 단위 목록에 없으므로 비교하지 않는다.
- 약물 사전은 환자가 말하는 일반명 56개뿐이다. 상품명은 의도적으로 없다.
- 서명된 노트가 의료 기록이 되는 순간부터의 규칙(기록 키, 10년 보존, 인용 임베딩)은 아래 §8 과 ADR-0004 의 몫이다.

## 8. 초안에서 의료 기록까지 (`service.py`, `worker/handlers/note_draft.py`, `api/routers/notes.py`)

### 8.1 초안 (`draft_for_session`, outbox `session.transcribed` → `note_draft`)

| 단계 | 무엇을 | 실패하면 |
|---|---|---|
| 동의 게이트 | 환자의 **현재** 동의에서 `ai_drafting` (스냅샷이 아니라 실제 행) | `abstained(consent_scope_missing)` — 세그먼트를 열지도, DEK 를 풀지도 않는다 |
| 세그먼트 | `repo.segments.replay` 500행씩 → 세션 DEK 로 복호화 (`SegmentView`) | 복호화 실패는 핸들러 예외(재시도 → DLQ): 검증할 수 없는 세그먼트 위에 초안을 쓰지 않는다 |
| 프로바이더 | `CHARTWIRE_NOTE_PROVIDER` 가 고르는 하나(`extractive` 기본, `anthropic` 은 모델이 설정된 경우만) | `ProviderError` → `abstained(provider_error)`. **대체 프로바이더 없음** (출처가 바뀌면 안 된다) |
| 스키마 | `parse_draft` — 실패 시 프로바이더를 한 번 더 호출 | 두 번째 실패 → `abstained(schema)`. 거부된 출력도 세션 DEK 로 암호화해 보관 |
| 검증·정책 | `verify` → `decide` (§3, §4) | — |
| 저장 | `notes`(version = 이전 + 1, `raw_draft_enc`, `prompt_hash`, coverage, 카운트) + `note_statements`(`text_enc`, evidence = `{seq, quote_hash, start, end, method}`, verdict/사유) | 같은 트랜잭션. 폴러 아래서는 `processed_events` 와 함께 커밋 |
| 뒤처리 | `sessions.state='drafted'`, 감사 `note.drafted`/`session.drafted`, PUBLISH `note.status`, 메트릭 §9.6 | Redis 발행 실패는 경고만 (캐시일 뿐) |

파기된 세션(`purged` 또는 DEK 없음)은 노트를 만들지 않고 건너뛴다(`NoteOutcome.skipped`). `evidence` JSONB 에는 인용문이
없다(해시만). REST 가 보여 주는 인용문은 `raw_draft_enc` 를 세션 DEK 로 다시 열어 `(section, ordinal)` 로 맞춘 것이다.

### 8.2 검토 (REST `decision`, `assessment`)

- 문장마다 `accept | edit | reject`. `edit` 의 텍스트는 세션 DEK 아래 `edited_text_enc` 에 들어가고 원문은 남는다.
- `PUT /notes/{id}/assessment` 는 `note_assessments` 의 **유일한** 쓰기 경로다. 프로바이더·핸들러는 이 테이블을 쓸 코드가 없고,
  출력 스키마에도 그런 필드가 없다(§2). 평가는 **테넌트 기록 키**로 암호화된다 — 파기와 무관하게 남아야 하는 값이기 때문이다.
- auditor 는 노트를 읽되 `text`/`edited_text`/인용문/평가가 모두 `null` 이다(메타데이터만). staff/admin 은 REST 403 이고,
  잊더라도 RLS `role_gate` 가 0행을 돌려준다.

### 8.3 서명 (`sign`, ADR-0004)

서명 조건: 평가가 있고, `unsupported` 문장이 전부 `reject` 또는 `edit` 되었을 것. 조건이 빠지면 409 (`CW-4092`, `CW-4093`).

서명 순간 만들어지는 `signed_content_enc` 는 **자기완결적** 문서다: 수락·수정된 S/O/P 문장, 각 문장의 **verbatim 인용**(seq·오프셋 포함),
평가, 임상의 id, 서명 시각, 세션·환자 id, 프로바이더/검증기 버전. 이것을 세션 DEK 가 아니라 **기록 키**로 암호화하고
`legal_hold='medical_record'`, `retention_until = signed_at + 10년`, `sessions.state='signed'` 를 같은 트랜잭션에서 쓴다.

그 뒤 동의가 철회되어 세션이 파기되면 세그먼트·`raw_draft_enc`·`note_statements.text_enc` 는 DEK 와 함께 사라지지만,
`GET /notes/{id}` 는 서명 노트를 `signed_content_enc` 에서만 렌더링하므로 **그대로 읽힌다** — 통합 테스트
`test_signed_note_survives_purge_of_segments_and_session_dek` 가 이를 고정한다. 반대로 서명되지 않은 초안은 파기 뒤 404 다.

수정된 `unsupported` 문장은 기록에 들어가되, 실제로 매치된 근거만 붙는다(지어낸 인용문이 의료 기록에 박히지 않도록).
거부된 문장은 기록에 없다. `original_text` 로 수정 전 문장은 남긴다.
