# 실행계획 연구 (`chartwire perf study`, spec §4.6)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 아래 표의 숫자 자리는 `docs/perf/summary.json` 에서
> `scripts/readme_numbers.py --write` 가 채웁니다(§11.4). **측정 전에는 자리표시자(`—`)이며, 손으로 적은 수치는 없습니다.**
> 측정되지 않은 행은 README 에서 삭제됩니다.

## 데이터셋

`chartwire synth bulk --seed 7 --segments 2000000 --tenants 8 --out docs/perf/bulk.json` (§4.6, §10.3 `bulk`):

| 항목 | 값 |
|---|---|
<!-- row:perf.bulk.tenants -->| 테넌트 | <!-- num:perf.bulk.tenants -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.sessions -->| 세션 (× 100 세그먼트) | <!-- num:perf.bulk.sessions -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.patients -->| 환자 | <!-- num:perf.bulk.patients -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.segments -->| `transcript_segments` (24개 월 파티션) | <!-- num:perf.bulk.segments -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.search -->| `segment_search` (세션의 60 %) | <!-- num:perf.bulk.search -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.risk -->| `risk_events` (세그먼트의 2 %, 그중 1 % 미확인) | <!-- num:perf.bulk.risk -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.outbox -->| `outbox_events` (99.9 % done) | <!-- num:perf.bulk.outbox -->—<!-- /num --> |
<!-- /row -->
<!-- row:perf.bulk.total_s -->| 적재 + VACUUM ANALYZE 시간 | <!-- num:perf.bulk.total_s -->—<!-- /num --> s |
<!-- /row -->

- 키워드 비율(`segment_search.text`): `불면` 2 %(그중 `불면증` 0.5 %, 부분 문자열 포함), `에스시탈로프람` 0.3 %, `자해` 0.2 %.
  `terms[]` 는 `risk.terms.lexicon_tag` 로 문장마다 한 번 태깅.
- `text_enc` 는 1바이트 자리표시자 `\x00`, 환자 식별자도 자리표시자. `name_hmac` 만 진짜 블라인드 인덱스(Q6).
- **로더는 우회가 아니다.** owner 로 접속하지만 모든 테넌트 테이블은 FORCE RLS 라 owner 에게도 정책이 적용된다.
  PostgreSQL 은 RLS 가 적용되는 테이블에 `COPY FROM` 을 거부하므로(`FeatureNotSupportedError`), temp 스테이지에
  COPY 한 뒤 테넌트 트랜잭션 안에서 `INSERT … SELECT` 로 옮긴다 — `WITH CHECK` 정책을 통과한 행만 들어간다.

## 방법

`perf/study.py`: superuser psycopg 연결 하나, 쿼리마다 별도 트랜잭션에서 `SET LOCAL ROLE chartwire_app|chartwire_owner`(또는 그대로 superuser)
+ `set_config('app.tenant_id', …, true)` → `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 워밍업 1회 + 5회 → **클라이언트 wall time 중앙값**,
중앙값 실행의 플랜을 `docs/perf/plans/<id>_<state>.json` 에 기록. 전부 롤백(Q4 의 `FOR UPDATE SKIP LOCKED` 도 데이터를 바꾸지 않음).
`--state before` 는 리비전 0006(RLS 까지), `--state after` 는 0007(인덱스·함수). `summary.json` 은 두 실행을 병합한다.

```bash
chartwire db downgrade 0006 && chartwire perf study --state before --out docs/perf
chartwire db upgrade head  && chartwire perf study --state after  --out docs/perf
```

<!-- row:perf.pg_version -->PostgreSQL <!-- num:perf.pg_version -->—<!-- /num -->
<!-- /row -->
설정은 `summary.json.states.*.settings` (`shared_buffers`, `work_mem`, `jit` …) · 헤더에 git sha, CPU, RAM, 시드.

## 결과 (ms, 중앙값)

| # | 쿼리 (앱이 실행하는 그대로) | before (0006) | after (0007) | 무엇을 보여주나 |
|---|---|---|---|---|
<!-- row:perf.Q1 -->| Q1 | 환자 종단 타임라인 6개월 `ORDER BY created_at DESC, id DESC LIMIT 50` | <!-- num:perf.Q1.before_ms -->—<!-- /num --> (<!-- num:perf.Q1.before_plan -->—<!-- /num -->) | <!-- num:perf.Q1.after_ms -->—<!-- /num --> (<!-- num:perf.Q1.after_plan -->—<!-- /num -->) | 파티션 프루닝 + 무중단 파티션 인덱스 `ix_segments_patient_time` |
<!-- /row -->
<!-- row:perf.Q1a -->| Q1a | 세션 replay `seq > :a` — `created_at` 조건 없음 / `>= started_at` / 상·하한 | <!-- num:perf.Q1a_without.before_ms -->—<!-- /num --> / <!-- num:perf.Q1a_with.before_ms -->—<!-- /num --> / <!-- num:perf.Q1a_bounded.before_ms -->—<!-- /num --> | <!-- num:perf.Q1a_without.after_ms -->—<!-- /num --> / <!-- num:perf.Q1a_with.after_ms -->—<!-- /num --> / <!-- num:perf.Q1a_bounded.after_ms -->—<!-- /num --> | 파티션 키가 조건에 있어야 프루닝; 하한만 주면 **과거** 파티션만 잘린다 |
<!-- /row -->
<!-- row:perf.Q1b -->| Q1b | keyset 100번째 페이지 vs `OFFSET 5000` | <!-- num:perf.Q1b_keyset.before_ms -->—<!-- /num --> / <!-- num:perf.Q1b_offset.before_ms -->—<!-- /num --> | <!-- num:perf.Q1b_keyset.after_ms -->—<!-- /num --> / <!-- num:perf.Q1b_offset.after_ms -->—<!-- /num --> | OFFSET 은 건너뛰는 행을 전부 읽는다 |
<!-- /row -->
<!-- row:perf.Q2a -->| Q2a | `text ILIKE '%불면%'` as app (2음절) | <!-- num:perf.Q2a.before_ms -->—<!-- /num --> | <!-- num:perf.Q2a.after_ms -->—<!-- /num --> (<!-- num:perf.Q2a.after_plan -->—<!-- /num -->) | 2음절은 트라이그램이 없다 — 정직한 한계; API 는 3자 이상만 |
<!-- /row -->
<!-- row:perf.Q2b -->| Q2b | `text ILIKE '%불면증%'` as app (RLS) | <!-- num:perf.Q2b.before_ms -->—<!-- /num --> | <!-- num:perf.Q2b.after_ms -->—<!-- /num --> (<!-- num:perf.Q2b.after_plan -->—<!-- /num -->) | **GIN 이 있어도 미사용** — `texticlike` 가 leakproof 가 아님 (`leakproof.txt`, ADR-0005) |
<!-- /row -->
<!-- row:perf.Q2c -->| Q2c | 같은 쿼리 as owner (RLS 면제 = `search_segments` 실행 컨텍스트) | <!-- num:perf.Q2c.before_ms -->—<!-- /num --> | <!-- num:perf.Q2c.after_ms -->—<!-- /num --> (<!-- num:perf.Q2c.after_plan -->—<!-- /num -->) | 해결책: SECURITY DEFINER 경계 |
<!-- /row -->
<!-- row:perf.Q2d -->| Q2d | `search_segments('불면증','text')` / `('불면','term')` as app (wall time) | — (0007 이전 함수 없음) | <!-- num:perf.Q2d_text.after_ms -->—<!-- /num --> / <!-- num:perf.Q2d_term.after_ms -->—<!-- /num --> | app 역할에서의 end-to-end 수정; `terms @>` 는 배열 GIN |
<!-- /row -->
<!-- row:perf.Q3 -->| Q3 | 미확인 경보 `ORDER BY sla_deadline_at LIMIT 100` | <!-- num:perf.Q3.before_ms -->—<!-- /num --> (<!-- num:perf.Q3.before_plan -->—<!-- /num -->) | <!-- num:perf.Q3.after_ms -->—<!-- /num --> (<!-- num:perf.Q3.after_plan -->—<!-- /num -->) | 부분 인덱스 `ix_risk_open_sla` (대시보드가 초당 폴링) |
<!-- /row -->
<!-- row:perf.Q4 -->| Q4 | outbox 클레임 `status='pending' … FOR UPDATE SKIP LOCKED` (2 M 행) | <!-- num:perf.Q4.before_ms -->—<!-- /num --> (<!-- num:perf.Q4.before_plan -->—<!-- /num -->) | <!-- num:perf.Q4.after_ms -->—<!-- /num --> (<!-- num:perf.Q4.after_plan -->—<!-- /num -->) | 부분 인덱스 `ix_outbox_pending`; dead tuple <!-- num:perf.pgstattuple.after.dead_tuple_percent -->—<!-- /num --> % (`pgstattuple`) |
<!-- /row -->
<!-- row:perf.Q5 -->| Q5 | RLS 오버헤드: Q1 / Q3 를 app(정책) vs superuser(우회) | Q1 <!-- num:perf.rls_overhead.before.q1_pct -->—<!-- /num --> %, Q3 <!-- num:perf.rls_overhead.before.q3_pct -->—<!-- /num --> % | Q1 <!-- num:perf.rls_overhead.after.q1_pct -->—<!-- /num --> %, Q3 <!-- num:perf.rls_overhead.after.q3_pct -->—<!-- /num --> % | 정책 형태 인라인 <!-- num:perf.Q5_form_inline.after_ms -->—<!-- /num --> vs InitPlan <!-- num:perf.Q5_form_initplan.after_ms -->—<!-- /num --> (superuser 에뮬레이션) |
<!-- /row -->
<!-- row:perf.Q6 -->| Q6 | 환자 이름 `name_hmac = :h` (50 K 환자) | <!-- num:perf.Q6.before_ms -->—<!-- /num --> | <!-- num:perf.Q6.after_ms -->—<!-- /num --> (<!-- num:perf.Q6.after_plan -->—<!-- /num -->) | 암호화 컬럼의 HMAC 블라인드 인덱스 정확 일치 |
<!-- /row -->

`(…)` 안은 플랜 최상위 노드(`summary.json.queries[].{state}_plan`); 인덱스 이름과 노드 목록은 `{state}_indexes` / `{state}_node_types`, 5회 샘플은 `{state}_samples_ms`.

### Q2 세 플랜 나란히 (after)

`plans/q2b_after.json`(app, RLS) · `plans/q2c_after.json`(owner) 를 비교하면 같은 SQL 이 역할에 따라 다른 플랜을 받는다는 것이 보인다.
Q2d 는 plpgsql 함수 호출이라 플랜 파일이 없고 wall time 만 기록한다. 서술은 측정 뒤 통합자가 플랜을 인용해 채운다.

### Q4 bloat 관찰

`summary.json.pgstattuple.{before,after}` — `outbox_events` 의 `tuple_count`, `dead_tuple_count`, `dead_tuple_percent`, `free_percent`.
`outbox_prune` 티커(24 h 지난 done 삭제)가 돌기 전/후의 값을 비교하려면 두 실행 사이에 `chartwire outbox`… 대신 워커를 잠시 띄운다.

### Q5 주의

RLS 오버헤드는 같은 플랜에서 정책 qual 평가 비용만 재는 것이 아니라, RLS 때문에 플랜 모양이 달라지는 경우(Q2b) 그 차이도 포함한다.
정책 형태 비교(인라인 vs InitPlan)는 정책 자체를 바꾸지 않고 superuser 쿼리에 같은 qual 을 두 형태로 써서 **에뮬레이션**한 것이다(`perf/queries.py`).

## 파일

| 파일 | 내용 |
|---|---|
| `summary.json` | README 키의 유일한 출처. `{header, states{before,after}, queries[{id, before_ms, after_ms, *_plan, *_indexes, *_samples_ms, *_note}], rls_overhead{state}{q1_pct,q3_pct}, pgstattuple{state}}` |
| `plans/<id>_<state>.json` | `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 중앙값 실행 (예: `q1_after.json`, `q2b_before.json`) |
| `leakproof.txt` | `pg_proc.proleakproof` 쿼리와 출력 |
| `bulk.json` | 적재 리포트 (`plan`, `counts`, `timings_s`, `total_s`, 헤더) |
| `top_queries.md` | (선택) 부하 시나리오 A 중 `pg_stat_statements` 상위 10 — 확장이 preload 돼 있을 때만 |
