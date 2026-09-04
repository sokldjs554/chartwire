# 동의 · 파기 · 보존 (consent, purge, retention)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음.
> 이 문서는 spec §8.3–§8.4 의 구현을 설명한다. 코드: `src/chartwire/consent/`, `src/chartwire/purge/`,
> `src/chartwire/api/routers/{consents,purge}.py`, `src/chartwire/worker/handlers/purge_{run,verify}.py`.
> 결정 배경은 ADR-0004(`docs/adr/0004-retention-vs-purge-split.md`).

## 1. 동의 범위(scopes)

| 범위 | 허용하는 것 | 게이트 위치 |
|---|---|---|
| `recording` | 세션 생성, 오디오 수신 | `POST /v1/sessions` (`CW-4031`), WS hello (`4011`) |
| `transcription` | STT 어댑터 호출, `transcript_segments` 저장 | stt-worker 청크 처리 |
| `search_index` | 평문 검색 행 `segment_search` 저장 | stt-worker 최종 세그먼트 트랜잭션 |
| `ai_drafting` | SOAP 초안 생성 | `note_draft` 핸들러 (`abstained: consent_scope_missing`) |

동의는 **버전 행**이다(`consents.version`, `UNIQUE (patient_id, version)`). `POST /v1/patients/{id}/consents` 는
항상 새 버전을 만들고(`consent.granted` 감사), `active_scopes` 는 **가장 높은 버전 한 행**만 본다 — 그 행이 철회됐으면
이전 버전이 되살아나지 않고 빈 집합이다(`tests/unit/test_consent_gates.py`). `sessions.scopes_snapshot` 은 생성 시점의
기록일 뿐이며, 모든 게이트는 라이브 동의 행을 다시 읽는다. 게이트는 전부 fail-closed 다: 동의 행이 없으면 거부다.

## 2. 철회 (`POST /v1/consents/{id}/revoke`)

한 트랜잭션에서 다음이 함께 커밋되거나 함께 실패한다(`consent/service.py::revoke`):

1. `consents.revoked_at/revoked_by/revoked_reason`
2. `patients.consent_state = 'revoked'`
3. 환자 단위 `purge_jobs` 행(`reason='consent_revoked'`, `state='queued'`)
4. 아웃박스 `consent.revoked {patient_id, consent_id, purge_job_id}` (멱등 키 `consent.revoked:{consent_id}:{version}`)
5. 감사 `consent.revoked`, `purge.requested`

커밋 뒤에 Redis 효과가 따른다: 환자의 `recording/paused` 세션마다 `ctl:{sid}` 에 `{"t":"consent_revoked"}` 를 발행해
녹음기와 뷰어를 `4011` 로 닫고, `outbox:wake` 로 워커를 깨운다. 응답은 `202 {consent_id, patient_id, revoked_at,
purge_job_id, outbox_event_id, live_sessions_notified}`. 같은 동의를 다시 철회하면 `409 CW-4097`.

## 3. 파기 실행 (`purge_run`, §8.4 1–6단계)

`purge.requested`(관리자 `POST /v1/purge-jobs`) 와 `consent.revoked` 둘 다 `purge/pipeline.py::run` 으로 간다.
환자 단위 작업은 그 환자의 **모든 세션**에 세션 단계를 반복한 뒤 환자 식별자를 처리한다. 폴러 아래서는 `ctx.tenant_tx`
가 핸들러 트랜잭션에 참여하므로 아래 전부와 `processed_events` 가 하나의 커밋이다.

| 단계 | 하는 일 | `counts` 키 |
|---|---|---|
| `capture` | `state='running'`; 첫 `text_enc` 를 `sample_ciphertext` 로, `dek_wrapped` 의 sha256 을 지문으로 보관 | `sample_ciphertext`, `dek_fingerprint` |
| `redis` | `ctl:{sid}` 에 `{"t":"purge"}` (ingest/watch `4012`), `SREM stt:active`, `sess:{sid}*` 와 `stt:owner:{sid}` DEL, `stt:lag` 필드 HDEL | `redis_keys`, `stt_active`, `stt_lag` |
| `objectstore` | `delete_prefix("{tenant}/{session}/")` | `objects` |
| `rows` | `segment_search`, `transcript_segments`(모든 파티션), `risk_events`, `legal_hold IS NULL` 인 `note_statements`·`note_assessments`·`notes`, `stt_offsets`, `audio_chunks` DELETE | 테이블 이름별 행 수 |
| `crypto_shred` | `sessions.dek_wrapped = NULL, dek_destroyed_at, state='purged', purged_at`; `keys:invalidate` 발행 | `session_dek_destroyed` |
| `patient_shred` (환자 단위) | `name_enc/phone_enc = NULL`, 환자 DEK 폐기, `consent_state='purged'` | `patient_identifiers`, `patient_dek_destroyed` |
| `skipped` | 이미 파기된 세션 / 없는 세션 | `already_purged`, `missing_session` |

각 단계는 `purge_jobs.steps` 에 `{step, at, subject_id, counts}` 로 붙고(`steps || …` SQL 이라 부분 읽기가 없음)
`purge.step` 감사 행을 남긴다. 마지막에 `state='completed'`, `receipt_hash`, 아웃박스 `purge.completed`, 감사
`purge.completed`, `session.purged`. 완료·검증된 작업에 다시 실행이 오면 그대로 반환한다(재전달 안전).
`sessions_dek_guard` 트리거가 `dek_destroyed_at` 이 찍힌 행에 `dek_wrapped` 를 다시 넣는 UPDATE 를 거부하므로 툼스톤은
서비스 역할로도 되살릴 수 없다.

## 4. 파기 영수증 (purge receipt)

`GET /v1/purge-jobs/{id}` (admin, auditor) 와 `chartwire purge receipt --job` 이 돌려주는 것:

```json
{
  "id": "…", "subject_type": "session", "subject_id": "…", "reason": "admin", "state": "verified",
  "steps": [{"step": "capture", "at": "…", "subject_id": "…", "counts": {"sample_ciphertext": 1, "dek_fingerprint": 1}, "count": 2}, "…"],
  "counts": {"objects": 5, "audio_chunks": 5, "transcript_segments": 4, "segment_search": 4, "risk_events": 1, "notes": 1, "note_statements": 1, "stt_offsets": 1, "redis_keys": 4, "session_dek_destroyed": 1, "…": "…"},
  "dek_fingerprints": ["<sha256 hex of dek_wrapped>"],
  "receipt_hash": "<sha256 hex>", "receipt_hash_valid": true,
  "verify_result": {"ok": true, "checks": {"rows:<sid>": true, "objectstore:<sid>": true, "redis:<sid>": true, "dek_null:<sid>": true, "unwrap:<sid>": true, "decrypt_sample:<sid>": true}, "failed": [], "detail": {}},
  "artifact": "파기 영수증 (purge receipt)"
}
```

`receipt_hash = sha256(canonical_json({steps, counts, dek_fingerprints}))` — 정렬된 키, 공백 없음, UTF-8
(`purge/receipt.py`). 영수증을 가진 누구나 보이는 필드로 해시를 다시 계산할 수 있고, API 는 `receipt_hash_valid` 로
그 결과를 같이 보여 준다. 같은 값이 감사 `purge.completed.detail.receipt_hash` 에도 남는다.

## 5. 검증 (`purge_verify`, 7단계)과 "복호화 시도"

`purge.completed` 이벤트가 `purge/verify.py::run` 을 돌린다. 대상 세션마다:

| 검사 | 통과 조건 |
|---|---|
| `rows:{sid}` | 3절의 모든 테이블에서 해당 세션 행 수 == 0 (서명 노트 제외) |
| `objectstore:{sid}` | `list("{tenant}/{session}/") == []` |
| `redis:{sid}` | `SCAN sess:{sid}*` == 0 |
| `dek_null:{sid}` | `sessions.dek_wrapped IS NULL` 이고 `state='purged'` |
| `unwrap:{sid}` | `KeyCache.get(sid, kek_ref, None)` 이 `DekDestroyedError` |
| `decrypt_sample:{sid}` | 보관한 `sample_ciphertext` 를 테넌트 KEK 로 `Envelope.decrypt` → `DecryptError(invalid_tag)` |
| `patient_shredded` (환자 단위) | 식별자 NULL, 환자 DEK NULL, `consent_state='purged'` |

결과는 `verify_result` 로 저장되고 상태는 `verified` 또는 `failed`. **검증은 자기 트랜잭션에서 먼저 커밋한 뒤** 실패면
예외를 던진다 — 예외는 아웃박스 재시도(백오프)와 8회 뒤 DLQ 로 이어지고, `failed` 상태는 그 전에 이미 영구적이다.
운영자가 잔존물을 치우고 `POST /v1/ops/dead-letters/{id}/replay`(또는 `chartwire outbox dlq replay --id`) 하면
재검증된다. `chartwire purge verify --job` 은 같은 검사를 손으로 돌리며 실패 시 종료 코드 1.

`POST /v1/purge-jobs/{id}/verify-decrypt` 는 저장된 판정을 읽지 않고 **지금** 언래핑과 샘플 복호화를 다시 시도해
`{"decrypt_attempted": true, "unwrap": "failed:dek_destroyed", "decrypt_sample": "failed:invalid_tag", "job_state": "verified"}`
를 돌려준다(콘솔 "복호화 시도" 버튼). 파기 전에 부르면 `unwrap: "succeeded"` 가 나오므로 뒤의 실패가 의미를 갖는다.

## 6. 법적으로 남는 것

| 남는 것 | 이유 | 열쇠 |
|---|---|---|
| `notes` 중 `legal_hold='medical_record'` (`signed_content_enc`) + `note_assessments` | 의료법 진료기록 10년 보존(`retention_until = signed_at + 10년`) | 테넌트 record key — 파기에서 절대 폐기하지 않음 |
| `sessions` 툼스톤 (`state='purged'`, `dek_wrapped NULL`, 시각·id 만) | 영수증과 감사가 가리킬 대상 | 없음(PHI 없음) |
| `audit_events` | 추가 전용, 삭제하지 않음 (`trg_audit_immutable`) | 없음(id·카운트·해시만) |
| `purge_jobs` | 영수증 자체 | 없음(`sample_ciphertext` 는 키 없는 암호문) |

서명 문서는 인용문을 안에 품고 있어서 원본 세그먼트가 사라져도 그대로 읽힌다(`tests/integration/test_notes_rest.py::
test_signed_note_survives_purge`, WP-D). 서명 전 초안(`legal_hold IS NULL`)은 파기와 함께 사라지고, 파기된 세션에는 새
초안을 만들 수 없다(`410 CW-4100`).

## 7. 명시된 한계

- **WAL, base backup, VACUUM 전 dead tuple** 은 영수증 범위 밖이다. 거기 남는 것은 세션 DEK 로만 열리는 암호문이며 그
  DEK 는 `crypto_shred` 단계에서 사라졌다 — 그러나 이 저장소는 백업 안의 바이트를 지우지 않으며 지웠다고 말하지 않는다.
- 영수증은 "파기 영수증"이다. 증명서·certificate·proof 라는 단어는 쓰지 않는다.
- `KeyCache` 는 프로세스마다 있다. `keys:invalidate` 를 못 받은 노드는 최대 TTL(600 s) 동안 언래핑된 DEK 를 들고 있을 수
  있다. 그 노드도 `dek_wrapped IS NULL` 인 행을 읽는 순간 `DekDestroyedError` 를 내므로 새 요청은 막히지만, 이미
  메모리에 있는 키 바이트가 즉시 0 이 되지는 않는다.
- 환자 단위 파기는 세션을 순서대로 처리하는 한 트랜잭션이다. 세션이 아주 많은 환자는 리스(300 s)를 넘길 수 있고, 그 경우
  다른 워커가 같은 행을 잡더라도 `processed_events` PK 가 두 번째 커밋을 막는다(WP-G 런타임).
- 로그·메트릭에는 id 와 카운트만 남는다(`tests/integration/test_phi_logs.py`).

## 8. 명령

```bash
chartwire purge run --tenant <uuid> --session <uuid>      # 만들고 실행하고 검증까지, 영수증 출력
chartwire purge run --tenant <uuid> --patient <uuid>      # 환자 단위
chartwire purge receipt --job <uuid> [--tenant <uuid>]    # 영수증 JSON
chartwire purge verify --job <uuid>                       # 7단계 재검증, 실패 시 exit 1
chartwire audit list --tenant <uuid> --action purge.completed
```
