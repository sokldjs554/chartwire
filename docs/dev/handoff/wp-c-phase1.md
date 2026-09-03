# WP-C Phase 1 핸드오프 — stt-worker · 위험 경보 · SLA 티커

> 상태: **진행 중** (계획/체크리스트). 중단되면 §0 순서대로 이어서 작업한다.
> 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_c CHARTWIRE_TEST_REDIS_DB=3`
> 라이브 포트 8102(이 WP 는 HTTP 서버를 띄우지 않음; 서브프로세스 워커만 사용).

## 0. 체크리스트 (재개용)

- [ ] `risk/alerts.py` — `sla_deadline`, `create_events`(tx 안), `after_commit` 효과(ZADD/PUBLISH), `ack`, `escalate_due`
- [ ] `worker/handlers/alert_sla.py` — `INTERVAL_S`, `tick(ctx)`
- [ ] `stt/consumer.py` — `SessionConsumer`(XAUTOCLAIM, XREADGROUP, dedup, gap rebuild, NOGROUP 복구, 동의 게이트, final tx, end marker)
- [ ] `stt/worker.py` — `SttWorker`(discovery, owner lease, task per session), `build_adapter`, `main(settings)`
- [ ] `tests/integration/test_alerts.py`
- [ ] `tests/integration/test_stt_worker.py` (finals/created_at/seq, dedup, gap rebuild, FLUSHDB, consent gates, end marker → transcribed + outbox)
- [ ] `tests/integration/test_stt_worker_chaos.py` (SIGSTOP/SIGCONT + 두 번째 워커 XAUTOCLAIM)
- [ ] `docs/stt-providers.md`, `docs/risk-detection.md` §8 갱신
- [ ] ruff / ruff format / mypy, 최종 테스트, 이 문서 완성
