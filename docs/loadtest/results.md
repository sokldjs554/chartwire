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

미측정.

## B — SlowStt 400 ms, N=50 (credit → 0, 유한 큐, 평평한 RSS)

미측정.

## C — 느린 뷰어 20 %, N=100

미측정.

## D — 카오스, N=100 (소켓 강제 종료 10 %/10 s · Redis flush 30 s · stt-worker SIGSTOP 15 s)

미측정.

## H — 아웃박스 벤치 (`chartwire outbox bench`)

미측정.

## E — drain (선택)

미실행 (스펙 §11.2: 시간이 남을 때만).
