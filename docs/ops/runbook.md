# 운영 런북 (chartwire)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 api / worker / stt-worker 세 프로세스를 운영하는 사람을 위한 절차서입니다. 수치가 필요한 곳은 `/metrics`(Prometheus 텍스트)와 `chartwire outbox stats` 출력을 기준으로 하며, 문서에 손으로 적은 숫자는 없습니다.

## 0. 전제: 전달 보장은 "최소 1회 + 멱등"이다

- 도메인 변경과 `outbox_events` 삽입은 **같은 트랜잭션**에서 일어납니다(§7.1). 따라서 "이벤트가 유실됐다"는 상황은 존재하지 않고, "아직 처리되지 않았다"만 존재합니다.
- 핸들러는 **최소 1회(at-least-once)** 실행됩니다. 핸들러의 DB 효과와 `processed_events(handler, event_id)` 삽입이 한 트랜잭션으로 커밋되므로, 커밋 뒤 `mark_done` 전에 워커가 죽어도 재전달 시 PK 충돌로 "이미 처리됨"으로 건너뜁니다. PUBLISH · 오브젝트 삭제 · `ZADD` 같은 DB 밖 효과는 구성상 멱등입니다.
- 이 시스템은 **정확히 1회(exactly-once)를 약속하지 않습니다.** 중복 실행이 보이면 그것은 설계된 동작이며, 중복 *효과*가 보일 때만 버그입니다.
- 재시도 간격: 실패 횟수 `attempts`(실패 직후 값 기준)에 대해 `min(300 s, 2^attempts) ± 20 %`. 8번째 실패에서 `status='dead'` + `dead_letters` 행 + `outbox_dead_total` 증가(§7.1, `outbox/backoff.py`, `outbox/dlq.py`).

## 1. 프로세스와 신호

| 프로세스 | 역할 | 헬스 | 드레인 예산 |
|---|---|---|---|
| `chartwire serve api` | REST + WebSocket 인제스트/뷰어 | `/healthz`(항상 200), `/readyz`(드레인·의존성 실패 시 503), `/metrics` | 20 s (§6.4-7) |
| `chartwire serve worker` | 아웃박스 폴러 + 티커(`alert_sla`, `partition_ensure`, `outbox_prune`, `session_reaper`) | `/healthz`, `/readyz`, `/metrics` (별도 포트) | 25 s (§7.3) |
| `chartwire serve stt-worker` | `sess:{sid}:chunks` 소비, 최종 세그먼트 + 위험 발화 트랜잭션 | 동일 | 25 s |
| `chartwire serve all --embedded` | 위 셋을 한 프로세스 안에서(Render 무료 티어 전용) | 동일 | 25 s |

- `SIGTERM`/`SIGINT` → `ops.drain.Drainer.begin()` → 등록된 훅이 한 번씩 실행(readyz 503, 새 클레임 중단, 리코더에 `bye{reason:'drain'}`) → 진행 중 작업(`Drainer.track()`)이 0이 되면 종료, 예산 초과 시 강제 종료(`drain deadline reached` 경고 로그).
- 컨테이너 `HEALTHCHECK`는 `/healthz`를 봅니다. 드레인 중에도 200이므로 오케스트레이터가 드레인 중인 프로세스를 죽이지 않습니다. 로드밸런서는 `/readyz`를 봅니다(ALB deregistration delay 30 s ≥ 드레인 예산).

## 2. 배포 / 드레인 절차

1. 새 태스크(이미지)를 먼저 띄우고 `/readyz`가 200이 될 때까지 기다립니다.
2. 이전 태스크에 `SIGTERM`. 즉시 `/readyz`가 503으로 바뀌어 LB 트래픽이 빠집니다.
3. api: 열린 리코더 연결마다 `bye{reason:'drain', ack_seq}` 전송 → 원장 배처 플러시(≤20 s) → `1012`로 닫음. 리코더는 새 노드에 `hello{resume:true, last_sent_seq}`로 붙고 `welcome{ack_seq, missing}`로 이어집니다(무손실은 시나리오 E에서 측정).
4. worker: 새 `claim_batch` 중단 → 진행 중 핸들러 완료(≤25 s) → 종료. 미완료 `in_flight` 행은 `lease_until` 만료 뒤 `stuck reclaim`(30 s 주기)이 `pending`으로 되돌립니다. **attempts는 증가하지 않습니다.**
5. stt-worker: `stt:owner:{sid}` 리스 갱신을 멈추면 30 s 뒤 다른 워커가 세션을 넘겨받고 `XAUTOCLAIM`으로 미확인 엔트리를 회수합니다.
6. 확인: 새 태스크의 `ws_connections{kind="ingest"}`가 올라오고 이전 태스크 값이 0, `outbox_lag_seconds`가 정상 범위로 복귀, `ws_resume_total{result="ok"}` 증가.

롤백은 같은 절차를 이미지만 바꿔 반복합니다. 마이그레이션이 포함된 배포는 `chartwire db upgrade`를 먼저(전방 호환 DDL만; §4.3), 코드 배포를 나중에 합니다.

## 3. 시나리오 A — Redis 손실 (재시작 / FLUSHALL / 네트워크 분리)

**증상**
- api 로그에 `dependency_unavailable` 급증, 리코더가 `error 4503(retryable)`을 받고 ack가 멈춤. 3회 연속 실패 시 연결이 닫힘. `ws_chunks_total{result="rejected"}` 증가.
- 뷰어는 `viewer.degraded`를 받음. `/readyz`의 `checks.redis`가 `fail`/`timeout` → 503.
- stt-worker: `XREADGROUP`이 `NOGROUP` 오류 → `stt_lag_chunks`가 0으로 떨어짐(가짜 안정처럼 보임에 주의).

**설계된 동작 (ADR-0002)**
- 인제스트는 **fail-closed**입니다. Redis에 못 쓰면 ack를 내지 않습니다. 원장(PostgreSQL `audio_chunks`)이 유일한 진실이고 Redis는 재구축 가능한 캐시이므로 데이터 유실은 없습니다. 클라이언트 링 버퍼(30 s)가 재전송을 담당합니다.
- `sess:{sid}` 해시가 사라지면 다음 `hello`에서 `sessions`/`audio_chunks`로부터 재수화(rehydrate)합니다. 컨슈머 그룹은 `XGROUP CREATE … MKSTREAM`으로 다시 만들어집니다.
- stt-worker는 `stt_offsets.last_chunk_seq` 이후의 청크를 `audio_chunks` + 오브젝트 스토어에서 다시 읽어 복구합니다(§7.4 rebuild). 이 경로는 `tests/integration`의 FLUSHALL 시나리오로 검증됩니다.

**조치**
1. Redis 복구(재시작/페일오버). 비밀번호·TLS 설정이 바뀌었으면 `CHARTWIRE_REDIS_URL` 확인.
2. `/readyz`가 200으로 돌아오는지 확인. 돌아오지 않으면 api 프로세스 재시작(연결 풀이 죽은 소켓을 물고 있을 때).
3. 리코더는 자동 재접속합니다. `ws_resume_total{result="ok"}`가 올라가고 `ws_ack_latency_seconds`가 정상화되는지 확인.
4. stt-worker 로그에서 `rebuild`가 세션마다 한 번씩 찍히고 `stt_lag_chunks`가 상승했다가 내려오는지 확인. 계속 0이면 `stt:active` SET이 비어 있는 것 — 진행 중 세션의 id를 `SADD stt:active`로 다시 넣습니다(`recording` 상태 세션 목록은 `sessions` 테이블).
5. 알림 SLA ZSET(`alerts:sla`)은 손실됩니다. `risk_events WHERE acknowledged_at IS NULL AND sla_deadline_at IS NOT NULL`로 다시 `ZADD`하는 `chartwire outbox stats --rebuild-sla`(Phase 1) 또는 수동 스크립트를 실행합니다. 그때까지 `risk_unacked_over_sla`는 PostgreSQL 기준으로 계속 계산됩니다(에스컬레이션만 지연).
6. 사후: `ws_chunks_total{result="rejected"}` 총량, 영향 세션 수, 재접속까지 걸린 시간을 기록합니다.

## 4. 시나리오 B — 아웃박스 지연 / DLQ 증가

**증상**
- `outbox_lag_seconds`(가장 오래된 `pending` 행의 나이)가 재시도 상한(300 s)을 넘어 계속 상승, `outbox_pending` 증가.
- `handler_failures_total{event_type=…}`가 특정 이벤트 타입에서만 증가, `outbox_dead_total` 증가.
- 콘솔에서 세션이 `transcribed`에 머무르고 `drafted`로 넘어가지 않음(=`session.transcribed` 핸들러 지연).

**우선 구분: 지연인가, 실패인가**

| 관찰 | 뜻 | 다음 단계 |
|---|---|---|
| `outbox_pending` ↑, `handler_failures_total` 평탄, `handler_duration_seconds` p95 ↑ | 처리량 부족(느린 핸들러: LLM 제공자, 파기 검증의 오브젝트 스토어 스캔) | 워커 수평 확장 — `claim_batch`가 `FOR UPDATE SKIP LOCKED`라 다중 워커 안전. 테넌트별 폴링 비용은 시나리오 H 수치 참조. |
| `outbox_pending` ↑, `handler_failures_total` 평탄, `handler_duration_seconds` 평탄 | 폴러가 돌지 않음(워커 죽음, `tenants.status` 캐시, DB 연결 끊김) | `/readyz`와 워커 로그. `outbox:wake` PUBLISH가 오지 않아도 1 s 폴링은 계속돼야 함. |
| `handler_failures_total{event_type=X}` ↑ | X 핸들러의 의존성 장애(KMS, 오브젝트 스토어, 동의 게이트) 또는 독약 메시지 | 아래 "DLQ 처리". |
| `in_flight` 행이 `lease_until` 지나서도 남음 | `stuck reclaim` 티커가 멈춤 | 워커 재시작. 30 s 안에 `pending`으로 돌아와야 함. |

**DLQ 처리**
1. `chartwire outbox dlq list [--tenant …]`로 `event_type`, `attempts`, `last_error`, `died_at` 확인. `last_error`는 예외 타입 + 메시지(전화번호/주민번호 형태는 `[REDACTED]`, 2,000자 제한)이며 전사 텍스트는 절대 담기지 않습니다.
2. 같은 오류가 여러 테넌트에서 동시에 → 공통 의존성 장애. 원인 복구 뒤 `chartwire outbox dlq replay --id <outbox_event_id>`. replay는 `attempts=0, status='pending', next_attempt_at=now()`로 되돌리고 `dead_letters.replayed_at`을 찍습니다. 핸들러가 멱등이므로 부분 실행된 이벤트를 다시 돌려도 안전합니다.
3. 한 행만 반복 실패(독약 메시지) → 페이로드(`session_id`/`patient_id` 등 id만 있음)로 대상 애그리거트 상태를 확인. 예: 이미 `purged` 세션에 대한 `session.transcribed` → 핸들러가 `abstain`으로 끝내야 정상이며 예외라면 코드 결함. 수정 배포 후 replay.
4. `purge.completed` 이벤트의 `purge_verify` 실패는 **파기 영수증에 `failed`가 남는** 사안입니다. replay 전에 `chartwire purge verify --job <id>`를 수동 실행해 잔여 행/오브젝트를 확인하고, 잔여가 있으면 `purge run`을 다시 돌립니다(멱등).
5. 처리 뒤 `outbox_dead_total` 증가가 멈추고 `outbox_lag_seconds`가 내려오는지 확인. `dead` 행은 `outbox_prune`이 지우지 않으므로(`done`만 삭제) 조사 기록으로 남습니다.

**`ix_outbox_pending` 비대화**
`outbox_prune`(10분 주기, 테넌트별 5,000행 배치)이 24 h 지난 `done` 행을 지웁니다. 티커가 오래 멈춘 뒤에는 인덱스 dead tuple 때문에 `claim_batch`가 느려질 수 있습니다(§4.6 Q4 관찰). `VACUUM outbox_events` 후 정상화 여부를 `handler_duration_seconds`가 아닌 폴러 로그의 `claim_ms`로 봅니다.

## 5. 시나리오 C — SLA를 넘긴 미확인 위험 경보

**배경**: stt-worker가 최종 세그먼트 트랜잭션 안에서 위험 발화를 감지하면 `risk_events` 행을 커밋하고, 그 다음 `alerts:sla`에 마감(심각도 3: +60 s, 2: +300 s)을 `ZADD`합니다. worker의 `alert_sla` 티커(1 s)가 마감이 지난 항목을 꺼내 `escalation_level=1`, `escalated_at`을 기록하고 `risk.escalated`를 세션 채널과 `tenant:{tid}:alerts`에 PUBLISH 합니다. 한 단계만 있습니다.

**증상**
- `risk_unacked_over_sla` > 0이 지속. 이 게이지는 PostgreSQL(`risk_events WHERE acknowledged_at IS NULL AND sla_deadline_at < now()`) 기준이므로 Redis 상태와 무관하게 참입니다.
- `risk_alert_latency_seconds` p95 상승(커밋 → PUBLISH 지연) 또는 콘솔 알림 패널이 비어 있음.

**진단 순서**
1. **뷰어에게 도달했는가**: 세션의 `sess:{sid}:viewers` SET이 비어 있으면 아무도 보고 있지 않은 것. 임상의 콘솔이 열려 있는지 확인. 뷰어가 있는데 못 받았다면 `ws_dropped_partials_total`은 무관(partial만 버림) — 경보는 `critical_q`로 가며 가득 차면 `4013`으로 닫히고 뷰어가 `from_seq`로 재접속하므로 DB 리플레이에 경보가 포함됩니다.
2. **에스컬레이션이 됐는가**: `risk_events.escalated_at`이 NULL이면 `alert_sla` 티커가 멈췄거나 `alerts:sla` ZSET이 손실된 것(시나리오 A). 워커 로그와 `ZCARD alerts:sla`.
3. **확인(ack)이 됐는가**: `POST /v1/alerts/{id}/ack`가 403이면 RBAC/테넌트 문제(감사 로그 `alert.ack` 부재). 401이면 토큰 만료.

**조치**
- 사람 우선: 담당 임상의에게 연락(이 시스템은 외부 호출/SMS 통합이 없습니다 — `docs/limitations.md`). 콘솔 알림 탭에서 확인 처리하면 `risk.ack`가 브로드캐스트되고 게이지가 내려갑니다.
- ZSET 손실이면 §3-5의 재구축을 실행. 티커 정지면 워커 재시작.
- 사후 검토에서 "경보가 늦게 왔다"와 "경보가 늦게 확인됐다"를 반드시 구분해 기록합니다. 전자는 `risk_alert_latency_seconds`, 후자는 `acknowledged_at - created_at`.

## 6. 파티션 점검

`transcript_segments`는 `created_at` 월 단위 RANGE 파티션이고 DEFAULT 파티션이 있습니다. worker의 `partition_ensure` 티커가 매시간 `ensure_segment_partition(m)`를 현재+1, +2개월에 대해 호출하고 DEFAULT 파티션 행 수를 `segments_default_partition_rows`로 내보냅니다.

- 게이지가 0이 아니면 시계가 어긋난 세션이거나 티커가 두 달 이상 멈춘 것입니다. 원인을 잡은 뒤 `chartwire db partitions ensure`로 파티션을 만들고 DEFAULT 행을 옮깁니다(`INSERT … SELECT` + `DELETE`, 테넌트별 `app.tenant_id` 설정 필요 — FORCE RLS).
- 파티션은 지우지 않습니다. 보존 정책은 파티션 드롭이 아니라 동의 철회 파기(세션 단위)와 서명 진료기록 보존(10년)으로 표현됩니다(ADR-0004).

## 7. 자주 보는 로그 이벤트

| 이벤트 | 프로세스 | 뜻 |
|---|---|---|
| `drain started` / `drain deadline reached` | 전체 | 드레인 시작 / 예산 초과 강제 종료 |
| `readiness check timed out` / `readiness check raised` | 전체 | `/readyz` 의존성 프로브 실패(어느 프로브인지 `check` 필드) |
| `drain hook raised` | 전체 | 드레인 훅 하나가 예외 — 나머지 훅과 드레인은 계속됨 |
| `outbox claimed` / `outbox handler failed` / `outbox dead` | worker | 배치 클레임 / 재시도 예약 / DLQ 이동(id·event_type·attempts만) |
| `rebuild` | stt-worker | 스트림 간격 감지 → 원장에서 재구축 |

로그에는 전사 텍스트·이름·토큰·키가 절대 나오지 않습니다(`core.logging.redact_phi`; 통합 테스트가 전체 실행 로그를 grep 합니다, §0-9).
