# ChartWire

**상담에 집중하세요. ChartWire는 실시간 기록부터 근거 연결 초안, 사람 검토, 데이터 파기 증적까지 한 흐름으로 연결합니다.**

[![ci](https://github.com/sokldjs554/chartwire/actions/workflows/ci.yml/badge.svg)](https://github.com/sokldjs554/chartwire/actions/workflows/ci.yml)

> 모든 데이터는 **합성(SYNTHETIC)** 입니다. 실제 환자 정보 없음 · 임상 사용 불가 · 연구/포트폴리오 구현.

## 서비스 데모

**Live**: <https://chartwire.onrender.com>  
`demo` / `clinician@demo.clinic` / `demo1234!`

![ChartWire 서비스 데모](docs/images/demo.gif)

## 제품 흐름

`새 상담 → 실시간 기록 → 초안·근거 → 사람 검토 → 파기 → 파기 영수증`

- **실시간 기록** — WebSocket 연결이 끊겨도 durable ACK 기준으로 이어서 처리합니다.
- **근거 연결 초안** — 초안 문장을 원문 발화와 연결하고, Assessment는 사람이 작성합니다.
- **기관별 격리** — PostgreSQL RLS로 테넌트 경계를 데이터베이스에서도 강제합니다.
- **파기 증적** — 동의 철회 → 파기 → 잔존/키 검증 → 영수증 흐름을 추적합니다.

## 검증된 범위

같은 호스트 4 vCPU · loopback · STT simulator 기준의 측정값입니다.

| 항목 | 결과 |
|---|---:|
| N=100 처리량 | <!-- num:load.A.n100.chunks_per_s -->500<!-- /num --> chunk/s |
| N=100 ACK p95 | <!-- num:load.A.n100.ack_p95_ms -->449<!-- /num --> ms |
| N=100 전송 결과 | loss <!-- num:load.A.n100.loss -->0<!-- /num --> / dup <!-- num:load.A.n100.dup -->0<!-- /num --> |
| N=200 부하 한계 | ACK p95 <!-- num:load.A.n200.ack_p95_ms -->9036<!-- /num --> ms · 실측 <!-- num:load.A.n200.chunks_per_s -->790<!-- /num --> chunk/s |

N=200에서는 지연이 급격히 증가해 현재 단일 Python 프로세스 구조의 한계가 드러납니다. 상세 결과는 [`docs/loadtest/results.md`](docs/loadtest/results.md)에 있습니다.

## 기술 구성

`FastAPI` · `PostgreSQL 16` · `Redis 7` · `WebSocket` · `Alembic` · `Docker` · `pytest`

핵심 구현은 `seq / ack / credit / resume / epoch`, PostgreSQL RLS, transactional outbox, crypto-shred, purge receipt입니다.

## 실행

```bash
docker compose up --build
```

실행 후 <http://localhost:8000> 에서 확인할 수 있습니다.

## 문서

- [WebSocket protocol](docs/protocol.md)
- [Grounding](docs/grounding.md)
- [Consent & purge](docs/consent-purge.md)
- [Load test](docs/loadtest/results.md)
- [Performance](docs/perf/README.md)
- [Operations runbook](docs/ops/runbook.md)
- [Limitations](docs/limitations.md)

## 범위

공개 데모는 무료 단일 컨테이너 환경이라 성능 측정용이 아닙니다. 실제 마이크/STT 품질, 실제 진료 대화 기반 생성 품질, 운영용 인증·KMS·멀티테넌트 배포·임상 검증은 이 데모의 검증 범위에 포함하지 않습니다.
