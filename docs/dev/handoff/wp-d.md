# WP-D handoff — Phase 0 (notes: 스키마 · 검증기 · 프로바이더)

상태: **진행 중** (재개, 2026-09-03). 중단되면 아래 체크리스트 순서대로 이어서 작업하세요.

## 계획 / 진행 체크리스트

- [x] `notes/schema.py` — 검토 완료 (§9.1 그대로, extra=forbid, 검증기 출력 모델 포함)
- [x] `notes/normalize.py` — 검토 완료 (NFC·구두점·공백, `normalize_numbers_ko`, `numeric_tokens`)
- [x] `notes/lexicon_drugs.py`(56), `notes/lexicon_diagnoses.py`(50) — 검토 완료
- [ ] `notes/verifier.py` — 8 규칙 순서대로, fail-closed, mypy --strict
- [ ] `notes/policy.py` — `decide()` 임계값 (0 / <3 & ≥0.85 / schema×2 / empty)
- [ ] `notes/extractive.py` — cue 정규식, `report_form`, ≤12/섹션, 근거 = 발화 전체
- [ ] `notes/providers/base.py` — `ProviderError`, RawDraft 헬퍼
- [ ] `notes/providers/recorded.py` — 픽스처 JSON → RawDraft
- [ ] `notes/providers/mutation.py` — 7 변이 클래스
- [ ] `notes/providers/paraphrase.py` — §9.2 변형 (인용 verbatim 유지)
- [ ] `notes/providers/anthropic.py` — import-guard, tool-use 구조화 출력, 30 s → ProviderError
- [ ] `tests/fixtures/anthropic/*.json` (≥5)
- [ ] `tests/unit/test_notes_*.py`
- [ ] `scripts/eval_anthropic.py` (키 없이 import 가능)
- [ ] `docs/grounding.md`, `docs/adr/0003-no-llm-in-safety-path-no-verdict-fields.md`
- [ ] ruff/mypy clean, 최종 테스트 수 기록, 변이 탐지표·거짓기각률 기록

## 설계 메모 (진행 중 결정)
- `policy.decide(verified, *, schema_failures=0, provider_error=False) -> Decision(status, reason)`:
  스펙의 `-> NoteStatus` 의 상위 집합(`decide(v).status`). `NoteOut.abstain_reason` 을 채우려면 사유가 함께 필요.
- 추출형 프로바이더는 규칙 8(injection) 정규식에 걸리는 발화를 **애초에 초안에 넣지 않는다** — "coverage 1.0 by construction" 을 실제로 성립시키기 위함.
