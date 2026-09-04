# 부하 테스트 결과 (spec §11.2)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 `chartwire loadtest <scenario>` 가
> `docs/loadtest/*.json` 에 쓴 값으로 `chartwire loadtest results` 가 생성한다(손으로 적은 숫자 없음).
> 캡션: **same host, 4 vCPU, loopback, STT simulator, client-confounded** — api/worker/stt-worker/PG/Redis/클라이언트가 한 박스에서 돌았고 클라이언트
> 시계로 잰 값이라 서버만의 지연이 아니다.

정의: `ack_rtt` = 바이너리 프레임 전송 → 그 seq 를 덮는 누적 `ack` 수신(클라이언트 시계, 50 ms 그룹 커밋 창 포함) ·
`final_e2e` = 발화의 마지막 청크 전송 → 뷰어 `transcript.final` 수신 · `alert_e2e` = 서버 `committed_at` → 뷰어 `risk.alert` 수신 ·
`loss` = 전송 seq 수 − `audio_chunks` 행 수 · `dup` = 뷰어에 같은 세그먼트 seq 가 두 번 배달된 수(DB 행 중복은 PK 로 0) ·
`superseded_closes` = 클라이언트가 관찰한 4409 종료 수(close 프레임 없이 끊긴 소켓은 서버 쪽 좀비 4409 를 받지 못하므로 세지 않는다;
옛 epoch 프레임의 펜싱 자체는 `ws_chunks_total{result="stale"}` 에 있다) · `rebuild_count` = stt-worker `stt_rebuilds_total`.

## A — N 세션 × 200 ms 청크 × 뷰어 1 × 60 s

_seed 42 · git `bd9b2df45932` · 2026-09-04T11:26:30+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| N | chunks/s | ack p50 ms | ack p95 ms | ack p99 ms | final e2e p95 ms | alert e2e p95 ms | credit min | loss | dup |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 250 | 74 | 208 | 254 | 215 | 52 | 50 | 0 | 0 |
| 100 | 500 | 159 | 308 | 628 | 816 | 60 | 0 | 0 | 0 |
| 200 | 830 | 2220 | 4679 | 5967 | — | — | 31 | 0 | 0 |

자원 (N=50):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 45.8 | 83.6 | 146.4 | 4.179 |
| worker | 5.4 | 72.8 | 130.2 | 3.148 |
| stt-worker | 55.7 | 100.7 | 115.0 | 8.806 |
| postgres | 5.3 | 35.8 | 1590.0 | 24.483 |
| redis | 5.4 | 8.0 | 11.3 | 1.788 |
| client | 6.6 | 11.0 | 183.6 | 39.092 |

자원 (N=100):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 69.1 | 105.6 | 167.0 | 11.385 |
| worker | 7.2 | 77.7 | 131.6 | 2.778 |
| stt-worker | 88.8 | 100.6 | 125.7 | 12.876 |
| postgres | 4.7 | 26.8 | 1606.9 | -1.325 |
| redis | 7.3 | 12.0 | 17.0 | 3.279 |
| client | 10.1 | 18.7 | 241.9 | 65.529 |

자원 (N=200):

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 79.4 | 117.1 | 218.0 | 20.176 |
| worker | 3.8 | 10.9 | 125.2 | 0.958 |
| stt-worker | 2.1 | 85.1 | 110.4 | 1.301 |
| postgres | 4.0 | 25.8 | 1586.5 | 7.197 |
| redis | 2.3 | 6.9 | 23.0 | 3.860 |
| client | 10.5 | 20.7 | 350.1 | 100.303 |

## B — SlowStt 400 ms, N=50 (credit → 0, 유한 큐, 평평한 RSS)

_seed 42 · git `bd9b2df45932` · 2026-09-04T11:28:27+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| credit 가 0 에 닿은 시각 (s) | 37.9 |
| credit 0 을 본 세션 수 | 50 |
| `pause` 수신 수 | 91 |
| 스트림 길이 최대 (상한 2000) | 301 |
| api RSS 기울기 (MB/min) | 2.413 |
| loss | 0 |

| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |
|---|---|---|---|---|
| api | 31.2 | 91.7 | 146.3 | 2.413 |
| worker | 3.6 | 10.0 | 125.8 | 1.165 |
| stt-worker | 75.9 | 99.7 | 118.3 | 4.132 |
| postgres | 6.6 | 29.0 | 2213.7 | 33.961 |
| redis | 4.1 | 7.0 | 25.4 | 1.147 |
| client | 4.4 | 10.0 | 183.9 | 18.752 |

## C — 느린 뷰어 20 %, N=100

_seed 42 · git `bd9b2df45932` · 2026-09-04T11:29:57+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| 녹음기 ack p95 (ms) | 316 |
| A N=100 의 ack p95 (ms) | 308 |
| 변화 (%) | 2.4 |
| 느린 뷰어 수 | 20 |
| 폐기된 partial (`ws_dropped_partials_total`) | 0 |
| 배달된 final | 0 |
| loss | 0 |

## D — 카오스, N=100 (소켓 강제 종료 10 %/10 s · Redis flush 30 s · stt-worker SIGSTOP 15 s)

_seed 42 · git `bd9b2df45932` · 2026-09-04T11:32:01+00:00 · Intel(R) Xeon(R) Processor @ 2.10GHz x4 · RAM 15.7 GB · Python 3.11.15 · PG 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)_

| 항목 | 값 |
|---|---|
| 재접속 시도 / resume 성공 | 140 / 140 |
| resume 성공률 (%) | 100.0 |
| superseded(4409) 종료 | 0 |
| 원장 rebuild (`stt_rebuilds_total`) | 135 |
| `stt_offsets.last_chunk_seq == final_seq` 세션 비율 (%) | 70.0 |
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
| 40.0 | stt_sigstop | 2594 |
| 50.0 | kill_sockets | 10 |
| 55.0 | stt_sigcont | 2594 |

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
