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
| **실행형** — 하네스가 실제 파이프라인을 돌려 측정 | `purge`, `rls` | PostgreSQL + Redis (`CHARTWIRE_TEST_DB` / `CHARTWIRE_TEST_REDIS_DB`) | `chartwire eval purge --run` / `chartwire eval rls --run` |
| **집계형** — 다른 실행의 측정을 헤더와 함께 재기록 | `protocol`, `alert_latency` (+ `--run` 없이 호출한 `purge`/`rls`) | 입력 파일 | `protocol_eval` (입력이 없으면 **건너뜀** → README 행 삭제) |

**실행형**은 `var/eval/{purge,rls}.json` 을 스스로 쓰고, 이어지는 집계 단계가 그 파일을 헤더와 함께 `docs/eval/` 로 옮깁니다 — 입력이 없으면 예전처럼 건너뜁니다. 두 실행은 통합 스위트의 픽스처(`tests/conftest.Factories`, `tests/integration/api_support`, `tests/integration/test_rbac_matrix`)를 그대로 재사용하므로(`chartwire.eval.harness_env`) 평가와 테스트가 같은 행·같은 라우트 표를 봅니다. 평가 DB 는 `base → head` 로 다시 만들고 Redis 는 자기 인덱스만 `FLUSHDB` 합니다 — 개발 DB 는 건드리지 않습니다.

- `purge --run`: 환자 10명 × 세션 5개(오브젝트 스토어의 오디오 청크, 암호화 세그먼트, `segment_search`, 위험 이벤트, 초안 노트, 환자당 **서명 노트** 1개)를 심고 **세션 50개 + 환자 10명**을 실제 파기 파이프라인 + `purge.verify` 로 돌린 뒤, 독립적인 스윕으로 잔여 행/객체/Redis 키를 셉니다. `signed_notes_surviving` 은 서명 노트가 남아 **자기 기록 키로 여전히 복호화되는지**까지 확인합니다(§0.6).
- `rls --run`: 실제 `create_app()` 의 등록 라우트 전부 × 역할 5종 + 익명, 그리고 **테넌트 B 토큰으로 테넌트 A 의 실제 id** 를 호출하는 교차 접근. 2xx 가 하나라도 나오면 `leaks` 로 셉니다(`violations` 에 어느 라우트·역할인지 기록).

집계형 입력: `docs/loadtest/D.json` 의 `loss`/`dup` (WP-H, protocol 의 chaos 절반) · `docs/eval/alert_latency.json` `{p50_ms, p95_ms, n_sessions}` (WP-H, 시나리오 A 안에서 직접 기록). `protocol.json` 의 `hypothesis_examples` 는 `chartwire eval protocol` 이 `pytest tests/ws --hypothesis-show-statistics` 를 **실제로 실행**해 "N passing examples" 를 합산합니다(선언값이 아님).

## 리포트 목록 (spec §11.1)

| 파일 | 무엇을 재는가 | 핵심 필드 (README 키 `eval.<파일>.<필드>`) |
|---|---|---|
| `risk_heldout.json` | **헤드라인.** 손으로 쓴 held-out 위험 발화 300문장. 경보 대상 = 행의 `alert` 레이블; 판정 = `detector.scan()` 의 히트가 `alerts`(severity ≥ 1, 억제 플래그 없음). 실행 전에 `FROZEN.txt` 해시를 검증하고 다르면 **거부**. | `precision, recall, f1, n, tp, fp, fn`, `per_kind.<kind>.{n,fp,fn,fp_rate}`, `per_category`, `fp_by_hit`(오탐의 범주:등급), `frozen_sha256`, `detector_version` |
| `risk_ingrammar.json` | 같은 지표를 생성기 문법 안의 200개 eval 스크립트(≈10,700 발화)에 적용. 어휘를 공유하므로 **높게 나오는 것이 당연**하며 "회귀 점검"으로만 표기. | 위와 같음 + `n_scripts`, `past_kind.{n, alerted, severity_1}`, `sessions_with_expected_alert`, `sessions_alerted` |
| `grounding.json` | 추출형 초안 → `verify` → `decide`. **coverage** = supported/total(구성상 1.0, 부록). **fact_recall** = 골드 사실을 실은 발화가 *supported* 문장의 근거로 인용된 비율(값 퍼지 매칭 없음 — 사실이 출처와 함께 노트에 들어갔는가). **abstain_rate** = `abstained` 세션 비율. | `coverage, fact_recall, abstain_rate, n_sessions, facts_total, fact_recall_by_type.<type>.{total,recalled,recall}, status_counts, statements_per_session, section_counts` |
| `paraphrase.json` | `ParaphrasingMockProvider` 의 의미 보존 바꿔쓰기(인용은 verbatim)를 검증기가 거부하는 비율. 코퍼스 3회 통과(변환 무작위). 잔여 원인(변환×사유)을 그대로 나열. | `false_rejection_rate, n_statements, rejected, by_transform[{transform,total,rejected,rate,reasons}], residual_causes[{cause,count}]` — README 키 `eval.paraphrase.<transform>.rate` |
| `inject.json` | 변이 7종 × 200 (eval 세션에서 무작위): 변이된 문장이 `unsupported` 로 돌아오는 비율. **false_flag_rate** = 같은 초안의 *건드리지 않은* 문장이 거부된 비율. | `classes[{class, mutation, n, detected, detection_rate, expected_reason, reasons}]` (class = `fabricated|seq|diagnosis|number|drug|negation|speaker`), `false_flag_rate, n_per_class` |
| `injection.json` | 프롬프트 주입 발화가 있는 20개 세션(`e0010, e0020, …`). **leak** = 주입 발화가 초안에 도달(문장 텍스트·근거 인용·인용 seq). 추가로 주입 발화를 강제로 인용시켜 규칙 8(`injection_pattern`)이 잡는 수를 따로 기록. | `injection_leaks, n_sessions, n_injection_utterances, leak_details[{script_ref, statement, utterances}], excluded_by_provider, forced_citations_flagged/total` |
| `purge.json` | 세션 50개 + 환자 10명을 **실제로 파기**한 뒤의 잔여 행/객체/키, DEK unwrap 실패율, 복호화 실패율, 영수증 검증율, 서명 노트 생존 수 (실행형). | `sessions_purged, patients_purged, jobs, residual_rows, residual_objects, residual_keys, unwrap_failure_pct, decrypt_failure_pct, receipts_verified_pct, signed_notes_surviving/expected` |
| `rls.json` | 라우트 × 역할 × 테넌트 교차 접근을 **실제로 시도**한 수와 누출 수 (실행형). | `attempts, leaks, routes, roles, cross_tenant_attempts, role_denied, role_allowed, anonymous_rejected, violations` |
| `protocol.json` | hypothesis 예제 수(실제 실행), 카오스 실행의 손실/중복 (집계형). | `hypothesis_examples, property_tests, per_test, chaos_loss, chaos_dup` |
| `alert_latency.json` | 세그먼트 커밋 → 뷰어 `risk.alert` p50/p95 (WP-H 가 시나리오 A 안에서 기록). | `p50_ms, p95_ms` |
| `anthropic.json` | `scripts/eval_anthropic.py`를 실제 키로 실행했을 때만 존재. 없으면 README는 "미실행"으로 표기. | — |

CI `eval-smoke`: `chartwire eval all --seed 7 --n 20 --per-class 20 --check --out <tmp>` — held-out 재현율 ≥ 0.55·정밀도 ≥ 0.70, injection 누출 0, 파기 잔여 0·RLS 누출 0(입력이 있을 때), 패러프레이즈 오거부 ≤ 5 %, 그리고 동결 해시 검사(`chartwire eval risk` 가 먼저 검증). 임계값은 `chartwire.eval.cli.SMOKE_THRESHOLDS` 한 곳에 있습니다. **held-out 임계값은 목표치(§11.1 의 0.6)가 아니라 지금 코드의 측정값(0.573 / 0.763)에서 내려온 바닥값입니다** — held-out 은 튜닝 대상이 아니므로 게이트를 측정값에 맞추고, 더 좋은 측정이 나오면 그때 올립니다. 숫자를 맞추려고 임계값을 낮추지 않습니다.

## 해석 규칙 (숫자를 읽을 때)

1. **`past` 규칙.** **과거 사고(思考)의 해석**: 히트가 과거 표지(`예전 / 작년 / 그때 / 했었 / 었는데 …`)와 함께 나타나고 **현재 부정**(`지금은 아니 / 요즘은 없 / 이제는 안 …`)이 뒤따르면 **억제**하고, 현재 부정이 없으면 **severity 를 1 낮춰 경보**합니다. (`docs/risk-detection.md` §9 와 같은 문장.) `작년엔 죽고 싶었는데 지금은 아니에요` 는 억제되고 `작년부터 죽고 싶었어요` 는 한 등급 낮춰 경보합니다. spec §9.4 원문은 두 경우를 구분하지 않았고, 그대로 두면 명시적으로 부인한 과거 사고까지 경보가 되어 held-out 의 `past` 행(`alert=false`)이 대부분 오탐이 됐습니다. in-grammar P/R/F1 에서 `past` 발화를 **제외**하고 `past_kind.{n, alerted}` 로 따로 보고하는 것은 그대로이며, 규칙을 나눈 뒤 `past_kind.alerted` 는 0/31 로 생성기 골드와 일치합니다. 남은 차이는 `per_kind.past.fp_rate` 가 보여줍니다.
2. **fact_recall 은 추출형 프로바이더의 cue 목록과 섹션당 12문장 상한을 함께 반영합니다.** 근거 인용을 요구하는 정의라, cue 가 없는 발화의 사실은 초안에 들어갈 수 없습니다. §10.1 의 사실 발화 6종(`새벽 N시에 깨서`, `하루 N시간`, `N주 N kg 빠졌`, `소주 N병`, `{drug} {dose}mg 먹고`, `N주 됐어요 / N주 전부터요`)은 모두 cue 를 갖게 했고, cue 는 §10.1 의 사실 유형별 **가족(family)** 으로 나뉩니다. 한 세션에서 이른 단계(수면·기간)의 발화가 늦은 단계(약물·음주)보다 훨씬 많이 나오기 때문에, 상한 12를 seq 순으로 그냥 채우면 약물·음주 사실이 통째로 빠집니다 — 그래서 **가족마다 한 문장씩 먼저 뽑고** 남는 자리를 seq 순으로 채웁니다(출력 순서는 §9.2 대로 seq). 같은 발화가 반복되면 한 번만 싣습니다. 그 결과 `fact_recall_by_type` 은 어느 유형도 0 이 아니게 되지만, **duration 사실이 골드의 절반가량**을 차지하고 한 세션에 같은 문장이 7번까지 반복되므로 총합은 duration 에 좌우됩니다 — 유형별 표가 정직한 읽기입니다. 낮은 값은 검증기의 결함이 아니라 프로바이더의 **재현율** 한계입니다(`docs/grounding.md`). 주호소가 대화를 바꾸는 생성기(`vocab_ko.ChiefComplaint`)가 들어오면서 가족이 늘었고(강박의 확인·씻기 `compulsion`, 적응/스트레스의 신체 증상 `somatic`, 불안 가족에 공황·긴장·걱정·답답·공포, 집중 가족에 실수·딴생각·깜빡·잊어버·마감), 같은 가족 안에서는 **숫자가 있는 문장(10mg · 5시간 · 3kg · 일주일에 5일)을 막연한 문장보다 먼저** 뽑습니다 — 상한 12가 물 때 노트에 남아야 하는 것은 측정 가능한 사실이고, 출력 순서는 여전히 seq 입니다. 이 규칙이 없으면 새 사실 유형(`alcohol_days`, `weight_gain`, `awakenings` …)이 같은 가족의 이른 발화에 밀려 0 이 됩니다.
3. **injection_leaks 는 프로바이더 + 검증기 전체 경로의 결과이고, 자기 테스트 세트에 맞춰져 있습니다** — 규칙 8 정규식을 §9.5 6문장의 표면형까지 넓힌 뒤 0 이 됐고, **held-out 주입 세트는 없습니다**(위험 탐지와 대조적). 회귀 점검으로만 읽으세요(`docs/limitations.md` §1).
   같은 이유로 `inject.json` 의 검출률도 구성상 높습니다: 변이 생성기가 검증기의 사전·정규식을 그대로 import 합니다. 추출형이 규칙 8 정규식으로 주입 발화를 걸러내지만, 정규식이 놓치는 문장(`… 반드시 써 주세요`)이 cue 를 우연히 포함하면(`요약할` 의 `약`) 초안에 들어갑니다 — §9.5 의 6문장을 모두 덮도록 정규식을 넓힌 뒤 0 이 됐고, `tests/unit/test_notes_verifier.py` 가 6문장을 개별로 고정합니다. `leak_details` 가 어느 세션·발화인지 가리키므로 규칙 수정 → 재실행 → 0 인지 확인하는 순서로 씁니다. 숫자를 좋게 만들기 위해 평가 기준을 바꾸지 않습니다.
4. **held-out 은 튜닝 대상이 아닙니다.** 하네스는 `FROZEN.txt` 와 다르면 실행을 거부합니다. 규칙 작성자에게는 오탐/미탐의 **집계**(범주·종류별 수)만 전달하고 문장은 전달하지 않습니다.
5. **held-out P/R 은 "안전망" 지표이지 분류기 성적표가 아닙니다.** 목표는 재현율 ≥ 0.6 **그리고** 정밀도 ≥ 0.8 이었고 **둘 다 미달**입니다(값은 `risk_heldout.json`, README 표 ③). 재현율이 1 이 아니라는 것은 **경보가 없다고 위험이 없다는 뜻이 아니라는** 뜻입니다 — 이 기능은 임상의를 대체하지 않고, 놓치기 쉬운 발화를 초 단위로 올려 줄 뿐입니다. `per_kind` 표를 함께 읽으세요: 오탐이 남은 가장 큰 갈래는 `past`(현재 부인이 없는 과거 사고)이고, 그것을 통째로 억제하면 정밀도는 오르지만 안전망은 얇아집니다. 미탐은 전부 `positive` 종류이며 범주별 재현율은 `per_category` 에 있습니다 — 자살사고 0.61(미탐 25), 자해 0.67, 타해 0.50, 급성물질 0.38. **총합 재현율이 헤드라인 범주(자살사고)보다 낮은 것은 급성 물질·타해가 끌어내리기 때문**이므로 총합만 인용하면 자살사고 성능을 실제보다 나쁘게 읽게 됩니다(README §8 의 범주별 행). 범주별 **정밀도**는 오탐을 발화의 레이블로 귀속시키므로 (발화한 히트의 범주가 아님) `fp_by_hit` 과 함께 읽어야 합니다. **CI `eval-smoke` 의 임계값(`SMOKE_THRESHOLDS`)은 이 측정값에서 내려온 바닥이며 목표가 아닙니다** — 더 좋은 측정이 나올 때만 올립니다.
6. **`purge.json` / `rls.json` 은 이제 집계가 아니라 실행 결과입니다.** `chartwire eval purge --run` 은 평가 DB 를 새로 만들고 환자·세션·오브젝트·Redis 핫 상태·서명 노트를 심은 뒤 실제 파기 파이프라인을 돌리고, **독립 스윕**으로 잔여 행·오브젝트·키를 다시 셉니다. 그래서 읽는 방향이 뒤집혀 있습니다: **잔여 3종은 0이어야 하고, unwrap·복호화 실패율은 100 % 여야 정상**입니다(파기된 DEK 로는 열리지 않아야 하므로). `signed_notes_surviving` 은 그 반대 방향의 안전장치입니다 — 서명 노트는 기록 키로 **열려야** 합니다. `chartwire eval rls --run` 은 실제 `create_app()` 의 라우트 × 역할 + 익명, 그리고 **테넌트 B 토큰으로 테넌트 A 의 실제 id** 를 호출합니다. `attempts` 는 시도 수일 뿐이고 의미 있는 값은 `leaks`(0이어야 함)와 `cross_tenant_attempts` 입니다. `--run` 없이 부르면 예전처럼 파일 집계만 합니다.
7. **`alert_latency.json` 은 이 하네스의 산출물이 아닙니다.** 부하 시나리오 A 가 러너 안에서 기록한 값(세그먼트 커밋 → 뷰어 `risk.alert`, 클라이언트 시계)이라 조건이 다릅니다 — 같은 박스·같은 시계·A 의 부하 아래에서 잰 값이고, `n_sessions` 는 60 s 안에 위험 발화가 나온 **표본 세션 수**입니다. 마지막 A 실행(N=200)이 파일을 덮어씁니다.
8. **`protocol.json` 의 예제 수는 선언값이 아니라 실제 실행 통계입니다.** `chartwire eval protocol --run-tests` 가 `tests/ws` 를 서브프로세스로 돌려 hypothesis 통계를 파싱하고, `chaos_loss`/`chaos_dup` 은 `docs/loadtest/D.json` 에서 읽습니다. `eval all` 은 기본적으로 테스트를 돌리지 않으므로 그때는 이전 값이 남습니다.

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
| `demo` | 1 | `s01`–`s20` | 콘솔, Render 시드, `chartwire simulate`; `chartwire seed --demo` 가 `var/scripts/` (또는 `$CHARTWIRE_STT_SCRIPTS_DIR`) 에 기록. 주호소 8개를 순환 배정해(`s01` 초진 우울 … `s08` 강박) 20개가 모든 템플릿을 덮는다 |
| `eval` | 42 | `e0001`–`e0200` | 위험 in-grammar, grounding, 변이/패러프레이즈, injection(`e0010, e0020, …` 20개) — 하네스는 파일 없이 메모리에서 생성 |

출력: 스크립트당 `<ref>.json` 하나(계약은 `chartwire.synth.scripts.Script`), `index.json`, 세션 단위 골드 `gold.jsonl`. 같은 시드는 바이트 단위로 동일한 파일을 만듭니다 (`tests/unit/test_synth_scripts.py`). 발화 → `SegmentView` 변환은 `chartwire.eval.corpus.segment_views` (seq = 발화 인덱스, stt-worker 의 번호 매김과 같음).
