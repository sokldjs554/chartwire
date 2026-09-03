# WP-F handoff — Phase 0 (합성 데이터 · 평가 스캐폴딩)

## 무엇을 만들었나

| 경로 | 역할 |
|---|---|
| `src/chartwire/eval/data/heldout_risk_ko.jsonl` | **첫 산출물.** 손으로 쓴 held-out 위험 발화 300문장. `risk/`·`vocab_ko.py` 이전에, 공유 슬롯 목록 없이 작성 (`risk/` 디렉터리는 한 번도 열지 않음). 분포: alert=false 58.7 %, kind = positive 124 / idiom 45 / negated 30 / clinician_question 30 / unrelated 27 / third_person 19 / past 14 / hypothetical 11; category = SI 108 / self_harm 39 / harm_to_others 27 / substance_acute 23 / null 103; 경상·전라 방언, 오타·띄어쓰기, 간접 사고, 계획·수단, 하드 네거티브 포함. |
| `src/chartwire/eval/data/FROZEN.txt` | `031a8f7d…ade5a916  heldout_risk_ko.jsonl` (`sha256sum -c FROZEN.txt` 로 검증). |
| `src/chartwire/synth/vocab_ko.py` | §10.1 전체: 주호소 8 템플릿, 수면/식욕/기분/불안/집중/약물/음주/계획/관찰/필러 템플릿(슬롯 + 골드 fact 타입), 약물·용량 16종, 위험 사례 7종(`RISK_CASES`), 프롬프트 주입 문장 6개, 성씨 50 + 희귀 음절 이름, 전화·주소 생성기, `pseudonym()`, 조사 선택 `josa()`. |
| `src/chartwire/synth/grammar.py` | §10.2 단계 문법(인사·주호소 → … → 마무리), `render()` 슬롯 채우기(약물은 `{drug}/{dose}/{drug_eul}/{drug_eun}` 동시 채움), 단계별 발화 조립. |
| `src/chartwire/synth/scripts.py` | **스크립트 JSON 계약** pydantic 모델(`Script`, `Utterance`, `Gold`, `GoldRisk`, `GoldPii`, `Meta`; 모두 `extra="forbid"`, `GoldFact`만 `extra="allow"`), 시드 결정론(`sha256(f"{seed}:{script_ref}")`), 위험/PII/주입 발화 배치, 타이밍, `generate_script()`, `generate_set()`, `script_refs()`. |
| `src/chartwire/synth/gold.py` | 세션 단위 골드: 고유 fact 집합, 위험 span(+`alert` 기대), `expected_alerts()`, PII 항목(+`[이름]/[전화]/[주소]` 토큰), `redact()`, `gold.jsonl` 레코드. |
| `src/chartwire/synth/cli.py` | `chartwire synth scripts --n 200 --seed 42 --out <dir> [--profile eval\|demo]` → `<ref>.json` + `index.json` + `gold.jsonl`. `write_scripts()`는 시드/bulk 로더가 재사용. |
| `scripts/readme_numbers.py` | stdlib 전용. `KEYS` 레지스트리(dotted key → `docs/**.json` + 경로), `--write` / `--check` / `--list`. |
| `src/chartwire/eval/readme_cli.py` | `chartwire readme-numbers --write\|--check` 래퍼(옵션은 그룹 콜백에 있어 하위 명령 없이 동작; 스크립트를 파일 경로로 임포트). |
| `src/chartwire/eval/report.py` | `report_header(seed, pg_version=None)` → `{seed, git_sha, generated_at, cpu, ram_gb, python, pg_version}`, `build_report()`. |
| `docs/eval/README.md` | §11.1 리포트 전부 + 누출 통제 프로토콜 (한국어). |
| `tests/unit/test_synth_vocab.py`, `test_synth_scripts.py`, `test_synth_gold.py`, `test_readme_numbers.py` | 27 tests. |

LOC: 구현 1,268 (어휘 데이터 포함) / 테스트 392.

## 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/unit/test_synth_vocab.py tests/unit/test_synth_scripts.py tests/unit/test_synth_gold.py tests/unit/test_readme_numbers.py -q   # 27 passed
ruff check src/chartwire/synth src/chartwire/eval scripts/readme_numbers.py tests/unit/test_synth_*.py tests/unit/test_readme_numbers.py
mypy --strict src/chartwire/synth src/chartwire/eval scripts/readme_numbers.py   # clean (strict는 의무 아님)
```

DB/Redis는 사용하지 않았습니다 (`chartwire_test_f` / Redis 6 미사용).

## 스크립트 JSON 계약 (WP-C 시뮬레이터 · WP-H 클라이언트가 소비)

```json
{"script_ref":"s01","seed":1,"template":"초진 우울","chunk_ms":200,"total_ms":166639,
 "utterances":[{"idx":0,"speaker":"clinician","text":"…","t_start_ms":0,"t_end_ms":4588,
   "gold":{"section_label":"none","facts":[],"risk":null,"pii":[]}}],
 "meta":{"generator":"chartwire.synth","version":"1","risk_kinds":["positive"],"injection":false,"injection_utterances":[]}}
```

- `utterances`는 `t_start_ms` 오름차순, 겹치지 않음(연속: 다음 `t_start = 이전 t_end`), `total_ms = 마지막 t_end + 1000`.
- 타이밍: `t_end = t_start + 180 ms × len(text) + U(400, 900)` (스펙 공식 그대로; 휴지가 `t_end`에 포함).
- `gold.risk.category`는 억제 종류에서도 표면 어휘의 범주(idiom만 `null`), `severity ≥ 1`은 `positive`에서만. **경보 기대 = `kind == "positive" and severity ≥ 1`** (`gold.alert_expected`).
- `gold.facts`: `{"type": "medication", "name": "에스시탈로프람", "dose": "10mg"}`, `sleep_latency{hours}`, `early_awakening{hour}`, `sleep_hours{hours}`, `weight_loss{weeks,kg}`, `alcohol{per_week,bottles}`, `duration{weeks}`, `plan_medication{name, dose?, action: increase|keep}`, `plan_followup{weeks}`.
- `gold.pii`: `{"kind": "name|phone|address", "value": "…"}`; 기대 `segment_search.text`는 `gold.redact(text, pii)`.
- `meta`는 생성기 소유 필드입니다. 소비자는 `chartwire.synth.scripts.Script.model_validate_json()`으로 검증하세요.
- 프로파일: `demo` → `s01..s20`, seed 1, 주입 없음. `eval` → `e0001..e0200`, seed 42, **주입 세션 = 매 10번째(`e0010, e0020, …`, 20개)**, 주입 발화 인덱스는 `meta.injection_utterances`.

eval 세트(seed 42, 200개) 실측: 발화 10,344개(세션당 46–61), positive 192 · 억제 종류 368(clinician_question 214, idiom 38, hypothetical 37, negated 31, past 31, third_person 17), 경보 기대 세션 100개, PII 발화 4.8 %.

## 스펙과 다른 점 (이유 포함)

1. **위험 사례 배치.** §10.2 "25 % 세션에 종류 균등 주입"을 문자 그대로 하면 positive 발화가 ~28개, 경보 세션 ~8개뿐이라 §10.3 표(≈220 positive / ≈330 억제)와 §11.1 `alert_latency`(경보 세션 100개)를 만족할 수 없습니다. 채택: 모든 세션에 임상가 위험 질문 1회(`clinician_question`); 50 % 세션은 위험 평가 답변이 positive + 0–2개 추가 positive; 독립적으로 25 % 세션에 억제 종류 1개(6종 균등) 2–4 발화 주입(positive가 아닌 세션이면 위험 평가 답변도 그 종류). 결과가 표의 수치와 일치합니다.
2. **`kind` 필드 추가 위치.** 스펙의 held-out 형식은 `{text, speaker, category|none, kind, note}`이지만 `severity`·`alert`를 추가해 경보 정답을 명시했습니다(경보 대상 정의를 하네스가 재구성하지 않도록).
3. **`readme_numbers.py`의 row 삭제 규칙.** "키가 없는 row"를 *row 안의 num 마커 중 하나라도 해결 불가(파일 없음/경로 없음/`null`)* 로 정의했습니다. row 밖의 미측정 num은 오류(exit 2) — 미측정 숫자를 조용히 남기지 않기 위해서입니다. 미등록 키도 exit 2.
4. **perf 요약 파일.** README 키는 `docs/perf/study.json`의 `{"queries":[{"id":"Q1","before_ms":…,"after_ms":…}]}`를 읽습니다(§11.3의 `docs/perf/plans/*.json`은 플랜 원본; Phase 1에서 제가 둘 다 씁니다).
5. `readme_cli.py`는 스크립트를 파일 경로로 임포트합니다(레지스트리는 CI가 패키지 설치 없이 실행해야 하므로 스크립트에 두어야 함). 휠 설치 환경에서는 명확한 `FileNotFoundError`.

## 스텁 / Phase 1로 넘긴 것

- `synth/seed.py`, `synth/bulk.py`, `eval/risk_eval.py`, `grounding_eval.py`, `inject_eval.py`, `purge_eval.py`, `perf/` — 미작성(계획대로 Phase 1). `vocab_ko.pseudonym()`은 seed.py용으로 미리 둠.
- 실제 리포트 JSON은 아직 없으므로 `readme_numbers.py --check`는 현재 README(마커 없음)에서 "최신"으로 통과합니다.

## 다른 WP에 요청

- **WP-A (`cli.py`)** — 이미 `LAZY_SUBAPPS`로 `synth` → `chartwire.synth.cli:app`, `readme-numbers` → `chartwire.eval.readme_cli:app`을 마운트합니다(확인: `chartwire synth scripts …`, `chartwire readme-numbers --check` 동작). 추가 요청 없음.
  CI `frozen-artifacts` 잡: `(cd src/chartwire/eval/data && sha256sum -c FROZEN.txt)` 와 `python scripts/readme_numbers.py --check`.
- **WP-C (`stt/simulator.py`)** — `Script.model_validate_json()`으로 로드; `Final.speaker`는 `utterance.speaker`, `t_start_ms/t_end_ms` 그대로. **`eval/data/`는 열지 마세요.**
- **WP-H (`loadtest/`, README)** — README 마커 키는 `python scripts/readme_numbers.py --list`. 리포트 JSON 형태:
  `docs/loadtest/A.json` `{"runs":[{"n":50,"ack_p50_ms","ack_p95_ms","ack_p99_ms","final_e2e_p95_ms","alert_e2e_p95_ms","chunks_per_s","credit_min","loss","dup"}, …]}`;
  `B.json` `{credit_zero_at_s, pause_count, stream_len_max, api_rss_slope_mb_per_min, loss}`;
  `C.json` `{ack_p95_ms, ack_p95_delta_pct, dropped_partials}`; `D.json` `{resume_success_pct, superseded_closes, rebuild_count, loss, dup}`;
  `E.json` `{loss, reconnect_p95_ms}`. 모두 `chartwire.eval.report.build_report(seed, body)`로 헤더를 붙이세요. 미측정 값은 키를 빼거나 `null`로 두면 해당 README 행이 삭제됩니다.
- **WP-G** — `docs/loadtest/H.json` `{events_per_s, dlq_count}` (+헤더).
- **WP-E / WP-D / WP-B** — Phase 1의 제 eval 러너가 만들 `purge.json`, `rls.json`, `inject.json`, `paraphrase.json`, `injection.json`, `protocol.json` 키는 `KEYS`에 이미 등록되어 있습니다(필드명은 `--list` 참조). 각 WP의 측정 함수는 그 필드명을 가진 dict를 돌려주면 됩니다.

## 알려진 이슈

- 발화 수는 세션당 46–61로 스펙 범위(40–120)의 아래쪽입니다. 단계별 풀이 스펙 어휘 목록으로 제한된 결과이며, Phase 1에서 필요하면 `Phase.n_statements`만 올리면 됩니다(계약 불변).
- `clinician_question` 종류의 추가 주입은 임상가 발화를 임의 위치에 넣으므로 그 자리의 화자 교대가 깨질 수 있습니다(교대율 평균 0.68, 테스트 하한 0.5).
