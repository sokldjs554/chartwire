#!/usr/bin/env bash
# End-to-end demo check over REST (docs/dev/e2e.md, spec §15 integration order 1–4).
# Prerequisites: `make demo` (or `chartwire serve all --embedded --port 8000`) against the seeded demo DB, and
# `chartwire simulate --script <ref> --speed 4` already finished for the session passed as $1
# (or omit $1: the newest `drafted` session is used). All data is synthetic. Prints one JSON line per check.
set -euo pipefail
API=${API:-http://127.0.0.1:8000}
SID=${1:-}
login() { curl -sf -X POST "$API/v1/auth/token" -H 'content-type: application/json' \
  -d "{\"tenant_slug\":\"demo\",\"email\":\"$1@demo.clinic\",\"password\":\"demo1234!\"}" | jq -r .access_token; }
TOK=$(login clinician); ADM=$(login admin)
H="Authorization: Bearer $TOK"; HA="Authorization: Bearer $ADM"
say() { printf '%s\n' "$*"; }
fail() { say "FAIL: $*" >&2; exit 1; }

if [ -z "$SID" ]; then
  SID=$(curl -sf "$API/v1/sessions?state=drafted&limit=1" -H "$H" | jq -r '.items[0].id // empty')
  [ -n "$SID" ] || fail "drafted 세션이 없습니다 — 먼저 chartwire simulate 를 실행하세요"
fi
say "session=$SID"

# 1. session + segments persisted and decrypted (REST decrypts under the session DEK)
curl -sf "$API/v1/sessions/$SID" -H "$H" | jq -c '{check:"session", state, ack_seq, final_seq, script_ref}'
SEG=$(curl -sf "$API/v1/sessions/$SID/segments?limit=500" -H "$H")
say "$SEG" | jq -c '{check:"segments", n: length, first_seq: .[0].seq, last_seq: .[-1].seq, decrypted: ([.[] | select(.text|length>0)] | length)}'
[ "$(say "$SEG" | jq 'length')" -gt 0 ] || fail "세그먼트 없음"

# 2. at least one risk alert with an SLA deadline; acknowledge it (ZREM alerts:sla + risk.ack publish + audit)
ALERTS=$(curl -sf "$API/v1/alerts?open=1" -H "$H" | jq -c "[.[] | select(.session_id==\"$SID\")]")
say "$ALERTS" | jq -c '{check:"alerts_open", n: length, first: (.[0] | {id, category, severity, segment_seq, sla_deadline_at})}'
AID=$(say "$ALERTS" | jq -r '.[0].id // empty')
if [ -n "$AID" ]; then curl -sf -X POST "$API/v1/alerts/$AID/ack" -H "$H" | jq -c '{check:"alert_ack", id, acknowledged_at}'; fi

# 3. note drafted by the extractive provider and verified (every statement cites verbatim evidence)
NOTE=$(curl -sf "$API/v1/sessions/$SID/notes/latest" -H "$H")
say "$NOTE" | jq -c '{check:"note", id, status, provider, coverage, statement_count, unsupported_count}'
NID=$(say "$NOTE" | jq -r .id)
LAST=$(say "$NOTE" | jq -r '.statements[-1].id'); PREV=$(say "$NOTE" | jq -r '.statements[-2].id')
say "$NOTE" | jq -c '.statements[0] | {check:"evidence_sample", section, verdict, evidence: [.evidence[] | {seq, start, end, quote_len: (.quote|length)}]}'

# 4. review → assessment → sign (409 CW-4092 without the assessment; A is clinician-authored only)
curl -s -X POST "$API/v1/notes/$NID/sign" -H "$H" | jq -c '{check:"sign_without_assessment", status, code}'
curl -sf -X POST "$API/v1/notes/$NID/statements/$LAST/decision" -H "$H" -H 'content-type: application/json' \
  -d '{"decision":"reject"}' | jq -c --arg id "$LAST" '{check:"decision_reject", decision: (.statements[] | select(.id==($id|tonumber)) | .decision)}'
curl -sf -X POST "$API/v1/notes/$NID/statements/$PREV/decision" -H "$H" -H 'content-type: application/json' \
  -d '{"decision":"edit","edited_text":"2주 뒤 재진 예정"}' | jq -c --arg id "$PREV" '{check:"decision_edit", decision: (.statements[] | select(.id==($id|tonumber)) | .decision)}'
curl -sf -X PUT "$API/v1/notes/$NID/assessment" -H "$H" -H 'content-type: application/json' \
  -d '{"text":"임상가 평가(합성 데모): 증상 지속, 위험 경보 확인 후 안전 계획 논의."}' | jq -c '{check:"assessment", has: (.assessment.text|length>0)}'
curl -sf -X POST "$API/v1/notes/$NID/sign" -H "$H" | jq -c '{check:"sign", status, legal_hold, retention_until, signed_at}'

# 5. consent revoke → patient-level purge job → receipt → verify-decrypt; the signed note survives
PID=$(curl -sf "$API/v1/sessions/$SID" -H "$H" | jq -r .patient_id)
CID=$(curl -sf "$API/v1/patients/$PID/consents" -H "$H" | jq -r '[.[] | select(.revoked_at==null)] | sort_by(.version) | last | .id')
REV=$(curl -sf -X POST "$API/v1/consents/$CID/revoke" -H "$H" -H 'content-type: application/json' \
  -H "Idempotency-Key: e2e-revoke-$CID" -d '{"reason":"e2e demo"}')
say "$REV" | jq -c '{check:"revoke", purge_job_id, live_sessions_notified}'
JOB=$(say "$REV" | jq -r .purge_job_id)
for _ in $(seq 1 60); do R=$(curl -sf "$API/v1/purge-jobs/$JOB" -H "$HA"); [ "$(say "$R" | jq -r .state)" = verified ] && break; sleep 1; done
say "$R" | jq -c '{check:"purge_receipt", state, receipt_hash_valid, steps: [.steps[] | (.step // .name)], counts: {audio_chunks: .counts.audio_chunks, transcript_segments: .counts.transcript_segments, objects: .counts.objects, session_dek_destroyed: .counts.session_dek_destroyed}}'
[ "$(say "$R" | jq -r .state)" = verified ] || fail "purge job not verified: $(say "$R" | jq -r .state)"
curl -sf -X POST "$API/v1/purge-jobs/$JOB/verify-decrypt" -H "$HA" | jq -c '{check:"verify_decrypt", decrypt_attempted, unwrap, decrypt_sample}'
curl -s "$API/v1/sessions/$SID/segments" -H "$H" | jq -c '{check:"segments_after_purge", status, code}'
curl -sf "$API/v1/notes/$NID" -H "$H" | jq -c '{check:"signed_note_after_purge", status, legal_hold, statement_count, quote_len: (.statements[0].evidence[0].quote|length), assessment_len: (.assessment.text|length)}'
curl -sf "$API/v1/patients/$PID" -H "$H" | jq -c '{check:"patient_after_purge", pseudonym, name, consent_state}'
say "OK"
