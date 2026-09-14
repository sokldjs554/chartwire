# chartwire

**실시간 상담 기록 → 근거 연결 초안 → 사람 검토 → 데이터 파기 증적까지 한 흐름으로 연결합니다.**

> **모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음.** API 키 없이 실행됩니다(STT 시뮬레이터 + 추출형 초안).
> 임상 사용 불가 — 연구/포트폴리오 구현입니다. STT 품질과 진료 노트 품질은 **평가하지 않았습니다**(합성 데이터, 시뮬레이터).

[![ci](https://github.com/sokldjs554/chartwire/actions/workflows/ci.yml/badge.svg)](https://github.com/sokldjs554/chartwire/actions/workflows/ci.yml)
**공개 데모**: <https://chartwire.onrender.com> — 로그인 `demo` / `clinician@demo.clinic` / `demo1234!`
<!-- 링크는 배포가 실제로 동작하는 동안에만 둔다 (spec §12.2). 내려갔으면 이 두 줄을 지운다. -->

## 서비스 데모

![ChartWire 서비스 데모](docs/images/demo.gif)

## 빠른 확인 경로

공개 데모: <https://chartwire.onrender.com>

`새 상담 → 실시간 기록 → 초안·근거 → 사람 검토 → 파기 → 파기 영수증`

1. **새 상담** — 합성 대본을 선택하고 세션을 시작합니다.
2. **실시간 기록** — WebSocket 전사와 위험 신호/SLA를 확인합니다.
3. **초안·근거** — 초안 문장을 눌러 원문 evidence를 펼칩니다.
4. **사람 검토** — 문장 결정을 남기고 Assessment를 작성한 뒤 서명합니다.
5. **파기** — 동의 철회 또는 admin 파기 작업의 진행 상태를 확인합니다.
6. **파기 영수증** — 삭제·키 폐기 증적과 복호화 실패 검증을 확인합니다.

> 제품 흐름을 먼저 확인한 뒤, `seq/ack/credit/resume`, RLS, outbox/DLQ, chaos 결과는 **시스템 상세**와 아래 기술 문서에서 확인할 수 있습니다.

> 무료 인스턴스라 **첫 접속은 1~2분** 걸립니다(잠든 컨테이너를 깨우고 initdb·마이그레이션·데모 시드를 다시 돌립니다).
> 상태는 보존되지 않습니다 — 재배포하거나 다시 잠들면 서명한 노트도 파기 영수증도 사라집니다.
> **성능을 재는 대상이 아닙니다**: PostgreSQL·Redis·api·worker·stt-worker 가 한 컨테이너에서 0.1 vCPU 를 나눠 쓰므로
> 여기서 잰 지연은 아래 표 ①·② 와 비교할 수 없습니다. 이유와 대가는 [`docs/limitations.md`](docs/limitations.md) §8.
> 즉시 보여줘야 한다면 `docker compose up --build` 가 낫습니다(§10).

정신과 진료실 실시간 음성차팅의 밑바닥 — 무손실 WebSocket 스트리밍 프로토콜, 테넌시 격리, 파기 영수증, 실측된 운영 수치.
*The measured reliability & compliance layer under SOAPY-class psychiatric voice-charting products.*

## 1. SOAPY-class 제품이 30 → 300 의원으로 갈 때 백엔드가 감당해야 하는 8가지

음성차팅 제품(진료 녹음 → 전사 → SOAP 초안, 세션 종료 시 오디오 삭제, 의원 ~30곳)은 STT 모델 때문에 실패하지 않습니다. 아래에서 실패합니다.

| # | 문제 | chartwire 의 답 | 근거 |
|---|---|---|---|
| 1 | 진료실 Wi-Fi 가 끊겨 오디오가 유실·중복된다 | seq/ack/credit/resume/epoch 프로토콜, **ack = PostgreSQL 에 durable**, 링 버퍼 재전송, 손실·중복 0 을 속성·카오스 테스트로 고정 | [`docs/protocol.md`](docs/protocol.md), §2 표 ① |
| 2 | 09:00 에 30 의원이 동시에 시작 → STT 큐 적체 → 메모리 폭주 | credit 백프레셔(STT 지연·원장 적체 반영), `pause`, `MAXLEN` 스트림, 유한 큐와 api RSS 기울기를 시나리오 B 로 측정 | §2 표 ①, [`docs/loadtest/results.md`](docs/loadtest/results.md) |
| 3 | 자살 사고 발화가 요약이 아니라 **초 단위**로 임상의에게 닿아야 한다 | stt-worker 트랜잭션 안의 결정론적 위험 탐지 → `risk.alert` 즉시 발행 → SLA 타이머·에스컬레이션 | [`docs/risk-detection.md`](docs/risk-detection.md) |
| 4 | LLM 초안이 아무도 말하지 않은 "리튬 복용 중"을 쓴다 | 문장마다 verbatim 근거 인용을 검증(8 규칙), 스키마에 진단/평가 필드 없음, 저커버리지면 abstain | [`docs/grounding.md`](docs/grounding.md) |
| 5 | 한 DB 에 30 의원 — `WHERE tenant_id` 하나 빠지면 유출 | PostgreSQL RLS + 단일 `chartwire_app` 역할(NOBYPASSRLS), 라우트×역할×테넌트 매트릭스 테스트, superuser 누출 대조 테스트 | [`docs/db/schema.md`](docs/db/schema.md), ADR-0001 |
| 6 | 환자가 동의를 철회 — 그러나 서명된 차트는 10년 보존해야 한다 | DEK crypto-shred + hard delete 파기 영수증 vs 기록 키로 봉인된 서명 노트(`legal_hold`, `retention_until`) | [`docs/consent-purge.md`](docs/consent-purge.md), ADR-0004 |
| 7 | "지난 6개월 이 환자의 불면 언급"을 수백만 세그먼트에서 100 ms 안에 | 월 파티션 + keyset, RLS 아래서 트라이그램 인덱스가 무시되는 플랜과 `SECURITY DEFINER` 해결, `terms[]` 인덱스 | [`docs/perf/README.md`](docs/perf/README.md), ADR-0005 |
| 8 | 라이브 세션을 떨어뜨리지 않고 배포하고, 파이프라인이 어디서 막혔는지 안다 | drain(`bye{drain}` → 원장 플러시 → 1012), 트랜잭션 아웃박스(리스/재시도/DLQ), `/metrics`, 런북 | [`docs/ops/runbook.md`](docs/ops/runbook.md) |

## 2. 실측 수치

세 표 모두 `scripts/readme_numbers.py --write` 가 `docs/{loadtest,perf,eval}/*.json` 에서 채웁니다(spec §11.4). 측정되지 않은 행은 **삭제**되며, 손으로 적은 숫자는 없습니다. 각 JSON 에는 `{seed, git_sha, generated_at, cpu, ram_gb, python, pg_version}` 헤더가 있습니다 — `git_sha` 가 무엇을 뜻하는지는 §2 끝의 **출처(provenance)** 문단을 읽어 주세요.

### ① 프로토콜 · 부하 — *same host, 4 vCPU, loopback, STT simulator, client-confounded*

api / worker / stt-worker / PostgreSQL / Redis / 부하 클라이언트가 **한 박스**에서 돌았고 클라이언트 시계로 잰 값입니다(서버만의 지연이 아님). 정의는 [`docs/loadtest/results.md`](docs/loadtest/results.md).

| 시나리오 | 항목 | 값 |
|---|---|---|
<!-- row:load.A.n50 -->| A N=50 (250 chunk/s) | ack p50 / p95 / p99 | <!-- num:load.A.n50.ack_p50_ms -->81<!-- /num --> / <!-- num:load.A.n50.ack_p95_ms -->223<!-- /num --> / <!-- num:load.A.n50.ack_p99_ms -->262<!-- /num --> ms |
<!-- /row -->
<!-- row:load.A.n50.e2e -->| A N=50 | final e2e p95 / alert e2e p95 / loss / dup | <!-- num:load.A.n50.final_e2e_p95_ms -->208<!-- /num --> ms / <!-- num:load.A.n50.alert_e2e_p95_ms -->56<!-- /num --> ms / <!-- num:load.A.n50.loss -->0<!-- /num --> / <!-- num:load.A.n50.dup -->0<!-- /num --> |
<!-- /row -->
<!-- row:load.A.n100 -->| A N=100 (500 chunk/s) | ack p50 / p95 / p99 | <!-- num:load.A.n100.ack_p50_ms -->182<!-- /num --> / <!-- num:load.A.n100.ack_p95_ms -->449<!-- /num --> / <!-- num:load.A.n100.ack_p99_ms -->1216<!-- /num --> ms |
<!-- /row -->
<!-- row:load.A.n100.e2e -->| A N=100 | final e2e p95 / alert e2e p95 / loss / dup | <!-- num:load.A.n100.final_e2e_p95_ms -->893<!-- /num --> ms / <!-- num:load.A.n100.alert_e2e_p95_ms -->65<!-- /num --> ms / <!-- num:load.A.n100.loss -->0<!-- /num --> / <!-- num:load.A.n100.dup -->0<!-- /num --> |
<!-- /row -->
<!-- row:load.A.n200 -->| A N=200 (목표 1,000 chunk/s — **무릎**) | ack p50 / p95 / p99 | <!-- num:load.A.n200.ack_p50_ms -->4512<!-- /num --> / <!-- num:load.A.n200.ack_p95_ms -->9036<!-- /num --> / <!-- num:load.A.n200.ack_p99_ms -->9576<!-- /num --> ms |
<!-- /row -->
<!-- row:load.A.n200.e2e -->| A N=200 | final e2e p95 / alert e2e p95 / loss / dup | <!-- num:load.A.n200.final_e2e_p95_ms -->8879<!-- /num --> ms / <!-- num:load.A.n200.alert_e2e_p95_ms -->230<!-- /num --> ms / <!-- num:load.A.n200.loss -->0<!-- /num --> / <!-- num:load.A.n200.dup -->0<!-- /num --> |
<!-- /row -->
<!-- row:load.A.n200.rate -->| A N=200 | **실측** chunk/s · credit 최솟값 | <!-- num:load.A.n200.chunks_per_s -->790<!-- /num --> chunk/s · <!-- num:load.A.n200.credit_min -->17<!-- /num --> |
<!-- /row -->
<!-- row:load.A.n200.sessions -->| A N=200 | **끝까지 간 세션 / 시도** | <!-- num:load.A.n200.sessions_ended -->158<!-- /num --> / <!-- num:load.A.n200.sessions_attempted -->200<!-- /num --> |
<!-- /row -->

### ② 저장·검색 — *same host, 4 vCPU, PostgreSQL 16*

| 데이터 | 항목 | 값 |
|---|---|---|
<!-- row:perf.1m -->| 1M segments | cursor p95 / search p95 / COUNT | <!-- num:perf.1m.cursor_p95_ms -->3.1<!-- /num --> ms / <!-- num:perf.1m.search_p95_ms -->7.5<!-- /num --> ms / <!-- num:perf.1m.count -->1000000<!-- /num --> |
<!-- /row -->
<!-- row:perf.10m -->| 10M segments | cursor p95 / search p95 / COUNT | <!-- num:perf.10m.cursor_p95_ms -->15.8<!-- /num --> ms / <!-- num:perf.10m.search_p95_ms -->39.7<!-- /num --> ms / <!-- num:perf.10m.count -->10000000<!-- /num --> |
<!-- /row -->

### ③ 근거 검증 — *synthetic transcripts, fixed generator, no LLM*

| corpus | sentences | verified | abstained | ungrounded accepted |
|---|---:|---:|---:|---:|
<!-- row:eval.summary -->| synthetic v1 | <!-- num:eval.sentences -->37<!-- /num --> | <!-- num:eval.verified -->31<!-- /num --> | <!-- num:eval.abstained -->6<!-- /num --> | <!-- num:eval.ungrounded_accepted -->0<!-- /num --> |
<!-- /row -->

### 출처(provenance)

`docs/{loadtest,perf,eval}/*.json` 의 `git_sha` 는 **측정 당시 코드 커밋**입니다. 숫자는 해당 raw JSON 에서만 뽑습니다. 현재 커밋에 대해 다시 실행했다는 뜻이 아닙니다. `scripts/readme_numbers.py --check` 는 README 와 raw JSON 의 일치만 검증합니다. 즉 위 표는 **고정된 과거 측정값(frozen artifacts)** 이며, 새 코드가 그 성능을 재현한다는 보장은 아닙니다.

## 3. Quick start

### 요구 사항
- Python 3.11+
- PostgreSQL 16+
- Redis 7+
- `ffmpeg` — STT worker 가 오디오 payload 를 WAV 로 감싸기 위해 필요합니다. 실험 모드는 시뮬레이터라 외부 STT 모델은 필요 없습니다.

### Docker Compose — 가장 빠름

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000` · Metrics: `http://localhost:8000/metrics` · OpenAPI: `http://localhost:8000/docs`

### 로컬(직접 실행)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
```

기본 비밀번호가 아닌 값을 `.env` 에 넣고 PostgreSQL/Redis 를 준비한 뒤 각 프로세스를 실행합니다.

```bash
chartwire-api
chartwire-worker
chartwire-stt-worker
```

상세 실행 절차: [`docs/ops/runbook.md`](docs/ops/runbook.md)

## 4. 레포 구조

```text
src/chartwire/       FastAPI API + WebSocket ingest/watch
tests/               unit / integration / property / chaos / smoke
docs/                protocol, schema, ADR, load/perf/eval raw results
scripts/             deterministic load/perf/eval runners
infra/               Docker + CDK/CloudFormation
```

## 5. 설계 문서

- [`docs/protocol.md`](docs/protocol.md) — seq/ack/credit/resume/epoch, close code, drain
- [`docs/db/schema.md`](docs/db/schema.md) — partition, index, RLS, roles
- [`docs/grounding.md`](docs/grounding.md) — 8 grounding rules, abstain
- [`docs/risk-detection.md`](docs/risk-detection.md) — deterministic alert + SLA
- [`docs/consent-purge.md`](docs/consent-purge.md) — crypto-shred / hard-delete / receipt
- [`docs/perf/README.md`](docs/perf/README.md) — 1M/10M 실행법 + explain 근거
- [`docs/ops/runbook.md`](docs/ops/runbook.md) — outbox/DLQ/drain/metrics
- [`docs/limitations.md`](docs/limitations.md) — 검증하지 않은 것까지 포함한 범위

## 6. 범위와 한계

- 합성 데이터만 사용합니다. 실제 PHI 없음.
- STT 는 테스트용 결정론적 시뮬레이터입니다.
- LLM 기반 진단/치료/평가 필드 없음. `Assessment` 는 임상의가 직접 작성.
- 실제 AWS 배포는 검증하지 않았습니다. Render 공개 데모는 무료 데모 환경입니다.
- 법적·임상적 적합성은 주장하지 않습니다.
