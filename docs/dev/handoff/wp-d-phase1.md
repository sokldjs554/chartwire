# WP-D Phase 1 핸드오프 — Notes 서비스 · note_draft 핸들러 · REST (스펙 §9, §6.9, §7.2, §8.4)

> 상태: **진행 중** (2026-09-03). 중단되면 아래 체크리스트 순서대로 이어서 작업한다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a; export CHARTWIRE_TEST_DB=chartwire_test_d CHARTWIRE_TEST_REDIS_DB=4`
> 라이브 포트 8103 (REST 테스트는 in-process ASGI 이므로 포트를 쓰지 않는다).

## 0. 체크리스트 (재개용)

- [ ] `notes/repo_adapter.py` — `SegmentRow` → `SegmentView` (세션 DEK 복호화, AAD = `ws.watch.segment_aad`), `load_segment_views`
- [ ] `notes/service.py` — `draft_for_session(ctx, session_id, *, tenant_id=None, provider=None) -> NoteOutcome`,
      `provider_from_settings`, `NoteView` 읽기(`get_note`/`latest_note`), `decide_statement`, `put_assessment`, `sign`, `request_draft`
- [ ] `worker/handlers/note_draft.py` — `@handler("session.transcribed", lease_s=120)`
- [ ] `api/routers/notes.py` — §6.9 노트 라우트 6개, 감사, auditor 메타데이터 전용
- [ ] `tests/integration/test_notes_service.py` — 추출형 초안 → verified, 왕복 복호화, 기권 경로 3종, 녹음 픽스처 e2e, 핸들러 등록/실행
- [ ] `tests/integration/test_notes_rest.py` — decision/assessment/sign 흐름과 거부, auditor, 파기 후 서명 노트 가독
- [ ] `docs/grounding.md` §7·§8 갱신 (서명 노트·기록 키)
- [ ] ruff check / ruff format / mypy(notes 패키지) / 최종 테스트, 이 문서 완성

## 1. 설계 메모 (구현 중 참조)

- 트랜잭션: 읽기 tx(세션·동의·세그먼트) → 프로바이더 호출(tx 밖) → 쓰기 tx(notes/note_statements/sessions/audit). 폴러 아래서는 `ctx.tenant_tx` 가 핸들러 tx 에 참여하므로 둘 다 같은 tx.
- AAD: 세그먼트 `segment_aad(tenant, sid, seq)`; 문장 `aad(tenant,"note",note_id,f"statement:{section}:{ordinal}")`, 편집본 `…:edited`; raw 초안 `aad(tenant,"note",note_id,"raw_draft")`(세션 DEK); 서명본 `aad(tenant,"note",note_id,"signed_content")`, 평가 `aad(tenant,"note",note_id,"assessment")`(기록 키).
- 기록 키: `keycache.get(f"record:{tenant_id}", tenant.kek_ref, tenant.record_key_wrapped)` — 절대 파기되지 않는 스코프.
- 서명 조건: 평가 존재 + `unsupported` 문장 전부 reject/edit. 서명본 = 수락/편집 문장(섹션별) + 인용 원문 + 평가 + 임상가 id + 타임스탬프 + 세션 id.
