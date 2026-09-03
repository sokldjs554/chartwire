# WP-D — 재개 실행 계획 (2026-09-03)

이 파일은 중단 대비 체크리스트다. 본 핸드오프(모듈 맵·테스트·측정표·편차·요청)는 `docs/dev/handoff/wp-d.md` 에 있다.

범위: Phase 0 잔여분(스펙 §9 전체). `notes/service.py` 와 `worker/handlers/note_draft.py` 는 Phase 1 (D1) 항목이며 이 실행의 범위가 아니다.

- [x] 기존 코드·테스트 읽기, `pytest tests/unit/test_notes_*.py` (140 passed), `mypy --strict notes/verifier.py`, ruff — 모두 clean
- [x] `claude-api` 스킬 로드 후 `providers/anthropic.py` 점검
- [x] 바꿔쓰기 코퍼스에 부정을 흡수하지 않는 동의어 키 2개 추가 → 거짓기각률 표가 코퍼스 인공물이 아니게
- [x] `DEFAULT_MAX_TOKENS` 16000, 강제 tool_choice 의 모델 제약 문서화
- [x] 변이 탐지표 · 거짓기각률 표를 `wp-d.md` 에 기록 (pytest -s 출력 그대로)
- [x] `wp-d.md` 완성 (모듈 맵, 테스트, 편차, 요청)
- [x] 최종: pytest / ruff check / ruff format --check / mypy --strict
