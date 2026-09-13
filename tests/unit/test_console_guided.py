"""Exercise the real guide and REST client against a stateful synthetic API double.

These are flow/ownership/error tests, not claims about the PostgreSQL purge pipeline.
The existing integration suite covers those server-side contracts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_console_static import HARNESS, _script

DOM = HARNESS.split("// The console script is strict-mode")[0]
GUIDE_HARNESS = r"""
document.body = makeEl();
const originalGet = document.getElementById;
document.getElementById = (id) => {
  const el = originalGet(id);
  el.click = () => {}; // RecorderClient's wire protocol is tested by its separate scripted WS test.
  el.checked = el.checked || false;
  return el;
};
eval(require('fs').readFileSync(process.argv[2], 'utf8') + `
;globalThis.guideTest = { guided, state, soap, sessions,
  setRecorder: (r) => { recorder = r; },
  select: (id) => { state.sessionId = id; state.patientId = sessions.rows.find(s => s.id === id).patient_id; }
};`);
const { guided, state, soap, sessions, setRecorder } = guideTest;
const el = (id) => document.getElementById(id);
let id = 0, role = 'clinician', badReceipt = false, badDecrypt = false, badRetention = false, unsupported = 0;
let failRevokeOnce = false, revocationKeys = [];
const calls = [], patients = [], consents = [], rows = [], notes = new Map();
const jsonResponse = (value, status = 200) => ({ ok: status < 400, status, statusText: 'fixture', text: async () => JSON.stringify(value), json: async () => value });
const signedAt = '2026-09-13T12:00:00Z', retention = '2036-09-13T12:00:00Z';
global.fetch = async (url, opts = {}) => {
  const path = new URL(url).pathname, method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : {};
  calls.push([method, path, role]);
  if (path === '/console/scripts.json') return jsonResponse({ scripts: [{ script_ref: 's01', template: '불면', total_ms: 20000, n_expected_alerts: 0, n_utterances: 2 }] });
  if (path === '/v1/auth/token') { role = body.email.split('@')[0]; return jsonResponse({ access_token: 'synthetic-test-token', expires_in: 3600 }); }
  if (path === '/v1/me') return jsonResponse({ role, sub: role });
  if (path === '/v1/patients' && method === 'POST') { const p = { id: 'patient-' + ++id, pseudonym: 'synthetic', consent_state: 'granted' }; patients.push(p); return jsonResponse(p); }
  if (/\/patients\/[^/]+\/consents$/.test(path) && method === 'POST') { const c = { id: 'consent-' + ++id, patient_id: path.split('/')[3], scopes: body.scopes }; consents.push(c); return jsonResponse(c); }
  if (path === '/v1/sessions' && method === 'POST') {
    const s = { id: 'session-' + ++id, patient_id: body.patient_id, script_ref: body.script_ref, state: 'created', created_at: signedAt };
    rows.push(s); return jsonResponse(s);
  }
  if (path === '/v1/sessions' && method === 'GET') return jsonResponse(rows);
  if (/\/sessions\/[^/]+$/.test(path)) return jsonResponse({ ...rows.find(s => s.id === path.split('/')[3]), state: 'drafted' });
  if (/\/segments$/.test(path)) return jsonResponse([{ seq: 1, speaker: 'patient', text: '합성 발화입니다.' }]);
  if (/\/notes\/latest$/.test(path)) {
    const sid = path.split('/')[3], n = { id: 'note-' + sid, session_id: sid, status: 'ready', unsupported_count: unsupported, coverage: 1, statements: [{ id: 1, section: 'S', ordinal: 1, text: '합성 발화입니다.', verdict: 'supported', evidence: [{ seq: 1, quote: '합성 발화입니다.' }] }] };
    notes.set(n.id, n); return jsonResponse(n);
  }
  if (/\/notes\/[^/]+\/assessment$/.test(path)) return jsonResponse({});
  if (/\/notes\/[^/]+\/sign$/.test(path)) { const n = notes.get(path.split('/')[3]); Object.assign(n, { status: 'signed', signed_at: signedAt, retention_until: retention, legal_hold: 'medical_record' }); return jsonResponse(n); }
  if (/\/notes\/[^/]+$/.test(path)) { const n = notes.get(path.split('/')[3]); return jsonResponse({ ...n, ...(badRetention && role === 'auditor' ? { retention_until: null } : {}) }); }
  if (/\/consents\/[^/]+\/revoke$/.test(path)) {
    revocationKeys.push(opts.headers['Idempotency-Key']);
    if (failRevokeOnce) { failRevokeOnce = false; throw new Error('simulated interrupted response'); }
    return jsonResponse({ purge_job_id: 'job-' + path.split('/')[3] });
  }
  if (/\/purge-jobs\/[^/]+\/verify-decrypt$/.test(path)) return jsonResponse({ unwrap: 'failed:dek_destroyed', decrypt_sample: badDecrypt ? 'skipped:no_sample' : 'failed:invalid_tag', decrypt_attempted: true, job_state: 'verified' });
  if (/\/purge-jobs\/[^/]+$/.test(path)) {
    const c = consents.find(c => 'job-' + c.id === path.split('/')[3]);
    return jsonResponse({ id: path.split('/')[3], subject_type: 'patient', subject_id: badReceipt ? 'someone-else' : c.patient_id, state: 'verified', receipt_hash_valid: true, receipt_hash: 'fixture', counts: { audio_chunks: 10 }, steps: [], verify_result: { checks: { rows_gone: true, keys_gone: true } } });
  }
  if (/\/patients\/[^/]+$/.test(path)) return jsonResponse(patients.find(p => p.id === path.split('/')[3]));
  throw new Error('Unexpected request: ' + method + ' ' + path);
};
// Preserve the real session-list HTTP call while emulating a native select change in our tiny DOM.
const realRefresh = sessions.refresh.bind(sessions);
sessions.refresh = async (keep) => { await realRefresh(keep); if (keep && rows.some(s => s.id === keep)) guideTest.select(keep); };
const prepare = async () => {
  await guided.start();
  assert.ok(guided.run.sessionId, 'fresh consultation created');
  const run = guided.run;
  setRecorder({ done: true });
  guided.stream({ lastSentSeq: 10, ackSeq: 10, credit: 50, ws: {}, done: true, epoch: 1 });
  await guided.perform(run, 'note', () => guided.awaitNote(run));
  assert.equal(el('guideNext').disabled, false, 'note ready');
  await guided.next();
  assert.equal(run.phase, 2);
  assert.equal(el('guideNext').disabled, true, 'review must not auto-confirm');
  guided.sawEvidence(run.note.statements[0]); el('guideReview').checked = true; guided.buttons();
  return run;
};
(async () => {
  let run = await prepare();
  const firstPatient = run.patientId;
  await guided.next();
  assert.equal(run.complete, true);
  assert.equal(role, 'auditor', 'receipt and retained metadata use auditor permissions');
  assert.ok(calls.find(c => c[1] === '/v1/consents/' + run.consentId + '/revoke'));
  assert.ok(calls.find(c => c[1] === '/v1/notes/' + run.noteId && c[2] === 'auditor'));
  const outcomes = ['full flow'];

  await guided.next(); // repeat from the completed screen
  assert.notEqual(guided.run.patientId, firstPatient, 'repeated walkthrough gets a fresh patient');
  guided.exit();

  run = await prepare();
  const before = calls.length;
  state.patientId = 'another-patient';
  await guided.next();
  assert.equal(run.complete, false);
  assert.ok(!calls.slice(before).some(c => c[0] !== 'GET'), 'changed selection cannot mutate any patient');
  state.patientId = run.patientId; guided.exit(); outcomes.push('changed selection blocked');

  for (const failure of ['receipt', 'decrypt', 'retention']) {
    run = await prepare();
    badReceipt = failure === 'receipt'; badDecrypt = failure === 'decrypt'; badRetention = failure === 'retention';
    await guided.next();
    assert.equal(run.complete, false, failure + ' must not display success');
    assert.equal(el('guideNext').disabled, true);
    badReceipt = badDecrypt = badRetention = false;
    await guided.retry();
    assert.equal(run.complete, true, failure + ' retry verifies same run');
    guided.exit(); outcomes.push(failure + ' failure/retry');
  }
  run = await prepare(); failRevokeOnce = true; revocationKeys = [];
  await guided.next(); assert.equal(run.complete, false);
  await guided.retry(); assert.equal(run.complete, true);
  assert.equal(revocationKeys.length, 2);
  assert.equal(revocationKeys[0], revocationKeys[1], 'ambiguous network response reuses idempotency key');
  guided.exit(); outcomes.push('idempotent revoke retry');

  unsupported = 1; run = await prepare();
  assert.equal(el('guideNext').disabled, true, 'unsupported note cannot be signed');
  const signCount = calls.filter(c => c[1].endsWith('/sign')).length;
  await guided.next();
  assert.equal(calls.filter(c => c[1].endsWith('/sign')).length, signCount);
  outcomes.push('unsupported note blocked');
  console.log(JSON.stringify({ ok: true, outcomes }));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def test_guided_demo_ownership_review_and_verified_results(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "console.js"
    script.write_text(_script(), encoding="utf-8")
    harness = tmp_path / "guided.js"
    harness.write_text(DOM + GUIDE_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] is True and len(result["outcomes"]) == 7
