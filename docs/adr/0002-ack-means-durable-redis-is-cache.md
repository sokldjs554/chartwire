# ADR-0002 — `ack` 는 PostgreSQL 커밋을 뜻한다; Redis 는 재구축 가능한 캐시다

상태: 채택 (2026-09-02) · 관련: spec §0.3, §5, §6.4, §6.6, §7.4, `docs/ops/runbook.md`

## 맥락

진료실 Wi-Fi 는 끊긴다. 녹음기는 `ack` 를 받은 청크를 링 버퍼에서 버린다(§6.4). 따라서 `ack` 가 "받았다"가 아니라
"잃어버리지 않는다"를 뜻해야 오디오 유실이 없다. Redis 스트림에 XADD 한 뒤 바로 ack 하면 빠르지만,
Redis 재시작·`MAXLEN` 트리밍·`FLUSHALL` 한 번에 청크가 사라진다.

## 결정

1. seq *n* 에 대한 누적 `ack` 는 **`audio_chunks` 1..*n* 행이 PostgreSQL 에 커밋된 뒤에만** 보낸다.
   `ws/ledger.LedgerBatcher` 가 50 ms / 500 행 단위로 묶어 커밋하고, 커밋 후 `IngestCore.on_ledgered(seqs)` 가 ack 를 만든다.
   `IngestCore` 불변식 `ack_seq <= ledger_seq` 는 hypothesis 로 검증한다.
2. 오디오 바이트는 객체 저장소(세션 DEK 로 암호화), 메타데이터는 `audio_chunks` — 이 둘이 진실이다.
3. Redis 가 들고 있는 것(`sess:{sid}` 해시, `sess:{sid}:chunks` 스트림, 소비자 그룹, `stt:lag`, 티켓)은 전부 **재구축 가능**해야 한다:
   해시는 `sessions`/`audio_chunks` 에서 rehydrate, 스트림 갭은 stt-worker 가 `audio_chunks` + 객체 저장소에서 rebuild(§7.4).
4. Redis 장애 시 ingest 는 **fail-closed**: ack 를 보내지 않고 `error 4503(retryable)`, 3회 연속 실패면 연결을 닫는다. 잘못된 ack 보다 느린 ack 가 낫다.

## 결과

- ack RTT 에 약 50 ms 의 바닥이 생긴다(배치 플러시 주기). README 는 이를 숨기지 않고 측정값과 함께 명시한다.
- Redis 는 `save ""` 로 띄워도 된다(compose). 캐시 손실 테스트(`FLUSHALL` + SIGSTOP 복구)가 stt-worker 게이트에 포함된다.
- 클라이언트 재개(resume)는 `welcome{ack_seq, missing}` 만으로 충분하다 — 서버의 진실은 원장 하나뿐이므로 "누가 맞는가"를 협상할 필요가 없다.
- 오디오 유실/중복 0 은 이 결정 덕분에 "설계상 참"이 아니라 시나리오 A·C 에서 **측정**되는 값이 된다.
