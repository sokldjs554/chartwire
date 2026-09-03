# WP-C handoff — Phase 0 (STT 어댑터 · 시뮬레이터 · 위험 탐지)

> 상태: **Phase 0 완료.** 단위 테스트 78개 통과, ruff/ruff-format/mypy --strict 클린. Phase 1 항목(`stt/worker.py`, `risk/alerts.py`, `worker/handlers/alert_sla.py`, 통합 테스트)은 아래 "Phase 1로 넘긴 것" 참조.

## 무엇을 만들었나 (모듈 맵)

| 경로 | 역할 |
|---|---|
| `src/chartwire/stt/base.py` | §3.1 동결 계약: `Chunk`, `Partial`, `Final`, `SttEvent`, `SttStream`, `SttAdapter`(Protocol), `SessionInfo{session_id, tenant_id, script_ref, chunk_ms=200, started_at=None}`, `Speaker` Literal. |
| `src/chartwire/stt/scripts_io.py` | 스크립트 JSON 계약의 pydantic 모델(`Script`, `Utterance`, `Gold`, `GoldFact`(extra allow), `GoldRisk`, `GoldPii`, `Meta`(필수 `generator`/`version`, 나머지 passthrough)) + 타임라인 검증(idx==위치, 정렬·비겹침, `total_ms ≥ 마지막 t_end`). `load_script(path) -> Script`, `script_path(scripts_dir, ref)`(`^[A-Za-z0-9_-]{1,64}$` 로 경로 탈출 차단), `ScriptError`. |
| `src/chartwire/stt/simulator.py` | `ScriptedSimulator(scripts_dir, *, seed=0, latency=gaussian_latency_ms, sleep=asyncio.sleep)`. `Timeline.build(script, chunk_ms)` 가 창(window)별 **prefix count 인덱스**(`finals_before[w]`)와 **현재 발화 인덱스**(`current[w]`)를 미리 계산 → `feed()`는 O(1) + 반환 이벤트 수. Final은 `t_end ∈ [offset, offset+chunk_ms)` 인 청크에서, 시드 지연 N(120,30) ms clamp [30,300] 한 번 `await sleep` 후 방출. Partial은 2번째 청크마다, `offset+chunk_ms` 를 포함하는 발화의 경과 비율 만큼 문자 접두사. `flush()` 는 남은 Final 전부. 중복 청크는 아무것도 재방출하지 않고, 갭 뒤 청크는 지나간 Final을 모두 방출(손실 0). 타임라인은 `(script_ref, chunk_ms)` 별 캐시. |
| `src/chartwire/stt/slow.py` | `SlowStt(adapter, delay_ms, *, sleep=asyncio.sleep)` — 청크마다 `delay_ms` 지연 후 내부 어댑터에 위임(시나리오 B 병목). |
| `src/chartwire/stt/aws_transcribe.py` | `map_results(results, *, from_seq, seq, speaker_labels)` — Transcribe `TranscriptEvent.transcript.results` → `Partial`/`Final`(화자 다수결 `spk_0→clinician`, `spk_1→patient`, 없으면 `unknown`; 신뢰도 = item confidence 평균; `start_time·1000`). `AwsTranscribeStream`(출력 스트림 펌프 태스크 + 큐, `feed`가 `send_audio_event`, `flush`가 `end_stream` 후 드레인), `AwsTranscribeStreaming(region, *, client=None, speaker_labels)` — SDK import-guard, `client` 주입으로 오프라인 테스트. |
| `src/chartwire/risk/lexicon_ko.py` | 구절 **292개**(`Phrase(text, category, severity)`), 4 범주 × 3 등급, 방언·붙여쓰기 포함. `IDIOMS_OVERLAP`(히트와 겹칠 때 억제) / `IDIOMS_CONTEXT`(같은 문장에 있으면 억제). 임포트 시 중복·공백·등급 검사. |
| `src/chartwire/risk/scope.py` | `ScopeFlags`(+`suppressed`), `classify(text, start, end, speaker)`. 창 = 히트 문장, 근접 = 공백 제외 음절 12(주어 8). 규칙 6종은 `docs/risk-detection.md` §3 표 참조. |
| `src/chartwire/risk/detector.py` | `DETECTOR_VERSION = "lex-1"`, `RiskHit`(+`suppressed`, `alerts`), `scan(text, speaker) -> list[RiskHit]` (0 또는 1개). 포함되는 짧은 어간 제거 → 범위 플래그 → `(경보, severity, 길이, 위치)` 순 선택. |
| `src/chartwire/risk/terms.py` | `lexicon_tag(text) -> list[str]` — 증상 정규형 19종, 약물 일반명 48종, 위험 범주 태그(`자살사고/자해/타해사고/급성음주`); 정렬·중복 제거; **언급** 기준(부정돼도 태그). |
| `tests/fixtures/scripts/t01..t03.json` | 손으로 쓴 소형 스크립트(5/2/1 발화; chunk 200/200/100; PII·위험 gold 포함). |
| `docs/risk-detection.md` | 설계·등급·범위 규칙·한 히트 규칙·terms 태깅·한계·누출 통제 프로토콜 (한국어). |

LOC(비공백): 구현 1,216 (그중 사전·용어 데이터 ≈ 330) / 테스트 593.

## 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/unit/test_stt_*.py tests/unit/test_risk_*.py -q      # 78 passed
ruff check src/chartwire/stt src/chartwire/risk tests/unit/test_stt_*.py tests/unit/test_risk_*.py
ruff format --check src/chartwire/stt src/chartwire/risk tests/unit/test_stt_*.py tests/unit/test_risk_*.py
mypy --strict src/chartwire/stt src/chartwire/risk                          # clean (strict는 의무 아님)
```

DB/Redis 미사용 (`chartwire_test_c` / Redis 3 은 Phase 1 통합 테스트에서 씀). `tests/unit` 전체도 함께 통과(478).

테스트가 덮는 것: §10.2 위험 사례 7종 전부(positive / negated / hypothetical / past / third_person / clinician_question / idiom) + 등급 순서 + 세그먼트당 1 히트 + 문법 어휘 무해 문장 회귀; 시뮬레이터는 Final 창 정확성(chunk 200/300/100), Partial 2청크 주기·단조 접두사·`from_seq == seq_start`, `flush`, 중복·갭 청크, 지연 시드/클램프/횟수, 캐시, 인덱스 vs 브루트포스; `SlowStt` 지연 횟수; Transcribe 매핑·스트림(가짜 클라이언트)·SDK 미설치 오류; 스크립트 계약 위반 8종.

**In-grammar 자가 점검**(held-out 아님, WP-F 생성기 eval 세트 200개 = 10,344 발화, 스크래치 스크립트로만 실행): gold positive 192/192 경보, 미탐 0, 무해·억제 발화 10,121개에서 경보 0. 유일한 불일치는 아래 요청 1의 `past` 31건.

## 스펙과 다른 점 / 스펙이 침묵해서 정한 것

1. **`past` 히트의 부정 소비.** §9.4의 `past` 규칙(과거 표지 + `지금은/요즘은` + 부정)이 성립하면 그 부정을 `negated` 로 다시 세지 않습니다. 그렇지 않으면 모든 past 문장이 `negated` 로도 억제되어 "severity −1, 여전히 경보" 규칙이 도달 불가능해집니다.
2. **가설 접미사.** 스펙은 `만약|라면|다면|가정|~면 어떻` 을 히트 **앞**에서 찾지만, 한국어에서 `죽고 싶다면` 처럼 히트에 바로 붙는 `다면/라면` 도 가설이므로 뒤 3글자 이내 접미사도 인정합니다(테스트 `test_hypothetical_kind`).
3. **음절 창.** "≤12 syllables" 를 공백 제외 문자 수로 구현했습니다(공백 포함 12자면 `손목을 긋고 싶다는 생각은 한 번도 안 해봤어요` 의 부정을 놓칩니다).
4. **포함 어간 제거.** 다른 후보 구간에 완전히 포함되는 후보(`손목` ⊂ `손목을 긋`)는 같은 언급이므로 버립니다. 스펙에 없지만 없으면 부정된 긴 구절 옆에서 짧은 어간이 따로 경보를 냅니다.
5. **경보 가능 히트 우선.** "최고 등급 1개" 를 `(경보 가능, severity, 길이, 위치)` 순으로 해석했습니다: 억제된 3등급보다 경보 가능한 1등급을 돌려줍니다(안전망 원칙). 억제만 있으면 최고 등급 억제 히트를 돌려주어 `risk_hits_total{suppressed}` 집계가 가능합니다.
6. **`total_ms` 검증은 `≥ 마지막 t_end`.** 계약은 `+1000` 이지만 로더가 생성기의 반올림 차이로 실제 스크립트를 거부하지 않도록 느슨하게 두었습니다(픽스처는 정확히 +1000).
7. **`Meta` 는 `extra="allow"`.** 계약의 필수 키는 `generator`, `version` 두 개이고 WP-F가 `risk_kinds/injection/injection_utterances` 를 추가하므로 통과시킵니다.
8. **시뮬레이터 창의 기준 chunk_ms 는 `SessionInfo.chunk_ms`**(실제 레코더 청크 폭)이며 스크립트의 `chunk_ms` 는 권장값입니다. `flush()` 는 지연을 넣지 않습니다(세션 종료 경로).
9. **Transcribe 화자 규약**: 첫 화자(`spk_0`) = 임상가(§10.2 인사가 임상가). `speaker_labels` 로 바꿀 수 있습니다.
10. **`terms[]` 형식**: `search_segments(:q, 'term')` 이 `terms @> ARRAY[:q]` 이므로 태그는 사람이 입력하는 한국어 정규어(`불면`, `에스시탈로프람`, `자해`)입니다. 위험 태그는 범위 플래그와 무관한 **언급** 기준입니다.

## Phase 1로 넘긴 것 (스텁 없음 — 파일 자체를 아직 만들지 않음)

- `stt/worker.py`(consumer group, XAUTOCLAIM, 갭 rebuild, inline risk tx), `risk/alerts.py`(`on_final_segment`, `escalate_due`), `worker/handlers/alert_sla.py`, 통합 테스트(FLUSHALL + SIGSTOP 복구). 이들은 WP-A의 `repo/segments.insert_final`, `redis/keys.py`, WP-B의 스트림 계약이 안정된 뒤 붙입니다.
- LOC 예산(§0: stt+worker+risk+alerts 1,000)은 Phase 0 만으로 1,216 입니다. 사전 데이터(≈330줄)를 빼면 ≈ 890. Phase 1 워커/알림은 예산을 넘기므로 통합 시 조정이 필요합니다(사전을 JSON 데이터로 옮기면 코드 LOC 는 줄지만 mypy·임포트 검사를 잃음 — 결정은 통합자에게).

## 다른 WP에 요청

1. **WP-F (`eval/risk_eval.py`, `synth/gold.py`)** — §9.4 는 `past` 종류를 "severity −1, 플래그 유지, ≥1이면 경보" 로 규정합니다. 따라서 `작년엔 죽고 싶었는데 지금은 아니에요` 는 detector 가 **severity 1 경보**를 냅니다. WP-F 골드(`alert_expected = kind=="positive" and severity>=1`)는 이를 "경보 없음" 으로 보므로 in-grammar 세트에서 31건이 FP 로 집계됩니다. 요청: `past` 종류의 골드를 `alert = (사전 severity − 1) >= 1` 로 두거나, 리포트에서 `past` 행을 분리해 주세요. 두 문서(`docs/eval/README.md`, `docs/risk-detection.md`)에 같은 해석을 적어야 합니다.
2. **WP-F** — 스크립트 로더는 `chartwire.stt.scripts_io.load_script` 를 쓰면 됩니다(계약은 WP-F `Script` 와 동일 필드; `Meta` 는 passthrough). `GoldRisk.category` 가 `null` 이면서 `severity ≥ 1` 인 조합은 두 모델 모두 허용하지만 의미가 없으니 생성기에서 만들지 않는지 확인 부탁드립니다.
3. **WP-B (`ws/`)** — stt-worker 가 소비할 스트림 엔트리 `seq,key,len,off,fl,ep,ts` 와 end marker `{"end":"1"}` 가 §5·§15 그대로라는 가정으로 Phase 1 워커를 씁니다. `off` 는 청크의 `offset_ms`(청크 폭 배수)여야 시뮬레이터 창이 맞습니다.
4. **WP-A (`redis/keys.py`, `repo/segments.py`, `repo/risk.py`)** — Phase 1에서 `stt:active`, `stt:owner:{sid}`, `stt:lag`, `alerts:sla`, `sess:{sid}:chunks` 키 헬퍼와 `segments.insert_final(...)`, `risk.insert_event(...)`(필드: `category, severity, phrase, span, scope flags, detector_version`) 시그니처를 사용할 예정입니다. 이미 있으면 그대로 쓰고, 없으면 Phase 1 시작 시 요청 diff 를 여기 추가합니다.
5. **WP-H (console/loadtest)** — 위험 경보 표시에 `RiskHit.scope` 플래그(특히 `past`)를 같이 보여주면 임상가가 "왜 1등급인가" 를 바로 알 수 있습니다(§6.3 `risk.alert` 페이로드에 `span` 만 있으므로 선택 사항).

## 알려진 이슈

- 이중 부정·12음절 밖 부정·`손목이 아파요` 류 오탐은 의도된 고재현 설계입니다(`docs/risk-detection.md` §6).
- `AwsTranscribeStreaming` 은 실제 네트워크로 검증되지 않았습니다(§0: 오프라인 빌드 박스). 매핑·스트림 로직만 가짜 객체로 테스트했습니다.
- 시뮬레이터 Partial 은 발화 길이가 `chunk_ms` 보다 짧으면 나오지 않을 수 있습니다(정상: `t03` 은 chunk 100 으로 테스트).

## 누출 통제

`src/chartwire/eval/data/` 는 열지 않았고 열지 않습니다 (§10.3, §15). 이 WP 의 모든 예문은 §9.4·§10.2 의 in-grammar 사례이거나 새로 지은 문장입니다.
