# ADR-0001 — 런타임은 `chartwire_app` 한 역할, 테넌시는 RLS, 교차 테넌트 작업은 테넌트 순회

상태: 채택 (2026-09-02) · 관련: spec §0.7, §4.1, §7.1, `docs/db/schema.md`

## 맥락

30개 의원이 한 PostgreSQL 에 산다. 애플리케이션 코드에서 `WHERE tenant_id = …` 를 한 번만 빼먹어도
그 순간 다른 의원의 상담 기록이 보인다(§2 (5)). 워커는 모든 테넌트의 outbox 를 처리해야 하고,
"편의상" `BYPASSRLS` 역할을 하나 두고 싶은 유혹이 가장 큰 곳이 바로 워커다.

## 결정

1. 런타임 프로세스(api, worker, stt-worker)는 **오직 `chartwire_app`**(LOGIN, `NOBYPASSRLS`, owner 의 멤버가 아님)으로만 접속한다.
   `chartwire_owner` 는 마이그레이션·스키마 덤프·테스트 픽스처 전용이며, superuser 는 `db bootstrap-roles`, 성능 연구, "superuser 는 새어 나온다" 테스트에서만 쓴다.
2. 모든 테넌트 테이블(파티션 하나하나 포함)에 `ENABLE` + `FORCE ROW LEVEL SECURITY` 와 정책
   `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` 를 건다. GUC 는 항상 `set_config(…, true)`
   (트랜잭션 로컬)로만 설정한다(`db.tenant.tenant_tx`). 컨텍스트가 없으면 NULL → 0행(fail-closed).
3. 교차 테넌트 작업(outbox 폴러, SLA 티커, 파티션 보장, 세션 리퍼)은 **테넌트를 순회**한다:
   `SELECT id FROM tenants WHERE status='active'`(30초 캐시) → 테넌트마다 `app.tenant_id` 를 설정한 트랜잭션 하나.
4. 유일한 예외는 SECURITY DEFINER 함수 두 개다. `search_segments()` 는 owner 로 실행되어 `segment_search`(ENABLE only, FORCE 아님)
   의 트라이그램 인덱스를 쓸 수 있고, `ensure_segment_partition()` 은 파티션을 만들면서 RLS·정책·GRANT 를 같이 건다.
   둘 다 함수 안에서 `app.tenant_id` 를 직접 읽어 테넌트 범위를 강제한다.

## 결과

- 좋은 점: 격리가 애플리케이션 규율이 아니라 데이터베이스 불변식이 된다. `tests/rls/` 는 원시 SQL 로 `WHERE tenant_id` 없이 조회해 0행을 확인하고,
  같은 쿼리를 superuser 로 실행해 두 테넌트가 보이는 것(픽스처가 진짜 비우회 역할임)을 증명한다.
- 비용: 테넌트별 폴링. 테넌트 N개면 1초마다 N번의 짧은 트랜잭션. 300 의원 규모의 비용은 시나리오 H 로 **측정**해 README 에 싣는다(추정치 금지).
- 비용: RLS 정책 안에서는 leakproof 가 아닌 연산자(`ILIKE`, `similarity` 등)에 인덱스를 못 쓴다(§4.6 Q2b). 그래서 검색은 SECURITY DEFINER 경계 뒤로 옮겼다.
- 관리형 PG(단일 역할: `chartwire_app` 역할이 없어 `helpers.app_role_exists()` 가 False)에서는 owner 가 곧 app 이지만 `FORCE` 덕분에 격리는 유지된다. `segment_search` 도 이 모드에서만 0006 이 `FORCE` 를 걸므로 owner 면제가 사라져 검색이 RLS 경로로 떨어진다(느림, 문서화). 두 역할 배포에서는 `segment_search` 가 `ENABLE` 만이고 런타임이 소유자가 아니라는 §0.7 이 그 면제를 좁게 유지한다.
