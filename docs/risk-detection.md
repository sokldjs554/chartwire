# 위험 발화 탐지 (`risk/`) — 설계 노트

> **이 모듈은 결정론적 고재현(high-recall) 안전망이지 임상 분류기가 아닙니다.** 자살·자해·타해·급성 음주 관련 표현이 전사에 나타나면 수 초 안에 임상가 화면에 경보를 띄우는 것이 목적이며, 진단·중증도 판정·치료 결정은 어떤 형태로도 하지 않습니다. 안전 경로에는 LLM이 없습니다 (ADR-0003, 스펙 §0.4, §9.4).

## 1. 왜 사전(lexicon) 기반인가

| 요구 | 선택 |
|---|---|
| 발화가 커밋되는 트랜잭션 **안에서** 실행 (§7.4) → 마이크로초 단위, 외부 호출 없음 | 순수 파이썬 부분 문자열 탐색 + 정규식 |
| 같은 입력은 언제나 같은 출력 → 감사·재현·회귀 테스트 가능 | 결정론, `DETECTOR_VERSION = "lex-1"` 을 `risk_events.detector_version`에 기록 |
| 놓치는 것(FN)이 잘못 울리는 것(FP)보다 훨씬 비쌈 | 사전은 넓게, 억제 규칙은 좁게 |
| 규칙이 왜 울렸는지 임상가가 바로 이해 | 경보에 `phrase`, `span`, `scope` 플래그 동봉 |

정밀도/재현율은 **측정값으로만** 말합니다 (`docs/eval/README.md`, `chartwire eval risk`). 이 문서에는 숫자가 없습니다.

## 2. 사전 (`lexicon_ko.py`)

- 구절은 **어간(stem)** 입니다. `죽고 싶` 은 `죽고 싶어요 / 죽고 싶다는 / 죽고 싶은 마음` 을 모두 잡습니다. 표준어·붙여쓰기(`죽고싶`)·경상/전라 방언(`죽고 싶데이`, `살기 싫어예`)을 포함합니다.
- 범주 4종: `suicidal_ideation`, `self_harm`, `harm_to_others`, `substance_acute` (`risk_events.category` CHECK와 동일).
- 등급 3단계:

| severity | 의미 | 예 | SLA (§7.4) |
|---|---|---|---|
| 3 | 계획·수단·시도 | `약을 모아`, `뛰어내리`, `목을 매`, `유서`, `번개탄`, `손목을 긋`, `칼로`, `술을 마시고 약을` | 60 s |
| 2 | 능동적 사고·자해 행위·급성 위험 | `죽고 싶`, `사라지고 싶`, `살기 싫`, `자살`, `자해`, `손목`, `때리고 싶`, `필름이 끊` | 300 s |
| 1 | 절망감·수동적 소망 | `살 이유가 없`, `희망이 없`, `의미가 없`, `짐이 되`, `없는 게 나` | 없음 |

- 관용구/하드 네거티브 두 종류:
  - **겹침 관용구** (`IDIOMS_OVERLAP`): 히트 구간과 겹칠 때만 억제 — `자살 예방`, `죽을 만큼`, `피곤해 죽`, `죽여주`, `미치겠` …
  - **문맥 관용구** (`IDIOMS_CONTEXT`): 같은 문장 안에 있으면 억제 — `드라마에서`, `뉴스에서`, `영화에서`, `기사에서`, `캠페인`, `예방 교육` …
- 모듈 임포트 시 중복·공백·등급 범위를 검사합니다. 어휘 추가는 해당 튜플에 한 줄 넣고 `tests/unit/test_risk_lexicon.py` 를 통과시키면 됩니다. 문법 어휘(§10.1)의 증상 표현(`갑자기 죽을 것 같은 공포가 와요`, `기운이 하나도 없어요`)이 걸리지 않도록 `죽을 것`·`없어` 류의 어간은 **넣지 않습니다** (회귀 테스트 `test_empty_and_benign_text`).

## 3. 범위 규칙 (`scope.py`)

창(window)은 **히트가 속한 문장** 이고, 근접 규칙은 공백을 뺀 **음절 수**로 셉니다(기본 12). 문장 경계는 `.?!…` 와 `요/다/죠/까/네 + 공백` 입니다.

| 플래그 | 조건 | 효과 |
|---|---|---|
| `negated` | 히트 **뒤** 12음절 안에 `않 / 없(이 제외) / 아니 / 안␣ / 못␣` | 억제 |
| `hypothetical` | 히트 **앞** 12음절 안에 `만약 / 라면 / 다면 / 가정 / 면 어떻`, 또는 히트에 바로 붙은 `다면/라면` | 억제 |
| `past` | 문장에 과거 표지(`예전 / 작년 / 그때 / 했었 / 전에는 …`) **그리고** 히트 뒤에 현재 표지(`지금은 / 요즘은 / 이제는 …`) **그리고** 그 뒤에 부정 | severity −1, 플래그 유지, 결과가 ≥1이면 여전히 경보. 현재절의 부정은 `negated`로 **다시 세지 않음** |
| `third_person` | 히트 앞 8음절 안에 `친구/동생/형/누나/언니/오빠/엄마/아빠/어머니/아버지/지인/동료/아는 사람 …` + 조사 `가/이/는/은/도/께서`; 그 사이에 1인칭(`제가/내가/저는 …`)이 있으면 무효 | 억제 |
| `clinician_question` | `speaker == "clinician"` 이고 문장이 `?` 또는 `세요/나요/십니까/습니까/까요/죠` 로 끝남 | 억제 |
| `idiom` | §2의 겹침/문맥 관용구 | 억제 |

**억제 = `negated ∨ hypothetical ∨ third_person ∨ clinician_question ∨ idiom`.** 경보 = `severity ≥ 1 ∧ ¬억제` (`RiskHit.alerts`). 임상가의 **평서문**(`환자분이 약을 모아두셨다고요`)은 억제되지 않습니다 — 안전망은 화자 역할을 신뢰하지 않습니다.

## 4. 세그먼트당 한 개의 히트 (`detector.py`)

1. 모든 사전 구절의 모든 출현을 후보로 모읍니다.
2. 다른 후보 구간에 **완전히 포함되는** 후보는 버립니다 — `손목` ⊂ `손목을 긋`, `죽고 싶` ⊂ `죽고 싶은 마음`. 같은 언급을 더 구체적인 어간으로 본 것이므로 범위 규칙은 가장 구체적인 구절에만 적용합니다. (그렇지 않으면 `손목을 긋고 싶다는 생각은 한 번도 안 해봤어요` 에서 부정이 12음절 밖에 있는 짧은 어간 `손목`이 따로 경보를 냅니다.)
3. 남은 후보마다 범위 플래그를 계산하고 `(경보 여부, severity, 구절 길이, 앞선 위치)` 순으로 하나를 고릅니다. 경보 가능한 히트가 하나라도 있으면 억제된 고등급 히트보다 우선합니다(안전망 원칙). 모두 억제됐다면 가장 높은 등급의 억제 히트를 돌려주어 `risk_hits_total{suppressed="true"}` 지표에 반영합니다.

stt-worker(§7.4)는 `hit.alerts` 인 경우에만 `risk_events` 행을 만들고 `risk.alert` 를 발행합니다.

## 5. `terms[]` 태깅 (`terms.py`)

`lexicon_tag(text)` 는 동의 범위 `search_index` 가 있을 때 `segment_search.terms` 에 들어가는 **정규 검색어** 목록입니다 (`terms @> ARRAY[:query]`, GIN, §4.6 Q2d). 증상 정규형(`불면`, `불면증`, `식욕저하`, `공황` …), 약물 일반명(≈48), 위험 범주의 한국어 태그(`자살사고`, `자해`, `타해사고`, `급성음주`)를 정렬된 중복 없는 리스트로 돌려줍니다. **언급**을 색인하므로 부정된 언급(`죽고 싶다는 생각은 없어요`)도 태그됩니다 — "지난 6개월 이 환자의 불면 언급"이 질문이고, 경보 여부는 탐지기가 답합니다.

## 6. 알려진 한계 (의도된 것)

- 이중 부정(`죽고 싶지 않은 건 아니에요`)은 `negated` 로 억제됩니다. 부정 표지가 12음절 밖에 있으면(`죽고 싶어요 … 안 …`) 경보가 납니다 — FN보다 FP 쪽으로 기울인 선택입니다.
- `손목`, `의미가 없`, `칼로` 같은 넓은 어간은 스펙(§9.4)이 명시한 고재현 항목이며 `손목이 아파요` 류의 오탐을 냅니다.
- 화자 라벨이 `unknown` 인 세그먼트(실제 STT)는 환자 발화로 취급됩니다(질문 억제 없음).
- 자소 분리·오타·초성(`ㅈㅅ`)은 다루지 않습니다. 사전 확장은 held-out 결과를 보고 결정하되, §7의 절차를 지킵니다.

## 7. 누출 통제 프로토콜 (§10.3, §15)

- 헤드라인 P/R 을 내는 `eval/data/heldout_risk_ko.jsonl` 은 WP-F가 이 모듈보다 **먼저**, 공유 슬롯 목록 없이 손으로 썼고 `FROZEN.txt`(sha256)로 고정됩니다.
- 이 모듈의 작성자(WP-C)는 `src/chartwire/eval/data/` 를 **열지 않았습니다**. 여기 있는 모든 예문은 스펙 §9.4·§10.2에 적힌 in-grammar 사례이거나 작성자가 새로 만든 문장입니다.
- held-out 작성자(WP-F)는 `risk/` 를 열지 않습니다. 평가 하네스(WP-F)가 두 세트를 따로 돌리고 별도 행으로 보고합니다.
- 사전을 고칠 때는 in-grammar 세트와 단위 테스트만 보고 고칩니다. held-out 점수를 보고 어휘를 추가하면 그 점수는 더 이상 held-out이 아닙니다 — 그런 변경은 `DETECTOR_VERSION` 을 올리고 새 held-out 세트를 만든 뒤에만 합니다.

## 8. 경보 수명주기 (`alerts.py`, stt-worker, `alert_sla` 티커)

탐지기는 순수 함수이고, 그 결과를 **행·타이머·메시지**로 바꾸는 것은 `risk/alerts.py` 입니다. 세 시점으로 나뉩니다.

| 시점 | 함수 | 하는 일 |
|---|---|---|
| 최종 세그먼트 트랜잭션 **안** (stt-worker, §7.4) | `create_event(session, …, hit, now)` | `hit.alerts` 인 경우에만 `risk_events` 행(`phrase`, `span`, `scope` 플래그, `detector_version`, `sla_deadline_at = now + 60 s(3등급) / 300 s(2등급) / 없음(1등급)`) + 감사 `alert.created`. 세그먼트와 같은 트랜잭션이므로 세그먼트 없는 경보, 경보 없는 세그먼트는 존재하지 않습니다 |
| 커밋 **뒤** | `after_commit(redis, event, committed_at)` | `ZADD alerts:sla {tenant}:{id} = 마감 epoch-ms`(타이머가 있는 등급만) + `PUBLISH sess:{sid}:events risk.alert{risk_event_id, category, severity, segment_seq, span, sla_deadline_at?, committed_at}`. `committed_at` 이 경보 지연 측정(§11.2 `alert_e2e`)의 시작점입니다. 둘 다 멱등이라 재전달돼도 안전합니다 |
| 임상가 확인 | `ack(ctx, tenant_id, risk_event_id, by, actor_role)` (REST 와 뷰어 소켓 공용) | 행위자 역할의 테넌트 트랜잭션에서 `acknowledged_at/by`(첫 확인이 이김, 멱등) + 감사 `alert.acked` → `ZREM` → `PUBLISH risk.ack{risk_event_id, by}`. RLS 밖의 id 는 `None` |
| 마감 경과 | `escalate_due(ctx, now)` — worker 의 `alert_sla` 티커(1 s) | `ZRANGEBYSCORE alerts:sla -inf now` → 테넌트별 한 트랜잭션에서 `escalation_level=1, escalated_at` + 감사 `alert.escalated` → 커밋 뒤 `PUBLISH risk.escalated` 를 세션 채널과 `tenant:{tid}:alerts` 양쪽에 → 마감이 지난 멤버는 (이미 확인됐거나 형식이 깨졌어도) 제거. **한 단계뿐**입니다 |

`risk_unacked_over_sla` 게이지는 Redis 가 아니라 PostgreSQL(`acknowledged_at IS NULL AND sla_deadline_at < now`, 부분 인덱스 `ix_risk_open_sla`)에서 10 s 마다 다시 셉니다 — ZSET 을 잃어도(시나리오 A) 게이지는 참이고, `chartwire outbox stats --rebuild-sla` 가 ZSET 을 되살립니다(`docs/ops/runbook.md` §3-5).

`risk_hits_total{category, severity, suppressed}` 는 억제된 히트까지 셉니다(경보 행은 만들지 않음). 통합 테스트: `tests/integration/test_alerts.py`(생성·확인·에스컬레이션, `FakeClock`), `tests/integration/test_stt_worker.py`(세그먼트 트랜잭션 안의 경보 + ZSET + 발행).

## 9. `past` 종류와 held-out 레이블의 차이 (`docs/eval/README.md` 해석 규칙 1 과 동일)

spec §9.4 는 과거 서술(`작년엔 죽고 싶었는데 지금은 아니에요`)을 *severity −1, 그래도 ≥ 1 이면 경보* 로 규정하고, 생성기 골드는 억제 종류 전부를 `alert=false` 로 둡니다. 두 해석을 하나로 합치지 않습니다: in-grammar P/R/F1 에서 `past` 발화를 **제외**하고 `past_kind.{n, alerted}` 로 따로 보고합니다. held-out 세트는 동결된 레이블(`alert=false`, 작성자의 임상 판단)을 그대로 쓰므로 §9.4 대로 동작하는 이 탐지기는 그 행에서 오탐으로 집계됩니다 — `per_kind.past.fp_rate` 가 그 크기를 보여줍니다. 이 문서와 `docs/eval/README.md` 는 같은 해석을 적습니다.
