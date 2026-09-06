"""Static + behavioural checks for ``console/index.html`` without a browser (spec §15 gate: ``node --check``).

Three layers:

1. the file is a single self-contained document (no external scripts/styles/CDN);
2. the extracted ``<script>`` passes ``node --check`` and mentions every endpoint, message type and
   close code the console must speak (spec §6.3, §6.8, §6.9, §13.3);
3. the recorder client is executed under Node with a stub DOM and a scripted fake WebSocket, and must
   mirror ``loadtest/client.py``: 12-byte LE header (magic ``0x4357``), ``SIM`` flag, hello shape,
   credit compliance, ring-buffer re-sends after ``welcome.missing`` and ``nack``, ``end`` → ``bye``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console" / "index.html"

REQUIRED_STRINGS = [
    "모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음",
    "AI가 작성하지 않는 영역",
    "네트워크 끊기 5초",
    "prefers-color-scheme",
    # REST endpoints (§6.9)
    "/v1/auth/token",
    "/v1/sessions?limit=",  # 모든 상태를 나열한다 — 끝난 세션의 초안·영수증도 다시 볼 수 있어야 한다
    "/console/scripts.json",  # 대본 카탈로그 (템플릿 · 예상 경보 수)
    "/ws-ticket",
    "/notes/latest",
    "/assessment",
    "/sign",
    "/revoke",
    "/v1/purge-jobs",
    "/verify-decrypt",
    "/v1/ops/dead-letters",
    "/metrics",
    # WS endpoints and message types (§6.1, §6.3)
    "/ws/v1/ingest",
    "/ws/v1/watch",
    "'hello'",
    "'welcome'",
    "'ack'",
    "'nack'",
    "'credit'",
    "'pause'",
    "'resume_rec'",
    "'end'",
    "'bye'",
    "'ping'",
    "'pong'",
    "'transcript.partial'",
    "'transcript.final'",
    "'risk.alert'",
    "'risk.ack'",
    "'risk.escalated'",
    "'session.state'",
    "'note.status'",
    "'viewer.presence'",
    "'viewer.lagged'",
    "'viewer.degraded'",
    "last_sent_seq",
    "legal_hold",
    "retention_until",
    # 소개 화면 · 투어 단계 · 사람이 읽는 영수증과 복호화 판정 (raw JSON 덤프가 아니다)
    "데모 계정으로 시작",
    "5분 투어",
    "파기 영수증",
    "복호화 실패 — 의도된 결과입니다",
    "재계산 해시 일치",
]
REQUIRED_CLOSE_CODES = [4000, 4001, 4003, 4004, 4005, 4008, 4009, 4010, 4011, 4012, 4013, 4409, 4503, 1012]

# Node harness: stub DOM + fake WebSocket, then drive RecorderClient through a scripted server.
HARNESS = r"""
const assert = require('assert');
const makeEl = () => {
  const el = { style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, children: [] };
  el.addEventListener = () => {}; el.appendChild = (c) => { el.children.push(c); return c; };
  el.querySelector = () => null; el.querySelectorAll = () => []; el.setAttribute = () => {};
  el.dispatchEvent = () => {}; el.remove = () => {}; el.closest = () => null; el.getAttribute = () => null;
  Object.defineProperty(el, 'innerHTML', { get: () => '', set() {} });
  Object.defineProperty(el, 'textContent', { get: () => '', set() {} });
  el.value = ''; el.disabled = false; el.childElementCount = 0; el.firstChild = null; el.scrollTop = 0;
  return el;
};
const els = new Map();
global.document = {
  getElementById: (id) => { if (!els.has(id)) els.set(id, makeEl()); return els.get(id); },
  querySelectorAll: () => [], createElement: () => makeEl(),
};
global.window = { addEventListener() {} };
global.location = { origin: 'http://test' };
global.localStorage = { getItem: () => null, setItem() {} };
global.performance = { now: () => Date.now() };
global.Option = function (t, v) { this.text = t; this.value = v; };
global.Event = function (t) { this.type = t; };
global.prompt = () => null;
global.fetch = async () => { throw new Error('no network in harness'); };
const sockets = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url; this.readyState = 1; this.sent = []; this.closed = null; sockets.push(this);
    setTimeout(() => this.onopen && this.onopen(), 0);
  }
  send(data) { this.sent.push(data); }
  close(code) { this.closed = code || 1000; this.readyState = 3; setTimeout(() => this.onclose && this.onclose({ code: this.closed, reason: '' }), 0); }
  push(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  serverClose(code) { this.readyState = 3; this.onclose && this.onclose({ code, reason: '' }); }
  frames() { return this.sent.filter((d) => d instanceof ArrayBuffer); }
  texts() { return this.sent.filter((d) => typeof d === 'string').map((d) => JSON.parse(d)); }
}
FakeWebSocket.OPEN = 1;
global.WebSocket = FakeWebSocket;
global.crypto = { randomUUID: () => '00000000-0000-4000-8000-000000000000' };

// The console script is strict-mode with top-level const/class, so export what the harness needs from
// inside the same eval scope. wsTicket() would call the REST API; the harness answers directly.
eval(require('fs').readFileSync(process.argv[2], 'utf8')
  + '\n;globalThis.__console = { RecorderClient, state, useTicket: (f) => { wsTicket = f; } };');
const { RecorderClient, state, useTicket } = globalThis.__console;
useTicket(async (kind) => `tk-${kind}-${sockets.length + 1}`);
state.sessionId = 'sess-1';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const header = (buf) => { const dv = new DataView(buf); return { magic: dv.getUint16(0, true), ver: dv.getUint8(2), flags: dv.getUint8(3), seq: dv.getUint32(4, true), off: dv.getUint32(8, true), len: buf.byteLength - 12 }; };

(async () => {
  const log = [];
  const rec = new RecorderClient({ totalChunks: 12, seed: 7, speed: () => 100, onGauge() {}, onLog: (t) => log.push(t), onDone() {} });
  await rec.start();
  await sleep(5);
  const s1 = sockets[0];
  assert.strictEqual(s1.url, 'ws://test/ws/v1/ingest');
  assert.deepStrictEqual(s1.texts()[0], { t: 'hello', ticket: 'tk-ingest-1', proto: 1, codec: 'pcm16le', sample_rate: 16000, chunk_ms: 200, resume: false });
  s1.push({ t: 'welcome', session_id: 'sess-1', epoch: 1, ack_seq: 0, credit: 2, heartbeat_ms: 15000, missing: [] });
  await sleep(40);
  // credit compliance: credit 2, nothing acked → exactly 2 frames in flight
  assert.strictEqual(s1.frames().length, 2, 'credit=2 must cap outstanding at 2');
  const h1 = header(s1.frames()[0]);
  assert.deepStrictEqual(h1, { magic: 0x4357, ver: 1, flags: 4, seq: 1, off: 0, len: 6400 });
  assert.strictEqual(header(s1.frames()[1]).off, 200);
  assert.notDeepStrictEqual(new Uint8Array(s1.frames()[0], 12, 16), new Uint8Array(s1.frames()[1], 12, 16), 'payload differs per seq');
  s1.push({ t: 'ack', ack_seq: 2, credit: 3 });
  await sleep(40);
  assert.strictEqual(s1.frames().length, 5, 'ack 2 + credit 3 → seqs 3..5 flow, 6 waits');
  assert.strictEqual(rec.ackSeq, 2);
  s1.push({ t: 'ping', ts: 42 });
  assert.deepStrictEqual(s1.texts().pop(), { t: 'pong', ts: 42 });
  // hard network drop: no close frame, then resume with missing ranges (server had ledgered 3, lost 4..5)
  rec.dropNetwork(20);
  assert.strictEqual(s1.closed, null, 'partition must not send a close frame');
  await sleep(40);
  const s2 = sockets[1];
  assert.ok(s2, 'reconnect after partition');
  const hello2 = s2.texts()[0];
  assert.strictEqual(hello2.resume, true);
  assert.strictEqual(hello2.last_sent_seq, 5);
  s2.push({ t: 'welcome', session_id: 'sess-1', epoch: 2, ack_seq: 3, credit: 50, heartbeat_ms: 15000, missing: [[4, 5]] });
  await sleep(40);
  const resent = s2.frames().slice(0, 2).map((f) => header(f).seq);
  assert.deepStrictEqual(resent, [4, 5], 'welcome.missing re-sent from the ring buffer in order');
  assert.strictEqual(rec.epoch, 2);
  s1.serverClose(4409);   // zombie superseded by epoch fencing: ignored by the live client
  assert.strictEqual(rec.done, false);
  s2.push({ t: 'ack', ack_seq: 11, credit: 50 });
  await sleep(60);
  const end = s2.texts().find((m) => m.t === 'end');
  assert.deepStrictEqual(end, { t: 'end', final_seq: 12 });
  // nack after end (last chunk not ledgered) → re-send from the ring buffer, then bye{ended}
  s2.push({ t: 'nack', missing: [[12, 12]] });
  await sleep(20);
  assert.strictEqual(header(s2.frames().pop()).seq, 12);
  assert.strictEqual(s2.frames().length, 2 + 7 + 1, '4,5 re-sent + 6..12 + one nack re-send');
  s2.push({ t: 'bye', reason: 'ended', ack_seq: 12 });
  assert.strictEqual(rec.done, true);
  assert.strictEqual(rec.outcome, 'ended');
  assert.strictEqual(rec.lastSentSeq, 12);
  assert.ok(header(s2.frames().find((f) => header(f).seq === 12)).flags & 1, 'LAST_CHUNK on the final chunk');
  assert.ok(log.some((l) => l.includes('RESUME OK')), 'resume log line');

  // superseded stops the client without reconnecting
  sockets.length = 0;
  const rec2 = new RecorderClient({ totalChunks: 50, seed: 1, speed: () => 100, onGauge() {}, onLog() {}, onDone() {} });
  await rec2.start(); await sleep(5);
  sockets[0].push({ t: 'welcome', session_id: 'sess-1', epoch: 1, ack_seq: 0, credit: 50, heartbeat_ms: 15000, missing: [] });
  await sleep(10);
  sockets[0].push({ t: 'bye', reason: 'superseded', ack_seq: 1 });
  sockets[0].serverClose(4409);
  await sleep(50);
  assert.strictEqual(rec2.outcome, 'superseded');
  assert.strictEqual(sockets.length, 1, 'no reconnect after superseded');
  console.log(JSON.stringify({ ok: true, reconnects: rec.reconnects }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _script() -> str:
    html = CONSOLE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1, "console must carry exactly one inline <script>"
    return scripts[0]


def test_console_is_a_single_self_contained_file() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert '<html lang="ko">' in html
    assert re.search(r"<script[^>]*\bsrc=", html) is None, "no external scripts (no CDN)"
    assert re.search(r"<link[^>]*\bhref=", html) is None, "no external stylesheets"
    assert "cdn." not in html.lower() and "unpkg" not in html.lower() and "jsdelivr" not in html.lower()
    assert "license" not in html.lower(), "no license text anywhere in the console"


@pytest.mark.parametrize("needle", REQUIRED_STRINGS)
def test_console_mentions_required_string(needle: str) -> None:
    assert needle in CONSOLE.read_text(encoding="utf-8"), needle


def test_console_lists_every_close_code() -> None:
    script = _script()
    for code in REQUIRED_CLOSE_CODES:
        assert re.search(rf"\b{code}\b", script), code


def test_console_protocol_constants_match_spec() -> None:
    script = _script()
    assert "0x4357" in script and "FLAG_SIM = 4" in script and "HEADER_LEN = 12" in script
    assert "CHUNK_MS = 200" in script and "CHUNK_BYTES = 6400" in script and "RING_SIZE = 150" in script
    assert "credit + 20" in script  # server tolerance shown on the gauge


@pytest.fixture(scope="module")
def node() -> str:
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed")
    return exe


def test_console_script_passes_node_check(node: str, tmp_path: Path) -> None:
    js = tmp_path / "console.js"
    js.write_text(_script(), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_console_recorder_mirrors_protocol_client(node: str, tmp_path: Path) -> None:
    js = tmp_path / "console.js"
    js.write_text(_script(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(js)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] is True and result["reconnects"] == 1
