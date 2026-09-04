# 품질 패스 핸드오프 (WP-F Phase 1 §5 요청 1–4)

> 상태: **중단됨 — `docs/dev/handoff/quality2.md` 가 이어받아 완료했다** (체크리스트 전 항목 + WP-F 요청 5 포함). 이 문서는 그 실행의 기준선(§1)만 남긴다. 작업 규칙: `src/chartwire/eval/data/`·`docs/eval/*.json` 은 열지 않는다(누출 통제). 단위 테스트만 실행(`tests/unit`). git 상태는 바꾸지 않는다.

## 0. 체크리스트

- [ ] 1. 위험 탐지 재현율 — `risk/lexicon_ko.py` 어간 확장(방언·오타·간접 사고·수단·자해·타해·급성 물질)
- [ ] 1. `risk/scope.py` — `past` 규칙 재정의(현재 부정 동반 → 억제, 부정 없음 → severity−1), 부정·가정·질문·3인칭 표지 보강
- [ ] 1. 관용구/하드 네거티브 목록 보강(정밀도 방어)
- [ ] 1. `tests/unit/test_risk_*.py` — 새 어간 가족마다 + past 규칙 테스트
- [ ] 1. `docs/risk-detection.md` §3·§9 + `docs/eval/README.md` 해석 규칙 1 에 같은 문장
- [ ] 1. in-grammar 회귀(P/R 1.0 유지, held-out 미열람) 확인
- [ ] 2. `notes/verifier.py` `INJECTION_RE` diff 확인 + §9.5 문장 6개 단위 테스트
- [ ] 2. `chartwire eval injection --seed 42` → leaks == 0
- [ ] 3. `notes/extractive.py` `SYMPTOM_CUES` (`됐어요` 포함) + 테스트
- [ ] 3. `chartwire eval grounding --seed 42` → fact_recall ≥ 0.7
- [ ] 4. `db/repo/segments.py::replay` 상한 확인(이미 적용됨) — 통합 테스트는 통합자가 재실행
- [ ] ruff check / ruff format / mypy(risk, verifier) 클린
- [ ] `pytest tests/unit -q` green
- [ ] `chartwire eval risk --seed 7` 1회 실행 → 집계 P/R/F1 만 기록
- [ ] 이 문서 완성(전/후 집계)

## 1. 전(前) 집계 (wp-f-phase1.md §3, seed 42)

| 리포트 | 전 |
|---|---|
| risk_heldout | n=300 P 0.699 / R 0.468 / F1 0.560 (FN 66/124; FP 25: past 11, idiom 8, hypothetical 3, clinician_question 2, third_person 1) |
| grounding | coverage 1.00, fact_recall 0.23 |
| injection | leaks 4, rule-8 forced 6/33 |
