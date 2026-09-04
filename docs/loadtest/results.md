# 부하 테스트 결과 (spec §11.2)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 `chartwire loadtest <scenario>` 가
> `docs/loadtest/*.json` 에 쓴 값으로 `chartwire loadtest results` 가 생성한다(손으로 적은 숫자 없음).
> 캡션: **same host, 4 vCPU, loopback, STT simulator, client-confounded** — api/worker/stt-worker/PG/Redis/클라이언트가 한 박스에서 돌았고 클라이언트
> 시계로 잰 값이라 서버만의 지연이 아니다.

정의: `ack_rtt` = 바이너리 프레임 전송 → 그 seq 를 덮는 누적 `ack` 수신(클라이언트 시계, 50 ms 그룹 커밋 창 포함) ·
`final_e2e` = 발화의 마지막 청크 전송 → 뷰어 `transcript.final` 수신 · `alert_e2e` = 서버 `committed_at` → 뷰어 `risk.alert` 수신 ·
`loss` = 전송 seq 수 − `audio_chunks` 행 수 · `dup` = 뷰어에 같은 세그먼트 seq 가 두 번 배달된 수(DB 행 중복은 PK 로 0) ·
**`loss` 는 보낸 적 없는 청크를 셀 수 없다** — 세션이 통째로 떨어진 경우는 `세션 결과` 행(클라이언트가 본 outcome)과
`세션 (ended / transcribed / 시도)` 의 분모로만 보인다. `세그먼트 seq 연속 세션` 도 행이 0 인 세션을 연속으로 세므로
괄호 안의 `행 0 세션` 과 함께 읽어야 한다 ·
`superseded_closes` = 클라이언트가 관찰한 4409 종료 수(close 프레임 없이 끊긴 소켓은 서버 쪽 좀비 4409 를 받지 못하므로 세지 않는다;
옛 epoch 프레임의 펜싱 자체는 `ws_chunks_total{result="stale"}` 에 있다) · `rebuild_count` = stt-worker `stt_rebuilds_total`.

## A — N 세션 × 200 ms 청크 × 뷰어 1 × 60 s

_seed 42 · git `316209103be3` · 2026-09-04T12:23:46+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| N | chunks/s | ack p50 ms | ack p95 ms | ack p99 ms | final e2e p95 ms | alert e2e p95 ms | credit min | loss | dup |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 250 | 81 | 223 | 262 | 208 | 56 | 50 | 0 | 0 |
| 100 | 500 | 182 | 449 | 1216 | 893 | 65 | 46 | 0 | 0 |
| 200 | 790 | 4512 | 9036 | 9576 | 8879 | 230 | 17 | 0 | 0 |

자원 (N=50):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 46.6 | 88.7 | 146.1 | 3.428 |
| worker | 2.4 | 67.5 | 130.7 | 2.709 |
| stt-worker | 57.1 | 100.7 | 115.7 | 7.434 |
| postgres | 1.7 | 9.0 | 1137.5 | 55.017 |
| redis | 5.7 | 9.0 | 25.3 | 1.661 |
| client | 6.6 | 11.9 | 184.3 | 38.773 |

DB 대조 (N=50):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 50 / 50 / 50 |
| 세션 결과 (클라이언트가 본 것) | ended 50 |
| `stt_offsets.last_chunk_seq == final_seq` | 50 / 50 |
| 세그먼트 seq 연속 세션 | 50 / 50 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 2578 / 61 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 10.1 |
| 검사 시각 (녹음 시작 후 s) | 73.0 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

자원 (N=100):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 70.6 | 108.6 | 168.6 | 11.683 |
| worker | 3.3 | 57.8 | 129.8 | 2.732 |
| stt-worker | 90.0 | 100.6 | 126.1 | 13.156 |
| postgres | 1.1 | 7.0 | 1080.8 | 38.602 |
| redis | 7.7 | 12.0 | 30.4 | 3.178 |
| client | 10.4 | 21.3 | 242.3 | 66.504 |

DB 대조 (N=100):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 100 / 100 / 100 |
| 세션 결과 (클라이언트가 본 것) | ended 100 |
| `stt_offsets.last_chunk_seq == final_seq` | 100 / 100 |
| 세그먼트 seq 연속 세션 | 100 / 100 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 5164 / 137 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 18.1 |
| 검사 시각 (녹음 시작 후 s) | 84.3 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

자원 (N=200):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 65.9 | 117.3 | 244.4 | 31.657 |
| worker | 3.8 | 79.1 | 131.3 | 4.081 |
| stt-worker | 80.7 | 101.4 | 130.6 | 8.380 |
| postgres | 1.5 | 7.0 | 1184.6 | 64.160 |
| redis | 6.0 | 11.9 | 39.2 | 2.245 |
| client | 8.7 | 20.8 | 315.7 | 57.263 |

DB 대조 (N=200):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 158 / 158 / 200 |
| 세션 결과 (클라이언트가 본 것) | ended 158 · running 42 |
| `stt_offsets.last_chunk_seq == final_seq` | 158 / 158 |
| 세그먼트 seq 연속 세션 | 200 / 200 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 8099 / 198 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 30.1 |
| 검사 시각 (녹음 시작 후 s) | 125.5 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

## B — SlowStt 400 ms, N=50 (credit → 0, 유한 큐, 평평한 RSS)

_seed 42 · git `316209103be3` · 2026-09-04T12:33:16+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| credit 가 0 에 닿은 시각 (s) | 39.5 |
| credit 0 을 본 세션 수 | 50 |
| `pause` 수신 수 | 90 |
| 스트림 길이 최대 (상한 2000) | 301 |
| api RSS 기울기 (MB/min) | 2.473 |
| loss | 0 |

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 29.0 | 89.7 | 147.3 | 2.473 |
| worker | 1.9 | 68.8 | 131.5 | 0.973 |
| stt-worker | 36.7 | 100.7 | 116.9 | 4.186 |
| postgres | 1.6 | 9.0 | 1181.5 | 24.825 |
| redis | 2.8 | 5.0 | 15.6 | 0.700 |
| client | 4.4 | 11.0 | 184.9 | 13.019 |

DB 대조 (N=50):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 50 / 50 / 50 |
| 세션 결과 (클라이언트가 본 것) | ended 50 |
| `stt_offsets.last_chunk_seq == final_seq` | 50 / 50 |
| 세그먼트 seq 연속 세션 | 50 / 50 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 2578 / 61 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 48.2 |
| 검사 시각 (녹음 시작 후 s) | 138.1 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

## C — 느린 뷰어 20 %, N=100

_seed 42 · git `316209103be3` · 2026-09-04T12:27:55+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| 녹음기 ack p95 (ms) | 356 |
| A N=100 의 ack p95 (ms) | 449 |
| 변화 (%) | -20.9 |
| 느린 뷰어 수 | 20 |
| 폐기된 partial (`ws_dropped_partials_total`) | 0 |
| 배달된 final | 5164 |
| loss | 0 |

DB 대조 (N=100):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 100 / 100 / 100 |
| 세션 결과 (클라이언트가 본 것) | ended 100 |
| `stt_offsets.last_chunk_seq == final_seq` | 100 / 100 |
| 세그먼트 seq 연속 세션 | 100 / 100 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 5164 / 137 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 20.1 |
| 검사 시각 (녹음 시작 후 s) | 85.6 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

## D — 카오스, N=100 (소켓 강제 종료 10 %/10 s · Redis flush 30 s · stt-worker SIGSTOP 15 s)

_seed 42 · git `316209103be3` · 2026-09-04T12:29:37+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| 재접속 시도 / resume 성공 | 140 / 140 |
| resume 성공률 (%) | 100.0 |
| superseded(4409) 종료 | 0 |
| 원장 rebuild (`stt_rebuilds_total`) | 57 |
| `stt_offsets.last_chunk_seq == final_seq` 세션 비율 (%) | 100.0 |
| 세그먼트 seq 연속 세션 비율 (%) | 100.0 |
| loss / dup | 0 / 0 |

카오스 이벤트 (실제 실행 시각):

| t (s) | 종류 | 대상 |
|---|---|---|
| 10.0 | kill_sockets | 10 |
| 20.0 | kill_sockets | 10 |
| 30.0 | flush_redis | FLUSHDB |
| 30.0 | kill_sockets | 10 |
| 40.0 | kill_sockets | 10 |
| 40.0 | stt_sigstop | 7277 |
| 50.0 | kill_sockets | 10 |
| 55.0 | stt_sigcont | 7277 |

DB 대조 (N=100):

| 항목 | 값 |
|---|---|
| 세션 (ended / transcribed / 시도) | 100 / 100 / 100 |
| 세션 결과 (클라이언트가 본 것) | ended 100 |
| `stt_offsets.last_chunk_seq == final_seq` | 100 / 100 |
| 세그먼트 seq 연속 세션 | 100 / 100 (행이 0 인 세션도 연속으로 센다) |
| 세그먼트 행 / 위험 이벤트 | 5164 / 137 |
| loss (전송 − 원장) | 0 |
| stt 파이프라인 소화 완료 / 대기 (s) | 예 / 30.2 |
| 검사 시각 (녹음 시작 후 s) | 97.3 |
| 이전 실행 잔여 세션 제거 | 0 |
| 데이터베이스 | `chartwire_load` |

## H — 아웃박스 벤치 (`chartwire outbox bench`)

_seed 42 · git `bd9b2df45932` · 2026-09-04T11:36:15+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| 이벤트 / 워커 / 테넌트 | 100000 / 2 / 30 |
| events/s | 413.2 |
| DLQ | 0 |
| 워커 SIGKILL 후 reclaim | 1345 (killed=예) |
| claim p50 / p95 (ms) | 11.0 / 41.9 |

## E — drain (선택)

미실행 (스펙 §11.2: 시간이 남을 때만).
