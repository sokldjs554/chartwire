# WP-D handoff — Phase 0 (notes: 스키마 · 검증기 · 프로바이더)

상태: **진행 중** (계획 단계, 2026-09-03). 중단되면 아래 순서대로 이어서 작업하세요.

## 계획 / 진행 체크리스트

- [ ] `notes/schema.py` — Evidence/Statement/NoteDraftOut(extra=forbid), SegmentView, DraftContext, RawDraft, NoteProvider, VerifiedStatement, VerifiedDraft, NoteStatus, VerdictReason
- [ ] `notes/normalize.py` — NFC, 구두점 제거, 공백 제거, `normalize_numbers_ko`, 숫자+단위 토크나이저
- [ ] `notes/lexicon_drugs.py`, `notes/lexicon_diagnoses.py`
- [ ] `notes/verifier.py` — 8 규칙 순서대로, mypy --strict
- [ ] `notes/policy.py` — decide()
- [ ] `notes/extractive.py` — ExtractiveProvider
- [ ] `notes/providers/{base,recorded,mutation,paraphrase,anthropic}.py`
- [ ] `tests/fixtures/anthropic/*.json` (≥5)
- [ ] `tests/unit/test_notes_*.py`
- [ ] `scripts/eval_anthropic.py`
- [ ] `docs/grounding.md`, `docs/adr/0003-no-llm-in-safety-path-no-verdict-fields.md`
- [ ] ruff/mypy clean, 최종 테스트 수 기록
