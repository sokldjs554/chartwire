# 적대적 리뷰 수정 (review-fixes)

> 외부 리뷰가 확정한 지적 30건을 적용한 기록. **테스트나 임계값을 낮춰서 통과시킨 곳은 없다** — 코드를 고쳤거나,
> 문서 주장을 산출물에 맞게 정정했다. 재측정이 필요한데 이 세션에서 재측정을 금지당한 항목은 §4 에 "고치지 않고
> 정직하게 적어 둔 것" 으로 남긴다. 실행 환경은 [`../AGENT_ENV.md`](../AGENT_ENV.md).

## 0. 게이트 (전부 통과)

```
pytest tests/unit -q                                                    827 passed
tests/ws (b/2)                                                          149 passed
tests/rls + test_migrations (a/1)                                        38 passed
test_ws_{state,ingest,watch,drain} (b/2)                                 36 passed
test_alerts + test_stt_worker{,_chaos} (c/3)                             16 passed
test_notes_{service,rest} (d/4)                                          28 passed
test_rbac_matrix + test_api_* + test_phi_logs + test_purge_pipeline (e/5) 30 passed
test_seed + test_bulk_small (f/6)                                         8 passed
test_outbox_* + test_ops_routes + tests/chaos (g/7)                      22 passed
ruff check / ruff format --check (src tests scripts)                     clean
mypy (ws/core.py, ws/codec.py, notes/verifier.py, crypto/, outbox/)      clean
python scripts/readme_numbers.py --check                                 최신
python scripts/check_links.py                                            45개 문서 정상
sha256sum -c src/chartwire/eval/data/FROZEN.txt                          OK
make schema-check                                                        schema matches
pytest infra/cdk/tests -q                                                13 passed
python -m pip check                                                      clean
```

CI 의 `docker` 잡에 `docker compose up -d --build --wait` + `/readyz`·`/console` 확인 단계를 추가했다
(빌드 박스에는 Docker 데몬이 없어 여기서 직접 띄우지는 못했다 — 그래서 README 의 컨테이너 경로 문장도
"CI 가 매번 실제로 띄운다" 로만 주장한다).

`chartwire eval {risk,grounding,inject,injection,paraphrase}` 를 임시 디렉터리로 다시 돌려 **계산형 평가 6종의 본문이
헤더를 뺀 바이트 단위로 `docs/eval/*.json` 과 같음**을 확인했다 — 이 수정들이 README 표 ③ 의 숫자를 바꾸지 않았다는
증거이자, §4 의 provenance 항목의 근거다.

## 1. 동작 결함 (코드 수정 + 회귀 테스트)

| # | 파일 | 결함 | 수정 | 회귀 테스트 |
|---|---|---|---|---|
| 1 | `ws/core.py`, `ws/ingest.py` | 셸이 **reorder 버퍼에 살아 있는 seq** 의 페이로드까지 버렸다. 코어는 그 seq 의 재전송을 "중복" 으로 세는데, 셸은 중복이면 캐시를 지웠다 → 구멍이 메워지는 순간 `Store` 가 바이트 없이 발행되고 청크가 **조용히 유실**, `contig_seq` 가 이미 지나가 `missing` 이 다시 요청할 수 없어 `ack_seq` 가 영구 정지 | `IngestCore.buffered(seq)` 를 노출하고 `_on_frame` 의 pop 을 그것으로 게이트. "셸은 코어가 `Store` 할 수 있는 seq 의 바이트를 정확히 보유한다" 가 불변식. `_run` 의 `payload is None` 은 이제 `continue` 가 아니라 4503 으로 연결을 끊어 녹음기가 `ack_seq` 부터 resume 하게 한다(옛 주석의 "nack 가 복구한다" 는 거짓이었다) | `test_ws_ingest.py::test_duplicate_of_a_buffered_chunk_keeps_its_payload` (수정 없이는 `{'code': 4503}`) |
| 2 | `stt/consumer.py:_handle_entries` | `_rebuild` 가 `stop()` 으로 중단돼도 그대로 다음 엔트리를 처리해 `last_chunk_seq` 를 구멍 위로 올렸고, `upsert_stt_offset` 의 `GREATEST` 가 그것을 영구화 → 건너뛴 청크는 **어느 워커도 전사하지 않는다** | `_rebuild` 뒤 구멍이 남아 있으면 `return False` — 엔트리를 unack 으로 두어 XAUTOCLAIM 이 다음 소유자에게 재배달 | (아래 5번 테스트가 같은 경로를 지난다) |
| 3 | `stt/consumer.py:_rebuild` | 마감도 소유권 확인도 없어, 영구적인 원장 구멍(에포크 펜싱으로 `xadd_chunk` 가 조기 반환한 seq) 하나가 컨슈머를 200 ms 폴링 루프에 **영원히** 가둔다 — 리스와 `max_sessions` 슬롯을 함께 잡은 채 | `rebuild_deadline_s`(기본 30 s) 추가: 매 패스마다 `_check_owner()`(정체된 옛 소유자는 `OwnershipLost` 로 빠진다), 마감 초과 시 error 로그 + `rebuild_gaps_skipped` + 그 seq 를 건너뛰고 진행(진행이 있으면 예산 리셋) | — |
| 4 | `stt/worker.py:run` | 디스커버리 루프에 패스별 예외 가드가 없어, Redis 장애 조치 한 번이 루프를 죽이고 모든 리스를 놓아준 뒤 **프로세스는 살아 `/readyz` 200** — 세션이 다시는 발견되지 않는데 아무 신호가 없다 | `outbox.poller.Poller.run` 과 같은 가드(틱 대기는 가드 밖). 더해 연속 실패 `DISCOVERY_FAILURES_BEFORE_DRAIN=10` 이면 `drainer.begin("discovery_failed")` 로 스스로 드레인해 오케스트레이터가 재시작하게 한다 | `test_stt_worker.py::test_discovery_survives_a_failing_pass` |
| 5 | `stt/consumer.py:_recover_group` | 녹음기의 end 마커와 그 배달 사이에 Redis flush 가 끼면 세션이 **영구히 `ended` 로 좌초**: 그룹이 `$` 로 재생성되어 마커가 사라지고, `ended` 는 `TERMINAL_STATES` 가 아니며, `_idle_should_exit` 는 컨슈머 자신이 다시 넣은 `stt:active` 때문에 발화하지 않고, `session_reaper` 는 `recording`/`paused` 만 본다 | `_recover_group` 이 `ended` 여부를 반환하고, `_run`/`_autoclaim` 이 그때 `_on_end(None)` 을 실행(`_xack` 는 `None` 을 허용한다) → 플러시·`transcribed`·아웃박스 이벤트·`SREM stt:active`. `SADD stt:active` 는 **조건 없이** 유지한다: `SttWorker.owns` 가 사라진 리스를 그 멤버십으로만 되찾으므로 빼면 rebuild 중에 소유권을 잃는다. 슬롯은 `_on_end` 가 직접 SREM 해서 반납한다 | `test_stt_worker.py::test_flush_after_the_end_marker_still_transcribes` |
| 6 | `ws/pubsub.py` | `subscribe` 의 docstring 이 "Redis 가 구독을 확인한 뒤에 반환한다" 고 주장했지만 redis-py 의 `PubSub.subscribe` 는 바이트만 쓰고 응답을 읽지 않는다 → 뷰어의 subscribe-before-replay 순서 보장이 성립하지 않았다 | 주장을 **참으로 만들었다**: pubsub 을 `ignore_subscribe_messages=False` 로 만들고(‼ redis-py 는 생성자 플래그가 켜져 있으면 호출 인자와 무관하게 subscribe 프레임을 버린다), 리더가 확인 프레임을 채널별 `asyncio.Event` 로 풀고, `subscribe` 가 명령을 쓰기 **전에** 대기자를 무장한 뒤 2 s 상한으로 기다린다. `_resubscribe` 는 리더 태스크 자신이라 기다릴 수 없어(교착) 그대로 두고 그 이유를 docstring 에 적었다 | 기존 `tests/integration/test_ws_watch.py` (부수 효과: ws 통합 그룹이 109 s → 53 s) |

## 2. 보안

| # | 파일 | 결함 | 수정 |
|---|---|---|---|
| 7 | `db/engine.py` | 엔진이 `hide_parameters` 없이 만들어져, 모든 SQLAlchemy 문 오류가 바인드 파라미터를 문자열에 담았다 — `segment_search.text` 는 **평문 전사**이고 `/v1/search` 는 임상의의 자유 검색어를 바인드한다. 그 문자열이 `stt/worker.py` 의 `log.exception` 과 `api/app.py` 의 `exc_info=exc` 로 로그에 간다(§0.9 위반) | 두 엔진 팩토리에 `hide_parameters=True`. `test_phi_logs.py::test_db_errors_never_render_bound_parameters` 가 실패하는 문에 마커를 바인드해 traceback 과 로그 레코드 어디에도 없음을 확인(기존 테스트는 성공 경로만 봤다) |
| 8 | `api/middleware.py`, `redis/idempotency.py` | 멱등성 레코드가 테넌트 키로만 저장·재생돼, 같은 테넌트의 **아무 주체**가 남의 키를 제시하면 라우트(=`rbac.require`)를 건너뛰고 그 응답을 받았다 — `staff` 가 admin 의 파기 영수증을 202 로 | `Record.sub` 추가(=키를 claim 한 주체). 불일치는 본문 불일치와 같은 422 CW-4222. 키스페이스는 스펙 §5 의 `idem:{tenant}:{key}` 그대로. `test_api_middleware.py` 의 해당 단언은 **취약 동작을 고정하고 있었으므로** 수정된 규칙으로 바꾸고, 거부된 재생이 라우트를 실행하지도 않았음을 함께 단언 |
| 9 | `migrations/versions/0006_rls.py` | 평문 전사를 담은 유일한 테이블 `segment_search` 가 `ENABLE` 만이라, **단일 역할 배포(Render: 런타임 = 소유자)** 에서는 테넌트 격리가 전혀 없었다 — ADR-0001/0005/SPEC §4.1/schema.md 가 이미 "그때는 면제가 사라진다" 고 적어 둔 것과 정반대 | `enable_rls("segment_search", force=not app_role_exists())`. 두 역할 배포(=측정이 이뤄진 박스)는 그대로라 **표 ② 의 Q2c/Q2d 수치에 영향이 없다**; 단일 역할에서는 문서가 이미 약속한 느린 경로가 실제로 일어난다. `tests/rls/test_superuser_leaks.py` 의 소유자 단언은 빈 테이블에 대한 것이라 무의미(vacuous)했으므로 두 테넌트의 행을 먼저 색인하고 `== 2` 를 단언하도록 고쳤다 |
| 10 | `db/repo/purge.py`, `purge/verify.py`, `eval/purge_eval.py` | 파기가 `name_hmac`(블라인드 인덱스)를 남겨, 영수증이 "파기됨" 이라고 한 뒤에도 `GET /v1/patients?name=김철수` 로 **이름이 확인 가능**했다. 이 키는 전역 KEK master 에서 파생되므로 DEK crypto-shred 과 무관하다 | `destroy_patient_dek` 이 `name_hmac`·`birth_year`·`sex` 도 NULL 로(컬럼 전부 nullable). SQL NULL 은 어떤 digest 와도 같지 않아 Q6 쿼리 변경 없이 purged 환자가 빠진다. `purge_verify` 의 `patient_shredded` 가 `name_hmac is None` 을 함께 요구하고, `purge_eval._residuals` 가 `consent_state='purged' AND name_hmac IS NOT NULL` 행을 쓸어 `residual_rows` 에 넣는다 |
| 11 | `api/routers/alerts.py`, `risk/alerts.py` | `POST /v1/alerts/{id}/ack` 에 세션 소유 검사가 없어, `risk_events.id`(순차 bigint)를 훑으면 **다른 임상의 환자의 자살 위험 경보를 읽고 영구히 확인 처리**할 수 있었다(`ZREM alerts:sla` 로 에스컬레이션까지 해제). `GET /v1/alerts` 와 WS ack 는 검사한다 | `acknowledge_in_tx(..., require_own_session=True)` — 같은 트랜잭션 안에서 `sessions.clinician_id == by` 를 확인(TOCTOU 없음). 라우터가 `principal.role == "clinician"` 일 때 켠다. `test_api_sessions.py` 에 다른 임상의의 ack 가 404 이고 `alerts:sla` 가 그대로임을 단언 |

## 3. 재현성 · 데드 코드 · 품질

- **`make test-integration` 이 결정적으로 실패했다** — `perf/study.py::pgstattuple` 이 `CREATE EXTENSION` 을 **커밋**해,
  같은 DB 에서 뒤에 도는 스키마 덤프 동등성 테스트가 영구히 깨졌다(그리고 `make schema-dump` 로 "고치면" CI `migrations`
  잡이 깨진다). `conn.commit()` → `conn.rollback()`(`leakproof_report` 와 같은 모양; `CREATE EXTENSION` 은 트랜잭션이고
  `IF NOT EXISTS` 라 기존 설치는 건드리지 않는다). 이미 오염된 `chartwire`, `chartwire_test{,_a,_f,_i}` 에서
  `DROP EXTENSION pgstattuple` 을 실행했다.
- **`.env.example` 이 빌드 박스 전용 슈퍼유저(`app:app`)를 고정**해 `dev_up.sh` 의 이식성 있는 기본값을 죽은 코드로
  만들었다. `.env.example` 과 `core/config.py` 의 기본값을 CI 가 쓰는 스톡 값(`postgresql://postgres@localhost:5432/postgres`)으로
  바꾸고, 빌드 박스 값은 추적되지 않는 `.env`(`dev_up.sh:7` 이 먼저 읽는다)에 두었다. **이 박스에서 테스트를 돌릴 때는
  `set -a; . ./.env.example; . ./.env; set +a` 로 두 파일을 모두 읽어야 한다** (`AGENT_ENV.md` §Test run 도 그렇게 고쳤다).
- **`docker compose up` 이 뜨지 않았다** — `scripts` 명명 볼륨의 마운트 지점을 이미지가 만들지 않아 root:root 로 생기고,
  uid 10001 로 도는 `migrate` 가 데모 스크립트를 못 써서 모든 서비스가 `depends_on` 에 걸려 시작하지 못한다. Dockerfile 의
  `mkdir -p` 에 `/var/lib/chartwire/scripts` 추가(`chown -R` 이 이미 덮는다). CI 의 `docker` 잡은 `compose config -q` 만
  하므로 이 부류를 잡지 못한다(§4).
- **worker/stt-worker 컨테이너가 영구 unhealthy** — 이미지 HEALTHCHECK 가 8000 을 찌르는데 두 워커는 9001/9002 의 ops
  서버만 연다. compose 에 서비스별 `healthcheck` 오버라이드 추가(포트를 문서화하는 효과도 있다).
- **죽은 코드 제거** — `SessionState.publish_ctl`(호출자 0; `purge/pipeline.py` 의 리터럴 `'{"t":"purge"}'` 를 이미
  선언돼 있던 `CTL_PURGE` 상수로 바꿔 ctl 봉투의 단일 출처를 만들었다), `repo/search.count_for_session`,
  `repo/risk.list_for_session`, `LedgerBatcher.flush_now`("tests, drain" 이라는 docstring 이 거짓이었다 — 드레인 플러시는
  `_run` 의 루프 뒤 `_safe_flush` 가 보장한다). `wp-b-phase1.md` 의 `publish_ctl` 언급도 정정.
- **`latest_active_consent` 가 fail-open 이었다** — `WHERE revoked_at IS NULL ORDER BY version DESC` 는 v2 를 철회하면
  v1 을 되살린다. `consent/gates.py` 의 docstring 이 명시적으로 금지한 해석이고, `tests/rls/test_repo_under_rls.py` 가
  `# v1 is still active` 주석으로 그 잘못된 의미를 **고정하고** 있었다. 최고 버전을 무조건 고른 뒤 철회 여부를 보도록
  고치고, 테스트 단언을 `is None` 으로 바꿨다(§8.3, `gates.active_scopes` 의 행 버전).
- **`CHARTWIRE_DB_SINGLE_ROLE` 은 죽은 설정이었다**(`Settings.db_single_role` 을 읽는 코드가 src 에 없음). 필드와
  render.yaml 항목을 지우고, render.yaml·SPEC §4.1·ADR-0001·ADR-0005·schema.md 를 실제 메커니즘(카탈로그 조회
  `helpers.app_role_exists()` + 두 DB URL 을 같은 connectionString 에 바인딩)으로 정정. render.yaml 의 "이 역할은 테이블
  소유자가 아니며" 는 사실과 반대여서 함께 고쳤다.
- **문서-코드 불일치 정정** — `docs/grounding.md` §6 의 fact recall 원인(단서 목록 → §9.2 의 섹션당 12문장 상한; 코드가
  옳고 문서가 낡았다), 같은 문서 §5 표의 규칙 8 정규식(실제 15 대안), `scripts/console_screenshots.py` 의 "번들 ffmpeg 로
  GIF 를 만든다"(`docs/dev/e2e.md` §5 가 불가능하다고 기록해 둔 것), `docs/dev/e2e.md` 의 하드코딩된 statement/alert id
  (`scripts/e2e_check.sh` 처럼 응답에서 뽑도록), `wp-f-phase1.md` §3 의 낡은 범주별 수치에 "전부 낡음" 배너.

## 4. 재측정이 필요해 **고치지 않고 정직하게 적어 둔 것**

이 세션은 부하·perf 재실행이 금지돼 있다. 아래는 코드가 아니라 **문서가 사실을 말하도록** 고친 항목이다.

1. **A n=200 에서 42/200 세션이 유실됐다.** `loss` 는 "전송 seq − 원장 행" 이라 **보낸 적 없는 청크를 셀 수 없고**,
   `segments_contiguous` 200/200 에는 행이 0 인 42 세션이 "자명하게 연속" 으로 들어 있었다. 재측정 없이 고칠 수 있는
   것은 전부 했다: `report.py` 가 세션 행을 `ended / transcribed / **시도**` 로 찍고 `clients.outcomes` 를 그대로 한 줄
   더 찍으며 연속성 행에 `행 0 세션 N 포함` 을 붙인다(`runner.py` 가 `segments_zero_row` 를 기록); README 의 무릎
   문단이 158/200 과 "무릎에서는 새 세션을 받지 못한다" 를 말하고 표에 두 행이 추가됐다; `limitations.md` §3 에도 적었다.
   그리고 다음에 숨지 못하도록 `RecorderClient.run` 이 예상 못 한 예외를 `errors` + `outcome="crashed:…"` 로 기록한다
   (그랬다면 42건의 hello 타임아웃이 측정 시점에 보였을 것이다).
2. **시나리오 C 의 `폐기된 partial 0` 은 구성상 도달 불가능하다** — 뷰어가 받는 partial 최대 150 < `partial_q` 256 이고,
   "느린" 뷰어의 5 msg/s 가 실제 도착률 ≈ 2.9 msg/s 보다 빠르다. 이 0 은 drop-oldest 경로가 **한 번도 실행되지 않았다**는
   뜻이다. README 표 ① 의 C 행에 각주를, `limitations.md` §3 에 계산과 함께 적고, 실제로 재려면 무엇을 바꿔야 하는지
   (`slow_viewer_delay_s` ↓, 더 긴 실행 또는 `partial_max` 오버라이드)까지 남겼다. **시나리오 파라미터는 바꾸지 않았다** —
   바꾸면 재실행 없이는 값이 없는 열이 된다.
3. **리포트 헤더의 `git_sha` 가 코드를 가리키지 않았다.** 커밋된 리포트는 전부 `3162091` 을 적지만 그 숫자를 낸 코드는
   다음 커밋 `d66d051` 에 있다(측정 시점 작업 트리가 더러웠고 헤더가 그것을 알리지 않았다). 재발 방지로
   `eval/report.py::git_sha()` 가 더러운 트리에 `-dirty` 를 붙인다. 계산형 평가 6종은 §0 처럼 재실행해 **본문이 바이트
   단위로 같음**을 확인했으므로 표 ③ 은 배포 코드의 값이다. **부하 리포트 A/B/C/D 는 같은 확인이 불가능하고 재실행하지
   않았다** — 헤더의 sha 를 체크아웃해 재현하면 redis 풀 상한 수정이 없는 코드가 나온다. 이 사실을 README §2 의
   "출처(provenance)" 문단과 `limitations.md` §3 에 적었다. **JSON 헤더의 sha 는 고쳐 쓰지 않았다**: 그 실행이 일어난
   시점의 HEAD 가 맞고, 다른 값을 적으면 또 다른 거짓이 된다.
4. **Ops 스크린샷(`docs/images/05_ops.png`)은 유휴 상태이고 DLQ 목록이 없다**, 그리고 붉은 `unacked alerts over SLA` 는
   공유 개발 DB 에 남은 부하/벌크 잔여 데이터다. 캡처를 다시 돌리지 않았으므로 README 캡션과 `limitations.md` §6 이
   그 사실과 게이지의 성질(프로세스 전체·테넌트 합산, 테넌트당 `OPEN_SCAN_LIMIT=1000`)을 설명한다. 고치는 방법
   (전용 `chartwire_demo` DB, 녹음 중 한 프레임 추가, `#loadDlq` 클릭)은 §4 에 적어 두었다.
5. **주입/변이 평가는 자기 테스트 세트에 맞춰져 있다.** 규칙 8 정규식이 §9.5 6문장의 표면형을 포함하도록 넓혀졌고
   held-out 주입 세트가 없으며, 변이 생성기는 검증기의 사전·정규식을 그대로 import 한다. **정규식을 되돌리지 않았다**
   (되돌리면 `injection_leaks` 가 나빠지는데, 그것은 값을 좋게 만들려고 기준을 바꾸는 것의 거울상이다). 대신 README
   표 ③ 의 두 행과 §2 의 "믿으면 안 되는 쪽", `grounding.md` §7, `docs/eval/README.md` 해석 규칙 3, `limitations.md` §1 이
   전부 "구성상 높다/낮다 — 회귀 점검용" 이라고 말하고, 진짜 수치를 만들 방법(사전 밖 홀드아웃 세트를 `FROZEN.txt` 에
   동결해 여덟 번째 변이 클래스로 보고)을 적었다.
6. **단일 역할에서 검색은 느린 경로다** — 9번 수정으로 격리는 확보했지만, 그 조건에서의 검색 지연은 측정하지 않았다.
   `limitations.md` §4 가 "README 표 ② 의 Q2c/Q2d 는 두 역할 배포의 값이고 Render 에서는 성립하지 않는다" 고 적는다.

## 5. 적용하지 않은 것

- **WS `watch` 의 ack 에는 소유 검사를 추가하지 않았다.** 11번은 REST 전용이다. WS 경로에는 세션 단위 티켓 검사만
  있고 "임상의(본인)" 규칙이 연결 수립 단계에 아예 없어서, 여기에 규칙을 넣으면 부하 클라이언트의 뷰어 ack 가 조용히
  멈춰 측정된 시나리오의 의미가 바뀐다(재측정 금지). 리뷰가 지적한 범위 밖의 별개 갭으로 남겨 둔다.
- **`docs/eval/*.json` 과 `docs/loadtest/*.json` 의 `git_sha` 재기입** — §4.3 의 이유.
- **README 에 실명·이메일** — 저장소 메타데이터로 확인 가능한 GitHub 핸들(`@sokldjs554`)과 저장소 링크만
  README 끝과 `pyproject.toml`(`authors`, `[project.urls]`)에 넣었다. 실명·연락처는 저자가 채울 자리다.
- **GitHub About/topics** — 저장소 설정이라 이 세션에서 바꿀 수 없다.
