# 평가 리포트 (`docs/eval/`)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 디렉터리의 숫자는 STT 품질이나 진료기록 품질을 평가한 것이 **아닙니다** (STT는 시뮬레이터, 발화는 생성기 산출물입니다). 평가 대상은 결정론적 안전망(위험 발화 탐지), 근거 검증기, 파기 파이프라인, 테넌시 격리, 스트리밍 프로토콜의 **동작 보증**입니다.

생성 명령: `chartwire eval all --seed 42 --out docs/eval/` (개별: `chartwire eval risk|grounding|inject|paraphrase|purge`). README의 모든 숫자는 `scripts/readme_numbers.py --write`가 이 JSON에서 채우며, 사람이 손으로 적는 숫자는 없습니다 (spec §11.4). 측정되지 않은 항목은 README에서 행째 삭제됩니다.

## 공통 헤더

모든 리포트 JSON은 다음 헤더로 시작합니다 (`chartwire.eval.report.report_header`):

```json
{"seed": 42, "git_sha": "…", "generated_at": "2026-09-02T09:00:00+00:00",
 "cpu": "… x4", "ram_gb": 15.0, "python": "3.11.x", "pg_version": "16.13"}
```

`pg_version`은 PostgreSQL을 쓰지 않는 리포트에서 `null`입니다.

## 리포트 목록 (spec §11.1)

| 파일 | 무엇을 재는가 | README 키 (`scripts/readme_numbers.py::KEYS`) |
|---|---|---|
| `risk_heldout.json` | **헤드라인.** 손으로 쓴 held-out 위험 발화 300문장에 대한 정밀도/재현율/F1. 경보 대상 = `alert=true` (severity ≥ 1이고 억제 종류가 아님). 종류(kind)별 오탐률을 함께 기록. | `eval.risk_heldout.{precision,recall,f1,n}` |
| `risk_ingrammar.json` | 같은 지표를 생성기 문법 안의 200개 eval 스크립트에 적용. 어휘를 공유하므로 **높게 나오는 것이 당연**하며 "회귀 점검"으로만 표기. | `eval.risk_ingrammar.{precision,recall,f1}` |
| `alert_latency.json` | 세그먼트 커밋(`committed_at`) → 뷰어가 `risk.alert`를 받기까지 p50/p95 (경보가 예상되는 100개 세션, 부하 시나리오 A 안에서 측정). | `eval.alert_latency.{p50_ms,p95_ms}` |
| `grounding.json` | 추출형 초안의 근거 커버리지(구성상 1.0 — 부록으로만 언급), 골드 사실(fact) 재현율, abstain 비율. | `eval.grounding.{coverage,fact_recall,abstain_rate}` |
| `paraphrase.json` | `ParaphrasingMockProvider`가 만든 의미 보존 문장을 검증기가 잘못 거부하는 비율(변환 종류별). 잔여 원인을 목록으로 기록. | `eval.paraphrase.false_rejection_rate` |
| `inject.json` | 변이 종류 7개 × 200 세션에 대한 환각 검출률(fabricated/seq/diagnosis/number/drug/negation/speaker)과 오검출률. negation/speaker는 낮게 나와도 그대로 보고. | `eval.inject.<class>.detection_rate`, `eval.inject.false_flag_rate` |
| `injection.json` | 프롬프트 주입 발화가 포함된 20개 세션(`e0010, e0020, …`)에서 초안으로 새어 나간 지시문 수. | `eval.injection.leaks` |
| `purge.json` | 세션 50개 + 환자 10명 파기 후 잔여 행/객체/키, DEK unwrap 실패율, 복호화 실패율, 영수증 검증율. | `eval.purge.*` |
| `rls.json` | 라우트 × 역할 × 테넌트 교차 접근 시도 수와 누출 수. | `eval.rls.{attempts,leaks}` |
| `protocol.json` | hypothesis 예제 수, 카오스 실행의 손실/중복 수. | `eval.protocol.*` |
| `anthropic.json` | `scripts/eval_anthropic.py`를 실제 키로 실행했을 때만 존재. 없으면 README는 "미실행"으로 표기. | — |

CI `eval-smoke` (seed 7, 스크립트 20개): held-out 재현율 ≥ 0.6, injection 누출 0, 파기 잔여 0, 패러프레이즈 오거부 ≤ 5 %, 그리고 아래 동결 해시 검사.

## 누출 통제 프로토콜 (spec §10.3)

위험 발화 탐지의 헤드라인 숫자가 "규칙을 쓴 사람이 자기 시험지를 채점한" 결과가 되지 않도록 다음을 지킵니다.

1. **작성 순서.** `src/chartwire/eval/data/heldout_risk_ko.jsonl`(300문장)은 생성기 어휘(`synth/vocab_ko.py`)와 탐지 규칙(`risk/lexicon_ko.py`)이 존재하기 **전에**, 공유 슬롯 목록 없이 임상 언어 지식만으로 작성했습니다. 패러프레이즈, 경상·전라 방언(`죽고 싶데이`, `살기 싫어예`, `죽고 싶당께요`), 오타·띄어쓰기 오류, 간접 사고(`사라지고 싶다`, `짐이 된다`), 계획·수단, 타해, 급성 물질 사용, 임상가 질문, 관용구 하드 네거티브(`피곤해 죽겠어요`, `자살 예방 교육`, 드라마 장면)를 포함하며 58 %가 `alert=false`입니다.
2. **역할 분리.** 규칙 작성자(WP-C)는 `eval/data/`를 열지 않고, held-out 작성자(WP-F)는 `risk/`를 열지 않습니다. 하네스 소유자(WP-F)가 두 세트를 모두 실행하고 **별도 행**으로 보고합니다.
3. **동결.** `eval/data/FROZEN.txt`에 sha256을 기록하고 CI `frozen-artifacts`가 변경 시 실패합니다. 문장을 고치고 싶으면 규칙을 먼저 고정한 뒤 새 버전을 별도 파일로 추가합니다.
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
| `demo` | 1 | `s01`–`s20` | 콘솔, Render 시드, `chartwire simulate` |
| `eval` | 42 | `e0001`–`e0200` | 위험 in-grammar, grounding, 변이/패러프레이즈, injection(`e0010, e0020, …` 20개) |

출력: 스크립트당 `<ref>.json` 하나(계약은 `chartwire.synth.scripts.Script`), `index.json`, 세션 단위 골드 `gold.jsonl`. 같은 시드는 바이트 단위로 동일한 파일을 만듭니다 (`tests/unit/test_synth_scripts.py`).
