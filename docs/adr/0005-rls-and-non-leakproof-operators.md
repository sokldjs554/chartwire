# ADR-0005 — RLS 아래에서는 leakproof 가 아닌 연산자가 인덱스를 못 쓴다: 검색은 SECURITY DEFINER 경계 뒤로

상태: 채택 (2026-09-04) · 관련: spec §4.6 (Q2a–Q2d), §4.1, ADR-0001, `docs/perf/README.md`, `docs/perf/leakproof.txt`

## 맥락

"지난 6개월 이 환자의 불면 언급"(§2 (7)) 은 `segment_search.text ILIKE '%불면증%'` 이다. 이 컬럼에는
`pg_trgm` GIN 인덱스(`ix_search_text_trgm`, 0007)가 있다. 그런데 `chartwire_app` 으로 실행하면 플래너가
GIN 을 쓰지 않고 테넌트 btree + `Filter: text ~~* '%불면증%'` 로 떨어진다(Q2b). 같은 쿼리를 owner 로
실행하면 `Bitmap Index Scan on ix_search_text_trgm` 이다(Q2c).

원인은 PostgreSQL 의 RLS 규칙이다. 정책이 걸린 테이블에서는 **정책 qual 이 사용자 qual 보다 먼저** 평가되어야
한다(사용자 함수가 오류 메시지 등으로 행 내용을 누설하는 것을 막기 위해). 예외는 `pg_proc.proleakproof = true`
인 함수·연산자뿐이다. 이 박스의 PostgreSQL 16 에서:

| 함수 | leakproof |
|---|---|
| `texteq`, `uuid_eq` | **true** — `tenant_id = …`, `= '문자열'` 은 인덱스 조건이 될 수 있다 |
| `texticlike` (`ILIKE`), `textlike` (`LIKE`), `textregexeq` (`~`) | false |
| `similarity`, `word_similarity` (`pg_trgm`) | false |
| `arraycontains` (`@>`) | false |
| `ts_match_vq` (`@@`) | false |

(실측 출력은 `docs/perf/leakproof.txt`, 쿼리 포함.) 즉 RLS 가 적용되는 역할에게는 `ILIKE`·트라이그램·
배열 포함·전문 검색 연산자 **전부**가 인덱스 조건이 될 수 없다. 인덱스를 아무리 만들어도 조용히 무시된다.

## 결정

1. `segment_search` 는 `ENABLE ROW LEVEL SECURITY` 만 걸고 `FORCE` 는 걸지 않는다(0006). 테이블 소유자
   `chartwire_owner` 는 정책에서 면제된다.
2. 검색은 `search_segments(p_query, p_mode, p_patient, p_limit)` **SECURITY DEFINER** 함수(0007, owner 소유)
   하나로만 실행한다. 함수는 `app.tenant_id` GUC 를 직접 읽어 `WHERE s.tenant_id = v_tenant` 를 강제하고,
   GUC 가 비어 있으면 `42501` 로 실패한다(fail-closed). `chartwire_app` 은 함수 EXECUTE 권한만 갖고
   테이블에 직접 `ILIKE` 를 던질 이유가 없다(`repo/search.search()` 가 유일한 호출부).
3. 자유 검색어는 3자 이상만 받는다(`MIN_TEXT_QUERY_CHARS`). 2음절(`불면`)은 트라이그램이 생기지 않아
   GIN 이 있어도 Seq Scan 이다(Q2a). 짧은 정규어는 `terms[]` 배열 GIN(`ix_search_terms`, `terms @> ARRAY[..]`)
   으로 검색한다(Q2d term) — 배열에는 자유 텍스트가 없다.
4. 이 결정의 효과는 `chartwire perf study` 가 Q2b(app, RLS) / Q2c(owner) / Q2d(app, 함수) 세 플랜을
   나란히 기록해 증명한다. README 의 수치는 그 JSON 에서만 온다.

## 결과

- 좋은 점: 테넌시 불변식(ADR-0001)을 깨지 않고 트라이그램 인덱스를 쓴다. 경계가 함수 하나라 감사가 쉽다.
- 비용: `segment_search` 가 FORCE 가 아니므로 owner 로 접속한 도구(마이그레이션·벌크 로더)는 정책을 보지
  않는다 — 런타임은 owner 로 접속하지 않는다는 ADR-0001 이 전제다. 관리형 PG 단일 역할 폴백
  (`CHARTWIRE_DB_SINGLE_ROLE=1`)에서는 owner = app 이라 면제가 사라지고 검색이 Q2b 경로(느림)로 떨어진다.
- 비용: SECURITY DEFINER 함수는 `search_path = public` 을 고정하고 인자를 파라미터로만 받는다(SQL 주입 표면 없음).
- 같은 이유로 **정책이 걸린 다른 테이블에도 `LIKE`/`~`/`@>` 조건에 기대는 인덱스를 만들지 않는다.**
  만들면 문서와 달리 동작하는 "장식 인덱스"가 된다.
- 벌크 로더에서 발견한 인접 사실: RLS 가 적용되는 역할은 `COPY FROM` 자체를 거부당한다
  (`FeatureNotSupportedError: COPY FROM not supported with row-level security`). 로더는 temp 스테이지 테이블에
  COPY 한 뒤 `INSERT … SELECT` 로 옮겨 `WITH CHECK` 정책을 통과시킨다(`synth/bulk.py`).
