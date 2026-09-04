# ADR-0004 — 동의 철회 파기와 진료기록 보존을 키로 분리한다

상태: 채택 (2026-09-04) · 관련: spec §0.6, §8.3, §8.4, `docs/consent-purge.md`, `docs/grounding.md` §8

## 맥락

환자가 녹음·전사 동의를 철회하면 그 데이터는 지워야 한다. 그런데 같은 상담에서 임상의가 **서명한 진료기록**은
의료법상 10년 보존 의무가 있다(진료기록부). 두 요구는 같은 세션 안에서 충돌한다: 오디오와 전사문은 "즉시 파기",
그 위에 쓴 서명 노트는 "10년 보존". 흔한 타협은 (a) 전부 남기고 "파기 요청됨" 플래그만 두거나, (b) 전부 지우고
법적 보존을 포기하는 것인데, 둘 다 틀렸다.

또 "지웠다"는 말은 PostgreSQL 에서 약하다. DELETE 뒤에도 dead tuple 은 VACUUM 전까지, WAL 과 base backup 은
보존 정책이 끝날 때까지 바이트가 남는다. 복구 불가능성을 행 삭제만으로 주장할 수 없다.

## 결정

1. **두 개의 키 스코프.** 세션마다 DEK 하나(오디오 청크, `text_enc`, `raw_draft_enc`, 초안 문장), 환자마다 DEK 하나
   (`name_enc`, `phone_enc`), 테넌트마다 **record key** 하나(`tenants.record_key_wrapped`, 서명 노트·Assessment).
   record key 는 어떤 파기에서도 폐기되지 않는다.
2. **서명 = 자기완결 문서로 재봉인.** `POST /notes/{id}/sign` 은 수락·수정된 S/O/P 문장, 각 문장의 **축어적 인용문**,
   Assessment, 임상의 id, 시각, 세션 id 를 하나의 JSON 으로 만들어 record key 로 암호화해 `signed_content_enc` 에 넣고
   `legal_hold='medical_record'`, `retention_until = signed_at + 10년` 을 찍는다. 서명 뒤 노트는 세션 DEK 를 다시
   필요로 하지 않는다 — 인용문이 문서 안에 박혀 있으므로 원본 세그먼트가 사라져도 읽힌다.
3. **파기 = crypto-shred + hard delete.** `purge_run` 은 (1) 세션 DEK 지문과 암호문 샘플을 기록, (2) 라이브 연결 종료와
   Redis 키 삭제, (3) 오브젝트 스토어 prefix 삭제, (4) `segment_search`, `transcript_segments`, `risk_events`,
   `legal_hold IS NULL` 인 `notes`/`note_statements`/`note_assessments`, `stt_offsets`, `audio_chunks` 삭제,
   (5) `sessions.dek_wrapped = NULL` + `dek_destroyed_at` (트리거가 복원을 막음), 환자 단위면 식별자와 환자 DEK 도,
   (6) `keys:invalidate` 발행과 영수증 해시. 한 트랜잭션이며 단계마다 멱등이다.
4. **남는 것은 명시적이다.** `legal_hold='medical_record'` 노트와 그 Assessment, PHI 가 없는 `sessions` 툼스톤,
   `audit_events`(추가 전용, 절대 삭제하지 않음), `purge_jobs` 영수증 행.
5. **잔존물은 키가 없다.** WAL·백업·dead tuple 에 남는 것은 세션 DEK 로만 열리는 암호문이고 그 DEK 는 (5) 에서
   사라졌다. `purge_verify` 는 실제로 언래핑(`DekDestroyedError`)과 샘플 복호화(`DecryptError`)를 시도해 이를 기록하고,
   콘솔의 "복호화 시도" 버튼(`POST /purge-jobs/{id}/verify-decrypt`)이 같은 코드를 호출한다.
6. **이름은 영수증이다.** `purge_jobs` 의 `steps/counts/dek_fingerprints` 와 `receipt_hash = sha256(canonical_json(...))`
   를 **파기 영수증(purge receipt)** 이라 부른다. "증명서", "certificate", "proof" 라는 말은 코드·문서·콘솔 어디에도
   쓰지 않는다. 영수증이 다루는 범위는 라이브 테이블·오브젝트 스토어·Redis 이고, WAL/백업/VACUUM 은 범위 밖임을
   `docs/consent-purge.md` 와 위협 모델 7번 행에 적는다.

## 결과

- 서명 노트는 파기 뒤에도 그대로 읽힌다(`tests/integration/test_notes_rest.py`, `test_purge_pipeline.py`). 서명 전
  초안은 파기와 함께 사라지고, 파기된 세션에서 새 초안은 만들 수 없다.
- 세션 DEK 로 암호화된 것이 "파기 대상", record key 로 암호화된 것이 "보존 대상" — 어떤 컬럼이 어느 쪽인지 키 스코프가
  말해 주므로 새 컬럼을 추가할 때 결정이 강제된다.
- 영수증 해시는 `steps/counts/dek_fingerprints` 만 덮는다. 감사 로그(`purge.step`, `purge.completed`, `purge.verified`)
  가 같은 값을 들고 있어 두 기록을 대조할 수 있다.
- 검증 실패는 `purge_jobs.state='failed'` 로 남고 이벤트는 DLQ 로 간다. 운영자는 잔존물을 치운 뒤 `dlq replay` 로
  재검증한다(`test_verification_failure_is_durable_and_reaches_the_dlq`).
- 백업 안의 암호문은 이 저장소가 다루지 않는다. 키를 없앴다는 사실과 그 한계를 함께 적는 것이 이 ADR 의 요지다.
