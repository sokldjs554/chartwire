# 한계와 미실행 항목

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 README 가 주장하지 **않는** 것을 적는다. 숫자를 좋게 보이게
> 하려고 기준을 바꾼 곳은 없다(ADR-0003); 그 대신 아래를 읽고 수치를 해석해야 한다.

## 1. 평가하지 않은 것

- **STT 품질** — 실제 음성인식은 한 번도 돌리지 않았다. 오디오는 시드 난수 바이트이고, 전사는 스크립트 타임라인을 재생하는
  `ScriptedSimulator` 다. `AwsTranscribeStreaming` 은 이벤트 매핑 코드와 가짜 클라이언트 테스트만 있으며 네트워크로 검증되지 않았다
  ([`stt-providers.md`](stt-providers.md) §2.3). 따라서 "전사 지연", "final e2e" 는 시뮬레이터의 지연 분포(N(120, 30) ms, [30, 300] 클램프)를 포함한 파이프라인 지연이지 인식 지연이 아니다.
- **진료 노트 품질** — 초안이 임상적으로 쓸 만한지는 묻지 않았다. 측정한 것은 "문장이 근거 인용으로 뒷받침되는가"(검증기), "변조를
  잡는가"(변이 7종), "정직한 바꿔쓰기를 거부하지 않는가"(패러프레이즈)뿐이다. 추출형 coverage 1.0 은 구성상 당연한 값이며, fact recall 이
  낮은 유형은 cue 부재 때문이다([`eval/README.md`](eval/README.md)).
- **Anthropic tier 미실행** — 빌드 박스에 키가 없고 외부 네트워크가 막혀 `AnthropicProvider` 는 요청 형태·오류 경로·픽스처 재생만
  테스트했다. `scripts/eval_anthropic.py` 는 키가 있을 때만 라이브로 돈다. README 는 "미실행" 으로 적는다.
- **위험 탐지의 임상 정확도** — held-out P/R 은 손으로 쓴 300 문장에 대한 규칙 사전의 수치다. 임상 검증, 방언·연령대 커버리지, 실제
  발화 분포에 대한 주장은 없다. `past` 종류의 해석(§9.4 severity −1 규칙 vs held-out 레이블)이 두 문서에 같은 문장으로 적혀 있다.

## 2. 합성 데이터의 상한

- 스크립트 문법(`synth/grammar.py`)이 만들 수 있는 서로 다른 문장은 수백 개 수준이다. in-grammar 회귀 수치(P/R ≈ 1)는 **같은 문법**으로
  만든 세트에 대한 값이라 일반화 근거가 아니다. held-out 세트만이 문법 밖의 문장이다.
- 대량 적재(2 M 세그먼트)의 텍스트 분포·키워드 비율·검색 세션 60 % 는 설계값이다. 실행계획은 이 분포에서 나온 것이고 실제 의원 데이터의
  카디널리티와 다를 수 있다. RLS 오버헤드 %(Q5) 는 같은 박스, 같은 캐시 상태에서의 상대값이다.
- 세션당 발화 46–61개, 세션 길이 ≈ 3 분(스크립트 `total_ms`)은 스펙 범위의 아래쪽이다.

## 3. 부하 수치는 클라이언트 혼입(client-confounded)이다

- api / worker / stt-worker / PostgreSQL / Redis / 부하 클라이언트가 **한 4 vCPU 박스**에서 돈다(`taskset` 으로 api 0–1, 서비스 2–3,
  클라이언트 3). 클라이언트와 stt-worker 가 코어를 나눠 쓰므로 N=200 에서의 지연에는 클라이언트 자체의 스케줄링 지연이 들어 있다.
  README 의 모든 부하 숫자는 "same host, 4 vCPU, loopback, STT simulator, client-confounded" 캡션을 단다.
- `ack_rtt` 는 클라이언트 시계로 잰 왕복이고 50 ms 그룹 커밋 창과 코어의 100 ms ack 규칙을 포함한다. 네트워크 지연은 0(loopback).
- 티켓 발급은 REST 가 아니라 프로세스 안에서 Redis 에 직접 쓴다(`POST /ws-ticket` 는 30/min 레이트리밋 — 200 세션 램프에 맞지 않음). 따라서
  REST 티켓 경로의 지연·429 동작은 부하 수치에 없다(통합 테스트가 따로 고정한다).
- 시나리오 D 의 "FLUSHALL" 은 실행 인덱스에 대한 `FLUSHDB` 다(공유 박스). 앱은 그 인덱스만 쓰므로 앱 관점의 효과는 같다.
  stt-worker SIGSTOP 15 s 는 리스 30 s 보다 짧아 **같은** 워커가 이어받는다; 두 워커 간 인계는 `tests/integration/test_stt_worker_chaos.py` 가 2 s 리스로 따로 증명한다.
- `dup` 은 "뷰어에 같은 세그먼트 seq 가 두 번 배달된 수" 다. DB 행 중복은 PK 로 불가능하고, stt 계층의 재전달 스킵 수는 로그 카운터라 리포트에 싣지 않았다.
- CPU/RSS 는 psutil 1 s 샘플이다. `postgres` 행은 모든 백엔드 프로세스의 합이라 공유 버퍼가 프로세스마다 중복 계산된다(추세 비교용).
- 시나리오 E(nginx 뒤 2 프로세스 drain)는 시간이 남을 때만 실행한다(스펙 §11.2). 실행하지 않으면 README 행이 삭제된다.

## 4. 단일 리전 · 단일 박스 운영 가정

- PostgreSQL 한 인스턴스, Redis 한 인스턴스, 리전 하나. CDK 스택은 Multi-AZ RDS 와 ElastiCache 복제본을 정의하지만 **synth 만 했고
  배포하지 않았다**(계정 없음). 장애 조치·리전 간 복제·백업 복원 시간은 측정하지 않았다([`aws.md`](aws.md)).
- 드레인은 프로세스 단위(SIGTERM)로만 검증했다. 롤링 배포 중 두 api 노드 사이의 세션 이동은 프로토콜(1012 → resume)이 지원하지만 nginx 뒤 실측은 시나리오 E 다.
- Render 무료 티어(`render.yaml`): 콜드 스타트가 길고 DB 는 기간 만료로 사라지며 디스크가 비영속이라 `localfs` 오브젝트 스토어는
  재배포 시 비워진다(파기 데모에는 문제없음). 라이브 URL 은 동작하는 동안에만 README 에 둔다.

## 5. 법적 보존 자동화는 플래그까지다

- 서명 노트에 `legal_hold='medical_record'`, `retention_until = signed_at + 10년` 을 기록하고 파기가 이를 건드리지 않는 것까지가 구현이다.
  보존 기간 만료 후의 자동 삭제, 보존 정책 변경, 법적 보류(litigation hold) 워크플로, 감사자 열람 승인 흐름은 없다.
- 파기는 "이 시스템이 통제하는 저장소" 안에서만 완결된다. PostgreSQL WAL·베이스 백업·복제본·오브젝트 스토어 버전·로그 집계 시스템에 남은
  **암호문**은 지우지 않는다 — DEK 를 파기했으므로 열리지 않는다는 것이 주장의 전부다([`consent-purge.md`](consent-purge.md) §7).
- `KeyCache` 는 프로세스별이라 `keys:invalidate` 를 놓친 노드는 최대 600 s 동안 언래핑된 키를 메모리에 둘 수 있다(새 요청은 `dek_wrapped IS NULL` 로 막힌다).
- 감사 기록은 append-only 트리거로 보호되지만 외부 WORM 저장소로 내보내지 않는다.

## 6. 그 밖의 알려진 갭

- `GET /alerts?open=0` 은 빈 배열(닫힌 경보 목록 함수 없음). `over_sla_count` 는 테넌트당 열린 경보 1,000개까지만 본다.
- `edit` 결정의 콘솔 UI 는 `window.prompt` 다(단일 파일·무빌드 제약).
- 프로토콜의 `IngestCore.on_ledgered` 가 `now_ms` 를 갱신하지 않아 100 ms ack 규칙이 지난 틱 시각으로 평가된다 — 5 chunk/s 에서 ack RTT 가
  200 ms 틱 근처로 양자화된다(WP-H 스모크에서 관찰, `docs/dev/handoff/wp-h-phase2.md` §4 요청). 수정되면 README 의 ack 수치는 재측정으로만 바뀐다.
- LOC 예산(§0)을 대부분의 작업 패키지가 넘겼다(핸드오프에 그대로 보고). 줄이지 않고 남긴 것은 통합자의 결정이다.
- `mypy --strict` 는 스펙이 정한 5 모듈에만 적용된다; 나머지 트리는 `mypy` 기본 설정에서 37건의 비-strict 오류가 있다(HEAD 에도 있던 것).
