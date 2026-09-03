# WP-H 핸드오프 — Infra / Console / Loadtest client (Phase 0)

> 상태: **Phase 0 완료** (2026-09-03, 3차 실행에서 마무리). 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음.
> Phase 1 항목(`loadtest/scenarios.py`·`runner.py`·`report.py`, `docs/loadtest/results.md`, CI 마무리, `docs/AGENTS.md`,
> README 조립)은 스펙 §15 대로 B/C/D/E 통합 뒤에 시작한다.

## 1. 무엇을 만들었나 (모듈 맵)

| 경로 | 내용 | 스펙 |
|---|---|---|
| `infra/cdk/stack.py` | 단일 `ChartwireStack`: VPC(2 AZ, NAT 1, public/private-egress/isolated), KMS KEK(회전), S3 audio(SSE-KMS, public block, enforce_ssl, 버저닝 off, 90 d 만료, RETAIN) + ALB 로그 버킷, RDS PG16 `t4g.medium` Multi-AZ(암호화, isolated, `pg_stat_statements`, `rds.force_ssl=1`, 백업 7 d, 포트 5433), ElastiCache Redis 7 `cache.t4g.small` ×2(TLS/at-rest/AUTH, 포트 6380), Secrets Manager(Redis AUTH, JWT; DB 는 RDS 생성), ECS 클러스터 + Fargate ×3(api 2 · worker 1 · stt-worker 1, 1 vCPU/2 GB, api CPU 60 % 2–6 오토스케일), `ContainerImage.from_registry(f"{ImageRepo}:{ImageTag}")`, ALB(`idle_timeout 3600`, 80 → api, `cert_arn` 있으면 443 L1 리스너, `/readyz`, dereg 30 s), 알람 3개 → SNS, 출력 3개 | §12.1 |
| `infra/cdk/app.py` | synth 진입점 + cdk-nag `AwsSolutionsChecks` — ERROR 가 남으면 exit 1 | §12.1 |
| `infra/cdk/cdk.json`, `requirements.txt` | `versionReporting/pathMetadata/assetMetadata=false`; `aws-cdk-lib` 정확 고정, `cdk-nag==2.38.2` | §12.1 |
| `infra/cdk/cdk.out/ChartwireStack.template.json` | 커밋되는 합성 결과(결정론적; CI `git diff --exit-code`) | §11.3 |
| `infra/cdk/nag-suppressions.md` | 수정한 규칙 / 억제한 규칙 + 사유 | §12.1 |
| `infra/cdk/tests/test_stack.py` | 13 테스트: idle timeout 3600, RDS 암호화, S3 public block, 알람 3개, 태스크 역할 최소 권한(`kms:GenerateDataKey/Decrypt` + `s3:*Object`), nag ERROR 0, 결정론 등 | §12.1 |
| `src/chartwire/loadtest/client.py` | asyncio `RecorderClient`/`ViewerClient`(§6 의미론: hello/welcome/ack/nack/credit/pause/end/bye, 링 버퍼 150, credit 준수, resume + missing 재전송, epoch/superseded, drain 1012 재접속, ping/pong), `SessionStats`(ack_rtt/final_e2e/alert_e2e/loss/dup, §11.2 정의), 카오스 훅(`abort_connection`, `delay`, `pause/resume/stop`), 실제 I/O 어댑터 `WebsocketsTransport`/`HttpTicketSource`(임포트 지연) | §6, §11.2 |
| `tests/unit/test_loadtest_client.py` | 19 테스트, 순수 파이썬(가짜 소켓 + 스크립트된 서버 + 가상 시계): 프레임 인코딩, credit 상한, abort→resume→missing 재전송, 링 초과 4008, pause 준수, ping/nack, superseded/consent_revoked, drain, 뷰어 dedup/latency/재접속 | §6 |
| `console/index.html` | 단일 파일 데모 콘솔(1,009 줄, CDN 없음, 한국어, 반응형, `prefers-color-scheme` 다크): 고정 배너, API base 설정, 로그인 박스(데모 4 계정), **Recorder** 탭(세션 선택/생성, 환자 찾기·생성, 동의 체크박스 → consents 엔드포인트, 속도 슬라이더, §6 JS 녹음기, 게이지 seq/ack_seq/credit/outstanding/RTT/epoch, "네트워크 끊기 5초"·일시정지·종료), **Live chart** 탭(뷰어 소켓: partial 회색 → final 검정, 화자 색, presence, final 별 지연, 위험 배너 severity 색/SLA 카운트다운/ACK/escalated, note.status 토스트, lagged/degraded 배지), **SOAP draft** 탭(S/O/P 문장, 클릭 → 근거 하이라이트, unsupported 배지+사유, coverage 게이지, accept/edit/reject, Assessment 텍스트영역 "AI가 작성하지 않는 영역" → PUT, 서명 → legal_hold/retention_until), 사이드 패널 **Consent & purge**(철회 → 파기 작업 폴링 → 단계 스트림 → 영수증 JSON → "복호화 시도") · **Ops**(`/metrics` 파서: 연결, chunks/s, ledger flush p95, stt lag, outbox pending/lag, SLA 초과, DLQ + replay) | §13.3 |
| `tests/unit/test_console_static.py` | 46 테스트: 단일 파일/외부 리소스 없음, 필수 문자열(배너, 엔드포인트, 메시지 타입, 종료 코드, 프로토콜 상수), `node --check`, **Node 하네스**(stub DOM + 가짜 WebSocket)로 녹음기 실행 — 헤더 바이트(0x4357/ver/SIM/seq/offset/6,400 B), hello 형태, credit 상한, close frame 없는 파티션 → resume hello(`last_sent_seq`) → `welcome.missing` 순서 재전송, 좀비 4409 무시, end/nack/bye, superseded 시 재접속 없음 | §15 gate |
| `render.yaml` | web(Docker, `chartwire serve all --embedded`, `preDeployCommand: chartwire db upgrade && chartwire seed --demo --if-empty`, `/healthz`) + Postgres free + Key Value free, `CHARTWIRE_DB_SINGLE_ROLE=1`, 비밀은 `generateValue` | §12.2 |
| `docs/aws.md` | mermaid 아키텍처, "synth 만, 미배포" 명시, 원칙↔스택 대응표, 30→300 의원 용량 산정(레버·규칙 중심, 실측 수치는 옮기지 않음), 가정 기반 비용 스케치 | §12.1 |

## 2. 테스트 실행법

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/unit/test_loadtest_client.py tests/unit/test_console_static.py infra/cdk/tests -q   # 78 passed
python infra/cdk/app.py                       # synth + cdk-nag (ERROR 0), cdk.out 갱신
ruff check src/chartwire/loadtest tests/unit/test_loadtest_client.py tests/unit/test_console_static.py infra/cdk
```
DB/Redis 는 필요 없다(전부 순수 파이썬/Node). `node` 가 없으면 콘솔 동작 테스트 2건은 skip.
최종 실행: **78 passed / 0 failed** (19 + 46 + 13). `tests/unit` 전체는 634 passed / 2 failed — 실패 2건은 WP-D 소유
(`test_notes_korean.py`, `test_notes_verifier.py`), 이 WP 와 무관.

## 3. 설계 결정 · 스펙이 침묵한 곳에서 고른 것

1. **브라우저의 "네트워크 끊기"**: 브라우저는 TCP RST 를 노출하지 않는다. `dropNetwork()` 는 close frame 을 보내지 않고 소켓
   참조를 버려 송수신을 모두 무시한다(서버 관점의 좀비 연결). 5 s 뒤 resume hello 가 epoch 를 올리면 서버가 좀비를
   `bye{superseded}` + 4409 로 밀어내고, 콘솔은 그 close 를 "좀비 연결 close 4409 (epoch fencing)" 로 로그한다 — 즉 §6.1 의
   epoch 펜싱을 그대로 시연한다. 재전송은 `welcome.missing` 만 따른다(client.py 와 동일).
2. **콘솔 녹음기 = client.py 미러**: 전송 루프 순서(재전송 → end 단계 → pause → credit → 새 청크), `queueResends` 가
   `seq ≤ ack_seq` 를 건너뛰고 링에 없으면 `gap_unrecoverable`, `RESUMABLE_CLOSE = {1006, 1012, 4000, 4503}`, end 뒤 nack 은
   재전송 후 계속 대기(≤10 s), `bye{ended}` 로 종료. 페이로드는 xorshift32 시드 난수(파이썬은 sha256 블록) — 둘 다 "청크마다
   다른 바이트" 요건만 만족하면 된다(§6.2).
3. **final 별 지연 표시**: 같은 페이지의 녹음기가 보낸 청크면 `t_end_ms` 로 마지막 청크 seq 를 역산해 send→final e2e 를,
   아니면 `committed_at` 이 있을 때 commit→view 를 보여준다(§11.2 의 두 정의 모두 표기).
4. **파기 추적**: 철회 응답에 `purge_job_id` 가 있으면 자동 추적, 없으면 admin 의 `POST /v1/purge-jobs` 또는 id 입력으로
   추적한다(워커가 작업을 만들기 때문). `GET /purge-jobs/{id}` 를 1 s 폴링해 `steps[]` 를 스트림처럼 표시, 종료 상태
   (`verified|failed|completed`)에서 영수증 JSON 을 보이고 "복호화 시도" 를 활성화한다.
5. **Ops chunks/s**: `ws_chunks_total{result="stored"}` 의 2 s 차분. ledger flush p95 는 히스토그램 누적 버킷에서 상한(`le`)
   으로 계산해 `≤N ms` 로 표기(버킷 상한이라 정확값이 아님을 표기).
6. **데모 계정 이메일**: 스펙에 없어 `clinician@demo.clinic` 등 `<role>@demo.clinic` 을 기본값으로 넣었고 입력란에서 바꿀 수
   있다(→ §5 요청).
7. **loadtest client 의 종료 코드 관찰**(이번 실행에서 추가): 서버가 `bye`/`error 4008` 로 종료를 주도하면 클라이언트가
   `close_grace_s`(기본 2 s) 동안 서버의 close frame 을 기다려 `close_codes` 에 기록한다(시나리오 D 의 superseded/4409
   집계에 필요). 클라이언트 주도 종료는 `close(1000)`.
8. **render.yaml**: 무료 티어 디스크가 비영속이라 `localfs` 오브젝트 스토어는 재배포 시 사라진다 — 파기 데모에는 문제
   없고 문서화(`docs/limitations.md` 는 WP-H Phase 1 에서 작성). Key Value 는 `noeviction` 으로 조용한 데이터 손실 대신
   fail-closed 를 택했다(ADR-0002 와 일관).
9. **cdk-nag 버전 고정**: `cdk-nag 3.0.2` 는 `aws-cdk-lib 2.267` 의 jsii 런타임에서 `aspect.visit is not a function` 으로
   실패해 `2.38.2` 로 고정(`infra/cdk/requirements.txt`).

## 4. 스펙과의 편차 (이유 포함)

- `SessionOut` 에 `script_ref` 가 없어(§3.2) 세션 선택기는 `script_ref` 가 응답에 있을 때만 표시하고 없으면 "(script 없음)"
  으로 대체한다 — 콘솔은 그대로 동작(§5 요청 참고).
- `docs/aws.md` 의 비용/용량 수치는 가격표 가정 기반 추정으로 명시(§0.2 는 README 실측 표에 대한 규칙; 이 문서는 README 에
  포함되지 않고 "실측 아님" 을 세 번 명시).
- `tests/unit/test_loadtest_client.py` 의 이전 실행본은 한 번도 통과한 적이 없었다(가상 시계가 이벤트 루프를 1턴만 양보해
  ack 처리가 시계 전진 뒤로 밀림, `lt.UTC` 미존재, 마지막 청크 뒤 `end` 가 큐에 남는 단정). 하네스를 고쳤고(`SETTLE_TURNS`),
  클라이언트는 §3.7 의 한 가지만 바꿨다.

## 5. 다른 WP 에 대한 요청

- **WP-F (`synth/seed.py`, `seed --demo`)**: 데모 사용자 4명의 이메일을 `clinician@demo.clinic`, `staff@demo.clinic`,
  `admin@demo.clinic`, `auditor@demo.clinic`, 비밀번호 `demo1234!`, 테넌트 slug `demo` 로 만들어 주세요(콘솔 기본값).
  데모 세션은 `state='created'` 에 `script_ref='s01'…'s20'` 로 최소 몇 개, 환자 `가상환자-0001` 은 4개 스코프 동의 활성.
- **WP-E (`api/schemas.py`, `routers/sessions.py`)**: `SessionOut` 에 `script_ref: str | None` 을 추가해 주세요(§3.2 에는
  없지만 DDL 에 있고 콘솔 세션 선택기가 표시함). `POST /consents/{id}/revoke` 202 응답에 가능하면 `purge_job_id` 를
  포함(없으면 콘솔은 수동 추적으로 동작). `GET /v1/me` 는 `{sub, tenant_id, role}` 평면 객체를 기대(중첩 `principal` 도 처리).
  `GET /v1/purge-jobs/{id}` 의 `steps[]` 원소는 `{step|name, count?, at?}` 형태를 가정.
- **WP-E (`api/app.py`)**: `/console` 에서 `console/index.html` 을 서빙(§13.3; Dockerfile 이 `/app/console` 로 복사함).
  같은 origin 이면 콘솔의 API base 를 비워도 된다.
- **WP-G (`ops/metrics.py`)**: 콘솔 Ops 패널은 `ws_connections{kind}`, `ws_chunks_total{result="stored"}`,
  `ledger_flush_seconds_bucket`, `ledger_pending_rows`, `stt_lag_chunks`, `outbox_pending`, `outbox_lag_seconds`,
  `risk_unacked_over_sla`, `outbox_dead_total` 이름을 그대로 파싱합니다(현재 레지스트리와 일치 확인).
- **WP-B**: 콘솔/클라이언트는 `welcome.missing`, `nack.missing` 을 닫힌 구간 `[[from,to],…]` 로, `bye.ack_seq` 를 선택
  필드로 가정(`docs/protocol.md` 와 일치). 변경 시 `tests/unit/test_console_static.py` 하네스와 `test_loadtest_client.py`
  를 같이 고쳐야 합니다.

## 6. 알려진 이슈 / 남은 일

- 콘솔은 실제 서버와 아직 end-to-end 로 붙여 보지 못했다(REST/WS 가 Phase 1). 엔드포인트가 없으면 problem+json 토스트로
  실패를 보여주고 나머지는 계속 동작하도록 설계했다.
- `edit` 결정은 `window.prompt` 를 쓴다(단일 파일·무빌드 제약에서 가장 단순한 선택; 스크린샷용 데모에 충분).
- Phase 1: `loadtest/scenarios.py`·`runner.py`·`report.py`(A–D, H, E 선택), `docs/loadtest/results.md`, CI `load-smoke`/
  `cdk` 잡 마무리, `docs/AGENTS.md`, `docs/limitations.md`, README 마커 조립.

## 7. 진행 로그
- 05:30 환경/스펙 읽기. `chartwire.ws.codec` 존재 → 로드테스트 클라이언트가 이를 임포트.
- 06:05 CDK synth 성공(nag ERROR 0), test_stack 13 passed; `cdk-nag==2.38.2` 고정.
- 05:38–05:40 `client.py` + `test_loadtest_client.py` 작성 중 중단(테스트 미실행 상태로 남음).
- 10:20 재개(3차). 테스트 하네스 수정 → 19 passed. `client.py` 에 `close_grace_s` 추가.
- 10:55 `console/index.html`(1,009 줄) + `test_console_static.py` 46 passed.
- 11:10 `render.yaml`, `docs/aws.md`, 이 문서. 최종 78 passed / 0 failed, ruff clean.
