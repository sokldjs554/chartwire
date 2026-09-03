# 장애 모드 표 (component × failure → behaviour → recovery → metric)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 절차는 `runbook.md`, 설계 근거는 ADR-0001/0002/0004. 메트릭 이름은 전부 `src/chartwire/ops/metrics.py::ALL_NAMES`에 있고 단위 테스트가 그 집합을 고정합니다.

| 구성요소 | 장애 | 동작 (설계) | 복구 | 관찰 메트릭 |
|---|---|---|---|---|
| 리코더 ↔ api WebSocket | Wi-Fi 끊김, 하드 드롭 | 서버 `ping` 15 s, `pong` 2회 누락 → `4000`. 세션은 `recording` 유지(재개 가능). 클라이언트 링 버퍼 150청크(30 s) | `hello{resume:true}` → `welcome{ack_seq, missing}` → 재전송. 링 버퍼 초과 간격 → `4008`, 세션 `ended`(부분 데이터, 감사) | `ws_resume_total{result}`, `ws_chunks_total{result="duplicate"}` |
| 리코더 ↔ api WebSocket | 같은 세션에 두 연결 | 에포크 펜싱: 새 `hello`가 `epoch` 증가, 이전 연결 `bye{superseded}` + `4409`, 오래된 에포크 프레임 폐기 | 자동(잠금 대기 없음) | `ws_chunks_total{result="stale"}` |
| 리코더 ↔ api WebSocket | 클라이언트가 크레딧 초과 전송 | 허용치 +20까지 수용, 초과 시 `4009` | 클라이언트 버그 수정; 서버는 손실 없음(ack 안 된 청크만 재전송) | `ws_credit`, `ws_chunks_total{result="rejected"}` |
| LedgerBatcher (`ws/ledger.py`) | PostgreSQL 쓰기 실패/지연 | 배치 future 실패 → `error 4503`, **ack 없음**(ack = durable). 청크는 클라이언트가 보관 | DB 복구 후 재전송; 배치 커밋 후 ack | `ledger_flush_seconds`, `ledger_pending_rows`, `ws_ack_latency_seconds` |
| Redis (`sess:*`, 스트림) | 재시작 / FLUSHALL / 분리 | 인제스트 fail-closed(`4503`, 3연속 실패 시 닫음), 뷰어 `viewer.degraded`, `/readyz` 503 | `sess:{sid}` 재수화, 컨슈머 그룹 재생성, stt-worker 원장 기반 rebuild (`runbook §3`) | `ws_chunks_total{result="rejected"}`, `stt_lag_chunks`, `/readyz checks.redis` |
| Redis (`alerts:sla`) | ZSET 손실 | 에스컬레이션 타이머만 손실; `risk_events`는 DB에 있음 | `risk_events` 미확인 행으로 ZSET 재구축 | `risk_unacked_over_sla`(DB 기준이라 계속 정확) |
| stt-worker | 프로세스 정지(SIGSTOP/크래시) | `stt:owner:{sid}` 리스 30 s 만료 → 다른 워커가 인수, `XAUTOCLAIM` idle >60 s 회수 | 자동; 중복 엔트리는 `stt_offsets.last_chunk_seq`로 스킵 | `stt_lag_chunks`, `segment_e2e_seconds` |
| stt-worker | 스트림 트리밍(`MAXLEN`)으로 간격 | `seq > last+1` 감지 → `audio_chunks` + 오브젝트 스토어에서 rebuild, 미원장 행은 200 ms 대기 | 자동 | `stt_lag_chunks`(상승 후 하강) |
| STT 어댑터 | 느림(시나리오 B) | `stt:lag` ↑ → `credit.compute`가 크레딧 감소 → `credit=0` 2 s 지속 시 `pause{stt_lag}` | 어댑터 복구 시 크레딧 자동 회복 | `ws_credit`, `stt_lag_chunks` |
| 뷰어 WebSocket | 느린 소비자 | `partial_q` 가득: 가장 오래된 partial 폐기 + `viewer.lagged`; `critical_q` 가득: `4013` 닫고 `from_seq` 재접속 → DB 리플레이 | 자동 | `ws_dropped_partials_total`, `ws_connections{kind="watch"}` |
| 아웃박스 워커 | 핸들러 예외 | `attempts+1`, `next_attempt_at = now + min(300 s, 2^attempts) ± 20 %`, `pending` | 자동 재시도(최소 1회 + 멱등 효과) | `handler_failures_total{event_type}`, `outbox_pending` |
| 아웃박스 워커 | 독약 메시지 (8회 실패) | `status='dead'` + `dead_letters` 행. 다른 행에는 영향 없음 | 원인 수정 후 `outbox dlq replay --id` (`attempts=0`) | `outbox_dead_total` |
| 아웃박스 워커 | 핸들러 도중 SIGKILL | `in_flight` 행이 `lease_until` 만료 → 30 s 주기 reclaim이 `pending`으로(attempts 불변). 커밋 뒤 죽었으면 `processed_events` PK 충돌로 스킵 | 자동 (`tests/chaos/test_worker_sigkill.py`) | `outbox_lag_seconds`, `outbox_pending` |
| 아웃박스 워커 | 워커 전부 다운 / 폴러 정지 | 행은 DB에 안전히 누적; 세션이 `transcribed`에 머묾 | 워커 기동; `outbox:wake` 없이도 1 s 폴링 | `outbox_lag_seconds` ↑, `handler_duration_seconds` 무변화 |
| 아웃박스 워커 | `done` 행 미정리 | `ix_outbox_pending` dead tuple → `claim_batch` 느려짐 | `outbox_prune` 티커(10 min, 24 h, 5,000행 배치), 필요 시 `VACUUM` | `outbox_pending` 대비 폴러 `claim_ms` 로그 |
| 아웃박스 워커 | `tenants.status` 변경 | 활성 테넌트 목록 30 s 캐시 → 최대 30 s 지연 | 자동 | `outbox_lag_seconds`(짧은 스파이크) |
| `note_draft` 핸들러 | LLM 제공자 오류/타임아웃 | 예외 → 재시도 정책; 동의 `ai_drafting` 없음 → `abstained`(재시도 아님) | 제공자 복구; `extractive` 제공자는 오프라인 동작 | `note_draft_seconds{provider}`, `note_status_total{status}`, `note_verify_reason_total{reason}` |
| `purge_run` / `purge_verify` | 오브젝트 스토어 삭제 실패, 잔여 행 | 단계별 멱등 재실행; 검증 실패 시 `purge_jobs.state='failed'` + DLQ | `purge run` 재실행 → `purge verify` → 영수증 갱신 | `purge_duration_seconds`, `outbox_dead_total` |
| 위험 경보 SLA | 미확인 경보가 마감 초과 | `alert_sla` 티커가 `escalation_level=1`, `risk.escalated` PUBLISH(한 단계) | 사람 확인(`POST /v1/alerts/{id}/ack`) (`runbook §5`) | `risk_unacked_over_sla`, `risk_alert_latency_seconds`, `risk_alert_publish_seconds` |
| PostgreSQL 파티션 | 다음 달 파티션 없음 | 행이 DEFAULT 파티션으로 감(유실 없음) | `partition_ensure` 티커(매시간 +1, +2개월) / `db partitions ensure` | `segments_default_partition_rows`(0이어야 함) |
| PostgreSQL 연결 풀 | 풀 고갈(20) | 요청 대기 → 타임아웃 → `4503`/503 | 느린 쿼리 확인(§4.6), api 수평 확장 | `db_pool_in_use` |
| PostgreSQL | RLS GUC 미설정 | `app.tenant_id` 없음 → 0행(fail-closed, `NULLIF` 정책) | 코드 결함: `tenant_tx` 경유 여부 확인 | (RLS 스위트 `tests/rls/`) |
| api / worker 배포 | SIGTERM | `/readyz` 503 → `bye{drain, ack_seq}` → 플러시 ≤20 s → `1012`; worker는 클레임 중단 → 진행 중 완료 ≤25 s | 리코더 다른 노드로 resume (`runbook §2`) | `ws_connections{kind}`, `ws_resume_total{result="ok"}`, `outbox_lag_seconds` |
| KEK / KMS | 언랩 실패 | `KekUnavailableError` → 핸들러 재시도; 파기된 DEK는 `DekDestroyedError`(정상, 재시도 아님) | KMS 권한/네트워크 복구 | `handler_failures_total{event_type}` |
