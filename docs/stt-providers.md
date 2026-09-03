# STT 어댑터와 stt-worker (`stt/`)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 음성 → 전사 경계의 **계약**과 그 계약을 소비하는 **stt-worker 프로세스**를 설명합니다. STT 품질은 어디에서도 평가하지 않았습니다(스펙 §0.10): 빌드 박스에는 마이크도, 외부 STT 키도 없고, 전사는 스크립트 타임라인에서 나옵니다.

## 1. 어댑터 계약 (`stt/base.py`, 스펙 §3.1 동결)

```python
@dataclass(frozen=True) class Chunk:   session_id, seq, offset_ms, flags, payload: bytes
@dataclass(frozen=True) class Partial: from_seq, text                       # DB 에 쓰지 않음
@dataclass(frozen=True) class Final:   seq_start, seq_end, speaker, t_start_ms, t_end_ms, text, confidence
class SttStream(Protocol):
    async def feed(self, chunk: Chunk) -> list[SttEvent]      # 청크 하나 → 그 시점에 확정된 이벤트
    async def flush(self) -> list[SttEvent]                   # 오디오 끝 → 남은 이벤트 전부, 이후 사용 불가
class SttAdapter(Protocol):
    async def open(self, session: SessionInfo) -> SttStream   # 세션마다 스트림 하나
```

`SessionInfo{session_id, tenant_id, script_ref, chunk_ms=200, started_at}` 는 어댑터가 `sessions` 행에서 필요로 하는 부분집합입니다. 어댑터는 PostgreSQL·Redis 를 만지지 않습니다 — 그 일은 워커의 몫이고, 그래서 어댑터는 서비스 없이 단위 테스트됩니다.

`Final.speaker` 는 `clinician | patient | unknown` 입니다. `unknown` 은 실제 STT 가 화자를 못 나눌 때의 값이며, 위험 탐지기는 이를 환자 발화로 취급하고(질문 억제 없음) 근거 검증기는 fail-closed 로 인용을 거부합니다(`docs/risk-detection.md` §6, `docs/grounding.md`).

## 2. 어댑터 세 가지

| 어댑터 | 파일 | 용도 | 검증 상태 |
|---|---|---|---|
| `ScriptedSimulator(scripts_dir, seed=, latency=, sleep=)` | `stt/simulator.py` | 기본. `(script_ref, offset_ms)` 타임라인 재생 | 단위 테스트 + 통합 테스트 + 부하 시나리오 A/C/D |
| `SlowStt(adapter, delay_ms)` | `stt/slow.py` | 청크마다 고정 지연 → STT 가 의도적 병목(시나리오 B) | 단위 테스트 + 카오스 테스트(150 ms) |
| `AwsTranscribeStreaming(region, *, client=, speaker_labels=)` | `stt/aws_transcribe.py` | 실제 스트리밍 STT 매핑 | **미검증** (아래 §2.3) |

### 2.1 시뮬레이터 (`ScriptedSimulator`)

- 입력은 `<scripts_dir>/<script_ref>.json` (계약: `stt/scripts_io.py`, 생성기: `chartwire synth scripts`). 오디오 바이트는 **보지 않습니다** — 녹음기가 보내는 페이로드는 시드 난수이고 전사는 스크립트에서 나옵니다(스펙 §10.4).
- `feed(chunk)` 는 창 `[offset_ms, offset_ms + chunk_ms)` 를 계산해, `t_end_ms` 가 창 안에 들어온 발화를 `Final` 로 내고(시드 지연 N(120, 30) ms, [30, 300] 클램프, 한 번 `await sleep`), 두 번째 청크마다 `offset+chunk_ms` 를 포함하는 발화의 경과 비율만큼 접두사를 `Partial` 로 냅니다.
- 타임라인은 창별 **prefix count** 로 미리 색인되어 `feed()` 가 O(1) + 반환 이벤트 수입니다. 시나리오 A 에서 워커가 병목이 되지 않아야 한다는 요구(§7.4)가 이 설계의 이유입니다.
- 갭 뒤 청크는 지나간 `Final` 을 **전부** 냅니다(손실 0). 중복 청크는 아무것도 다시 내지 않습니다. `flush()` 는 남은 `Final` 전부를 지연 없이 냅니다.
- `latency=None` 은 지연을 끕니다(테스트·오프라인 평가). `seed` 는 세션별 RNG(`f"{seed}:{session_id}"`)로 지연·신뢰도를 재현 가능하게 합니다. 타임라인은 `(script_ref, chunk_ms)` 별 캐시라 200 세션이 20 스크립트를 공유해도 파싱은 한 번입니다.
- 워커 관점에서 중요한 성질: **새 스트림은 역사를 다시 냅니다.** 재시작한 워커가 seq 30 부터 먹이면 첫 `feed()` 가 그 앞의 발화를 모두 `Final` 로 냅니다. 워커는 이를 시간 규칙으로 걸러냅니다(§3.4).

### 2.2 `SlowStt`

내부 어댑터에 위임하되 `feed()` 앞에 `delay_ms` 를 잡니다(`flush()` 는 지연 없음). 200 ms 청크 주기에 `delay_ms=400` 이면 스트림이 쌓이고 `stt:lag` 가 오르며 녹음기 credit 이 0 으로 내려가 `pause{reason:'stt_lag'}` 가 관찰됩니다 — 시나리오 B 의 유일한 목적입니다.

### 2.3 AWS Transcribe Streaming 매핑 (미검증)

`amazon-transcribe` SDK 의 `TranscriptEvent.transcript.results` 를 계약으로 옮기는 코드입니다.

| Transcribe | chartwire |
|---|---|
| `result.is_partial == True` | `Partial(from_seq=<이 발화의 첫 청크 seq>, text)` |
| `result.is_partial == False` | `Final(seq_start, seq_end=<마지막으로 feed 한 seq>, speaker, t_start_ms=start_time·1000, t_end_ms=end_time·1000, text, confidence=<item confidence 평균>)` |
| `item.speaker` 다수결 `spk_0` / `spk_1` | `clinician` / `patient` (`speaker_labels` 로 변경; §10.2 의 첫 화자는 임상가) / 없으면 `unknown` |

`AwsTranscribeStream` 은 출력 스트림을 펌프하는 태스크와 큐로 이루어져 `feed()` 가 `send_audio_event` 를 보내고 그때까지 도착한 이벤트를 돌려주며, `flush()` 가 `end_stream` 뒤 큐를 비웁니다. `client` 를 주입할 수 있어 가짜 이벤트로 매핑·스트림 로직만 단위 테스트했습니다.

**검증되지 않은 것**(빌드 박스는 오프라인, §0): 실제 네트워크 세션, 인증, 재연결, `start_time` 이 스트림 시작 기준이라는 가정(워커의 `created_at = started_at + t_start_ms` 규칙은 세션 시작 기준 시각을 전제하므로 재시작 시 오프셋 보정이 필요할 수 있음), 한국어 화자 분리 품질, 요금. README 는 이 어댑터를 "미실행" 으로 적습니다.

## 3. stt-worker (`stt/worker.py`, `stt/consumer.py`, 스펙 §7.4)

```
chartwire serve stt-worker            # 또는 python -m chartwire.stt.worker
```

### 3.1 프로세스 구조

```
SttWorker.run()  ── 1 s 마다 ──▶ SMEMBERS stt:active
                                   └─ 소유자 없음 → SET stt:owner:{sid} <worker_id> NX PX 30000
                                        └─ 테넌트 해석 → SessionConsumer 태스크 하나
                 ── 10 s 마다 ──▶ 리스 갱신(Lua compare-and-expire), stt_lag_chunks 게이지
SessionConsumer.run()
   _load()           stt_offsets(last_chunk_seq, last_segment_seq) + 마지막 세그먼트 t_end, DEK(KeyCache)
   _autoclaim()      XAUTOCLAIM idle > 60 s (죽은/멈춘 소비자의 미확인 엔트리 회수)
   loop              XREADGROUP GROUP stt <worker_id> COUNT 32 BLOCK 1000 STREAMS sess:{sid}:chunks >
                     └─ 엔트리마다 §3.3 → 배치 뒤 HSET stt:lag
   end marker        flush → 남은 Final → sessions.state='transcribed' + outbox session.transcribed (한 tx)
                     → XACK → PUBLISH session.state → SREM stt:active, HDEL stt:lag, 리스 해제
```

- **세션 → 테넌트.** `stt:active` 는 세션 id 만 담고 `sessions` 는 RLS 아래 있으므로 테넌트를 알아야 행을 읽을 수 있습니다. `sess:{sid}` 해시의 `tenant` 필드가 있으면 그것을(WP-B 에 요청), 없으면 활성 테넌트를 순회하며 PK 를 조회합니다(30 s 캐시, 세션당 한 번). 어느 테넌트에도 없는 id 는 `stt:active` 에서 제거합니다.
- **리스.** `SET NX PX` 로 잡고 Lua 로 갱신합니다: 값이 내 `worker_id` 면 `PEXPIRE`, 키가 **없으면**(Redis 손실) 다시 `SET`, 다른 워커의 것이면 0 → 그 세션의 소비자를 멈춥니다. 최종 세그먼트 트랜잭션과 오프셋 기록 직전에도 소유권을 확인하므로, SIGSTOP 됐다가 깨어난 옛 소유자가 새 소유자의 `stt_offsets` 를 되돌리지 못합니다(카오스 테스트).
- **드레인.** SIGTERM → 발견 중단 → 소비자마다 진행 중 엔트리까지 처리 → 리스 해제 → 종료 코드 0(예산 25 s 안) / 1. 다른 워커가 즉시 이어받습니다.
- **ops HTTP.** `CHARTWIRE_STT_WORKER_PORT`(기본 9002, `0` 이면 없음)에 `/healthz /readyz /metrics` (WP-G `standalone_app`).

### 3.2 설정

`Settings`(`CHARTWIRE_*`) 외에 워커 전용 환경변수를 `SttWorkerConfig.from_env` 가 읽습니다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `CHARTWIRE_STT_PROVIDER` | `simulator` | `simulator` · `slow` · `aws` |
| `CHARTWIRE_STT_SCRIPTS_DIR` | `var/scripts` | 시뮬레이터 스크립트 디렉터리 (`chartwire synth scripts --out`, `seed --demo` 와 같은 경로) |
| `CHARTWIRE_STT_SLOW_DELAY_MS` | `400` | `slow` 의 청크당 지연 |
| `CHARTWIRE_STT_SIM_SEED` / `_SIM_LATENCY` | `0` / `true` | 시뮬레이터 시드 · 제공자 지연 모델 on/off |
| `CHARTWIRE_STT_AWS_REGION` | `ap-northeast-2` | `aws` 리전 |
| `CHARTWIRE_STT_FETCH_AUDIO` | `aws` 면 true | 정상 경로에서 오브젝트 스토어의 오디오를 읽어 복호화해 어댑터에 넘길지. 시뮬레이터는 페이로드를 쓰지 않으므로 기본 false; 갭 rebuild 는 값과 무관하게 항상 원장 + 오브젝트 스토어에서 복호화(sha256 검증)합니다 |
| `CHARTWIRE_STT_LEASE_MS` / `_AUTOCLAIM_IDLE_MS` | `30000` / `60000` | 소유 리스 · XAUTOCLAIM 유휴 임계(테스트는 짧게) |
| `CHARTWIRE_STT_WORKER_PORT` | `9002` | ops HTTP 포트 |

### 3.3 엔트리 하나의 처리

스트림 엔트리 필드는 `seq,key,len,off,fl,ep,ts`(문자열), 엔드 마커는 `{"end":"1","ep":<epoch>}` 입니다(§5).

1. `seq ≤ last_chunk_seq` → 중복: `XACK` 만 하고 건너뜀 (재개 재전송, 재전달).
2. `seq > last_chunk_seq + 1` → **갭**: `audio_chunks WHERE seq BETWEEN last+1 AND seq-1 ORDER BY seq` 를 읽고 오브젝트 스토어에서 복호화(`aad = tenant:session:sid:chunk:{seq}`, sha256 대조)해 어댑터에 먹입니다. 행이 아직 없으면(원장 커밋 전) 200 ms 마다 다시 봅니다 — 엔드 마커는 `ledger_seq == final_seq` 뒤에만 오므로(§6.4) 무한 대기는 없습니다.
3. 동의 `transcription` 이 없으면 어댑터를 부르지 않고 오프셋만 전진(1 s 캐시, 최종 트랜잭션에서는 항상 다시 읽음). 엔드 마커의 `flush()` 도 같은 게이트를 지납니다.
4. `feed(chunk)` → `Partial` 은 `transcript.partial` PUBLISH(DB 없음) → `Final` 은 §3.4 의 트랜잭션 → `XACK`.
5. 배치가 끝나면 `XINFO GROUPS` 의 `pending + lag` 를 `HSET stt:lag sid` 로 기록합니다(credit 계산 입력, §6.4).

소비자 그룹이 사라졌으면(`NOGROUP`, 또는 블로킹 읽기 중 키가 지워져 `UNBLOCKED`) `XGROUP CREATE … $ MKSTREAM` 뒤 `SADD stt:active`(세션은 살아 있음) 하고 원장을 `last_chunk_seq + 1` 부터 재생합니다. 세션 상태가 종료 상태면 소비자를 끝냅니다.

### 3.4 최종 세그먼트 트랜잭션 (§7.4)

```
insert_final(seq = last_segment_seq + 1, text_enc = AES-GCM(DEK, aad tenant:session:sid:segment:{seq}),
             created_at = started_at + t_start_ms)          # (session_id, seq, created_at) 멱등
if search_index ∈ scopes: segment_search(text, terms = lexicon_tag(text))
for hit in detector.scan(text, speaker):                     # risk_hits_total{category,severity,suppressed}
    if hit.alerts: risk_events + audit alert.created         # alerts.create_event
stt_offsets upsert(last_chunk_seq, last_segment_seq)
COMMIT → committed_at
XACK · ZADD alerts:sla(sev3 +60 s, sev2 +300 s) · PUBLISH transcript.final{…, committed_at} · risk.alert{…, committed_at}
```

- `committed_at` 이 경보 지연(§11.2 `alert_e2e`)의 시작점입니다. 발행은 **커밋 뒤**에만 합니다 — 뷰어의 갭 메우기가 DB 를 읽기 때문입니다.
- **재시작 뒤 중복 Final.** 새 스트림이 역사를 다시 내므로 `final.t_end_ms ≤ 마지막으로 저장된 세그먼트의 t_end_ms` 인 Final 은 버립니다. 세그먼트는 시간순·비겹침이라 어댑터에 독립적인 규칙이고, `insert_final` 의 유니크 키가 2차 방어입니다.
- 세그먼트 `seq` 는 세션 안에서 0 부터 빈틈없이 증가합니다(뷰어 무손실 규칙의 축, `docs/protocol.md` §5).

### 3.5 복구 경로와 테스트

| 상황 | 동작 | 테스트 |
|---|---|---|
| 엔트리 재전달(재개 재전송, 크래시 뒤 미확인 엔트리) | `stt_offsets` 로 중복 제거 | `test_redelivered_entries_are_deduplicated_by_offsets` |
| 스트림 트림/유실로 seq 갭 | 원장 + 오브젝트 스토어 rebuild, 미원장 행은 200 ms 대기 | `test_gap_is_rebuilt_from_ledger_and_waits_for_late_rows` |
| `FLUSHDB`(해시·스트림·그룹·리스·`stt:active` 소실) | `UNBLOCKED/NOGROUP` → 그룹 재생성 `$` → `stt:active`·리스 재취득 → 원장 재생 → 마커 수신 시 정상 종료 | `test_flushdb_recreates_group_and_rebuilds_from_ledger` |
| 워커 SIGSTOP | 리스 만료 → 다른 워커 취득 → `XAUTOCLAIM` → 완료; 깨어난 워커는 소유권 상실을 보고 물러남(중복 0, 오프셋 회귀 0), 둘 다 SIGTERM exit 0 | `test_stt_worker_chaos.py` (실제 서브프로세스 2개) |
| 동의 없음 | `transcription` 없음 → 세그먼트 0, 오프셋은 전진; `search_index` 없음 → `segment_search` 0 | `test_consent_gates` |

메트릭: `stt_lag_chunks`, `segment_e2e_seconds`(청크 수신 → final 커밋), `risk_hits_total`, `risk_alert_latency_seconds`(커밋 → PUBLISH), `risk_alert_publish_seconds`. 로그에는 세션/청크/세그먼트 번호와 카운터만 있고 전사 텍스트·키·토큰은 없습니다(§0.9).
