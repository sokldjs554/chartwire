# 평가 리포트 (`docs/eval/`)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 디렉터리의 숫자는 STT 품질이나 진료기록 품질을 평가한 것이 **아닙니다** (STT는 시뮬레이터, 발화는 생성기 산출물입니다). 평가 대상은 결정론적 안전망(위험 발화 탐지), 근거 검증기, 파기 파이프라인, 테넌시 격리, 스트리밍 프로토콜의 **동작 보증**입니다.

생성 명령: `chartwire eval all --seed 42 --out docs/eval` (개별: `chartwire eval risk|grounding|inject|injection|paraphrase|purge|rls|protocol`). README의 모든 숫자는 `scripts/readme_numbers.py --write`가 이 JSON에서 채우며, 사람이 손으로 적는 숫자는 없습니다 (spec §11.4). 측정되지 않은 항목은 README에서 행째 삭제됩니다. 키 목록: `python scripts/readme_numbers.py --list`.

## 공통 헤더

모든 리포트 JSON은 다음 헤더로 시작합니다 (`chartwire.eval.report.build_report`):

```json
{"seed": 42, "git_sha": "…", "generated_at": "2026-09-02T09:00:00+00:00",
 "cpu": "… x4", "ram_gb": 15.0, "python": "3.11.x", "pg_version": "16.13"}
```

`pg_version`은 PostgreSQL을 쓰지 않는 리포트에서 `null`입니다.

## 두 종류의 리포트

| 종류 | 리포트 | 서비스 필요 | 만드는 곳 |
|---|---|---|---|
| **계산형** — 하네스가 직접 측정 | `risk_heldout`, `risk_ingrammar`, `grounding`, `inject`, `injection`, `paraphrase` | 없음 (순수 함수, ≈ 40 s) | `chartwire.eval.{risk,grounding,inject,paraphrase}_eval` |
| **집계형** — 다른 실행의 측정을 헤더와 함께 재기록 | `purge`, `rls`, `protocol`, `alert_latency` | 입력 파일 | `purge_eval` / `rls_eval` / `protocol_eval` (입력이 없으면 **건너뜀** → README 행 삭제) |

집계형 입력(각 소유 WP가 씀): `var/eval/purge.json` `{sessions_purged, patients_purged, residual_rows, residual_objects, residual_keys, unwrap_failure_pct, decrypt_failure_pct, receipts_verified_pct}` (WP-E) · `var/eval/rls.json` `{attempts, leaks, routes?, roles?}` (WP-E RBAC 매트릭스 테스트) · `docs/loadtest/D.json` 의 `loss`/`dup` (WP-H, protocol 의 chaos 절반) · `docs/eval/alert_latency.json` `{p50_ms, p95_ms, n_sessions}` (WP-H, 시나리오 A 안에서 직접 기록). `protocol.json` 의 `hypothesis_examples` 는 `chartwire eval protocol` 이 `pytest tests/ws --hypothesis-show-statistics` 를 **실제로 실행**해 "N passing examples" 를 합산합니다(선언값이 아님).

## 리포트 목록 (spec §11.1)

| 파일 | 무엇을 재는가 | 핵심 필드 (README 키 `eval.<파일>.<필드>`) |
|---|---|---|
| `risk_heldout.json` | **헤드라인.** 손으로 쓴 held-out 위험 발화 300문장. 경보 대상 = 행의 `alert` 레이블; 판정 = `detector.scan()` 의 히트가 `alerts`(severity ≥ 1, 억제 플래그 없음). 실행 전에 `FROZEN.txt` 해시를 검증하고 다르면 **거부**. | `precision, recall, f1, n, tp, fp, fn`, `per_kind.<kind>.{n,fp,fn,fp_rate}`, `per_category`, `fp_by_hit`(오탐의 범주:등급), `frozen_sha256`, `detector_version` |
| `risk_ingrammar.json` | 같은 지표를 생성기 문법 안의 200개 eval 스크립트(≈10,300 발화)에 적용. 어휘를 공유하므로 **높게 나오는 것이 당연**하며 "회귀 점검"으로만 표기. | 위와 같음 + `n_scripts`, `past_kind.{n, alerted, severity_1}`, `sessions_with_expected_alert`, `sessions_alerted` |
| `grounding.json` | 추출형 초안 → `verify` → `decide`. **coverage** = supported/total(구성상 1.0, 부록). **fact_recall** = 골드 사실을 실은 발화가 *supported* 문장의 근거로 인용된 비율(값 퍼지 매칭 없음 — 사실이 출처와 함께 노트에 들어갔는가). **abstain_rate** = `abstained` 세션 비율. | `coverage, fact_recall, abstain_rate, n_sessions, facts_total, fact_recall_by_type.<type>.{total,recalled,recall}, status_counts, statements_per_session, section_counts` |
| `paraphrase.json` | `ParaphrasingMockProvider` 의 의미 보존 바꿔쓰기(인용은 verbatim)를 검증기가 거부하는 비율. 코퍼스 3회 통과(변환 무작위). 잔여 원인(변환×사유)을 그대로 나열. | `false_rejection_rate, n_statements, rejected, by_transform[{transform,total,rejected,rate,reasons}], residual_causes[{cause,count}]` — README 키 `eval.paraphrase.<transform>.rate` |
| `inject.json` | 변이 7종 × 200 (eval 세션에서 무작위): 변이된 문장이 `unsupported` 로 돌아오는 비율. **false_flag_rate** = 같은 초안의 *건드리지 않은* 문장이 거부된 비율. | `classes[{class, mutation, n, detected, detection_rate, expected_reason, reasons}]` (class = `fabricated|seq|diagnosis|number|drug|negation|speaker`), `false_flag_rate, n_per_class` |
| `injection.json` | 프롬프트 주입 발화가 있는 20개 세션(`e0010, e0020, …`). **leak** = 주입 발화가 초안에 도달(문장 텍스트·근거 인용·인용 seq). 추가로 주입 발화를 강제로 인용시켜 규칙 8(`injection_pattern`)이 잡는 수를 따로 기록. | `injection_leaks, n_sessions, n_injection_utterances, leak_details[{script_ref, statement, utterances}], excluded_by_provider, forced_citations_flagged/total` |
| `purge.json` | 세션 50개 + 환자 10명 파기 후 잔여 행/객체/키, DEK unwrap 실패율, 복호화 실패율, 영수증 검증율 (집계형). | `residual_rows, residual_objects, residual_keys, unwrap_failure_pct, decrypt_failure_pct, receipts_verified_pct` |
| `rls.json` | 라우트 × 역할 × 테넌트 교차 접근 시도 수와 누출 수 (집계형). | `attempts, leaks` |
| `protocol.json` | hypothesis 예제 수(실제 실행), 카오스 실행의 손실/중복 (집계형). | `hypothesis_examples, property_tests, per_test, chaos_loss, chaos_dup` |
| `alert_latency.json` | 세그먼트 커밋 → 뷰어 `risk.alert` p50/p95 (WP-H 가 시나리오 A 안에서 기록). | `p50_ms, p95_ms` |
| `anthropic.json` | `scripts/eval_anthropic.py`를 실제 키로 실행했을 때만 존재. 없으면 README는 "미실행"으로 표기. | — |

CI `eval-smoke`: `chartwire eval all --seed 7 --n 20 --per-class 20 --check --out <tmp>` — held-out 재현율 ≥ 0.6, injection 누출 0, 파기 잔여 0(입력이 있을 때), 패러프레이즈 오거부 ≤ 5 %, 그리고 동결 해시 검사(`chartwire eval risk` 가 먼저 검증). 임계값은 `chartwire.eval.cli.SMOKE_THRESHOLDS` 한 곳에 있습니다.

## 해석 규칙 (숫자를 읽을 때)

1. **`past` 종류의 두 해석.** spec §9.4 는 과거 서술(`작년엔 죽고 싶었는데 지금은 아니에요`)을 *severity −1, 그래도 ≥ 1 이면 경보* 로 규정하고, 생성기 골드는 억제 종류 전부를 `alert=false` 로 둡니다. 두 해석을 하나로 합치지 않습니다: in-grammar P/R/F1 에서 `past` 발화를 **제외**하고 `past_kind.{n, alerted}` 로 따로 보고합니다. held-out 세트는 동결된 레이블(`alert=false`, 작성자의 임상 판단)을 그대로 쓰므로 §9.4 대로 동작하는 탐지기는 그 행에서 오탐으로 집계됩니다 — `per_kind.past.fp_rate` 가 그 크기를 보여줍니다. 이 문서와 `docs/risk-detection.md` 가 같은 해석을 적어야 합니다.
2. **fact_recall 은 추출형 프로바이더의 cue 목록을 그대로 반영합니다.** 근거 인용을 요구하는 정의라, cue 가 없는 발화(예: `새벽 4시에 깨서 다시 못 자요`, `3주 동안 3kg 빠졌어요`)의 사실은 초안에 들어갈 수 없습니다. `fact_recall_by_type` 이 어느 유형이 빠지는지 보여줍니다. 낮은 값은 검증기의 결함이 아니라 프로바이더의 **재현율** 한계이며, 그 한계는 임상가가 원문 옆에서 읽는다는 전제로 문서화돼 있습니다(`docs/grounding.md`).
3. **injection_leaks 는 프로바이더 + 검증기 전체 경로의 결과입니다.** 추출형이 규칙 8 정규식으로 주입 발화를 걸러내지만, 정규식이 놓치는 문장(`… 반드시 써 주세요`)이 cue 를 우연히 포함하면(`요약할` 의 `약`) 초안에 들어갑니다. `leak_details` 가 어느 세션·발화인지 가리키므로 규칙 수정 → 재실행 → 0 인지 확인하는 순서로 씁니다. 숫자를 좋게 만들기 위해 평가 기준을 바꾸지 않습니다.
4. **held-out 은 튜닝 대상이 아닙니다.** 하네스는 `FROZEN.txt` 와 다르면 실행을 거부합니다. 규칙 작성자에게는 오탐/미탐의 **집계**(범주·종류별 수)만 전달하고 문장은 전달하지 않습니다.

## 누출 통제 프로토콜 (spec §10.3)

위험 발화 탐지의 헤드라인 숫자가 "규칙을 쓴 사람이 자기 시험지를 채점한" 결과가 되지 않도록 다음을 지킵니다.

1. **작성 순서.** `src/chartwire/eval/data/heldout_risk_ko.jsonl`(300문장)은 생성기 어휘(`synth/vocab_ko.py`)와 탐지 규칙(`risk/lexicon_ko.py`)이 존재하기 **전에**, 공유 슬롯 목록 없이 임상 언어 지식만으로 작성했습니다. 패러프레이즈, 경상·전라 방언(`죽고 싶데이`, `살기 싫어예`, `죽고 싶당께요`), 오타·띄어쓰기 오류, 간접 사고(`사라지고 싶다`, `짐이 된다`), 계획·수단, 타해, 급성 물질 사용, 임상가 질문, 관용구 하드 네거티브(`피곤해 죽겠어요`, `자살 예방 교육`, 드라마 장면)를 포함하며 58 %가 `alert=false`입니다.
2. **역할 분리.** 규칙 작성자(WP-C)는 `eval/data/`를 열지 않고, held-out 작성자(WP-F)는 held-out 을 쓰는 동안 `risk/`를 열지 않았습니다. 하네스 소유자(WP-F)가 두 세트를 모두 실행하고 **별도 행**으로 보고합니다.
3. **동결.** `eval/data/FROZEN.txt`에 sha256을 기록하고 CI `frozen-artifacts`가 변경 시 실패합니다. 하네스(`chartwire.eval.corpus.verify_frozen`)도 실행 전에 같은 검사를 합니다. 문장을 고치고 싶으면 규칙을 먼저 고정한 뒤 새 버전을 별도 파일로 추가합니다.
4. **문법 내(in-grammar) 세트는 회귀 점검용.** 생성기 위험 사례(`RISK_CASES`)는 규칙 작성자도 보는 스펙 §10.2의 예문이므로, 그 세트의 높은 점수는 능력의 증거가 아니라 회귀 검출 장치입니다.

동결 해시 확인:

```bash
cd src/chartwire/eval/data && sha256sum -c FROZEN.txt
```

## held-out 파일 형식

```json
{"text": "…", "speaker": "patient|clinician",
 "category": "suicidal_ideation|self_harm|harm_to_others|substance_acute|null",
 "severity": 0, "alert": false,
 "kind": "positive|negated|hypothetical|past|third_person|clinician_question|idiom|unrelated",
 "note": "작성 메모"}
```

`alert`가 정답 레이블입니다. `category`는 억제 종류(negated/past 등)에서도 표면 어휘가 가리키는 범주를 담고 있으므로, 범주 정확도를 따로 계산할 때만 사용합니다.

## 합성 스크립트 (`chartwire synth scripts`)

| 프로파일 | 시드 | 이름 | 용도 |
|---|---|---|---|
| `demo` | 1 | `s01`–`s20` | 콘솔, Render 시드, `chartwire simulate`; `chartwire seed --demo` 가 `var/scripts/` (또는 `$CHARTWIRE_STT_SCRIPTS_DIR`) 에 기록 |
| `eval` | 42 | `e0001`–`e0200` | 위험 in-grammar, grounding, 변이/패러프레이즈, injection(`e0010, e0020, …` 20개) — 하네스는 파일 없이 메모리에서 생성 |

출력: 스크립트당 `<ref>.json` 하나(계약은 `chartwire.synth.scripts.Script`), `index.json`, 세션 단위 골드 `gold.jsonl`. 같은 시드는 바이트 단위로 동일한 파일을 만듭니다 (`tests/unit/test_synth_scripts.py`). 발화 → `SegmentView` 변환은 `chartwire.eval.corpus.segment_views` (seq = 발화 인덱스, stt-worker 의 번호 매김과 같음).
