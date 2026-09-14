"""Behavioural protocol checks for the redesigned product console."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console" / "index.html"


def _script_without_boot() -> str:
    html = CONSOLE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    # 부팅 블록은 DOM·네트워크를 건드리므로 하네스에서는 잘라 낸다. 첫 문장이 아니라
    # `/* boot */` 표식에 거는 이유: 부팅 순서가 바뀌어도 이 테스트가 깨지지 않게.
    stripped, n = re.subn(r"/\* boot \*/\(async\(\)=>\{.*?\}\)\(\);\s*$", "", scripts[0], flags=re.S)
    assert n == 1, "console script must end with a `/* boot */(async()=>{…})();` block"
    return stripped


@pytest.fixture(scope="module")
def node() -> str:
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed")
    return exe


HARNESS = r"""
const assert=require('assert');
const makeEl=()=>{const el={style:{},dataset:{},classList:{add(){},remove(){},toggle(){}},children:[]};el.addEventListener=()=>{};el.appendChild=c=>{el.children.push(c);return c};el.querySelector=()=>null;el.querySelectorAll=()=>[];el.setAttribute=()=>{};el.dispatchEvent=()=>{};el.remove=()=>{};el.closest=()=>null;el.scrollIntoView=()=>{};Object.defineProperty(el,'innerHTML',{get:()=>'',set(){}});Object.defineProperty(el,'textContent',{get:()=>'',set(){}});el.value='';el.disabled=false;el.childElementCount=0;el.firstElementChild=null;el.scrollTop=0;return el};
const els=new Map();
global.document={getElementById:id=>{if(!els.has(id))els.set(id,makeEl());return els.get(id)},querySelectorAll:()=>[],querySelector:()=>null,createElement:()=>makeEl()};
global.window={scrollTo(){},addEventListener(){}};
global.location={hostname:'chartwire.onrender.com',origin:'http://test'};
global.performance={now:()=>Date.now()};
global.fetch=async()=>{throw new Error('no network')};
global.crypto={randomUUID:()=> '00000000-0000-4000-8000-000000000000'};
const sockets=[];
class FakeWebSocket{constructor(url){this.url=url;this.readyState=1;this.sent=[];this.closed=null;sockets.push(this);setTimeout(()=>this.onopen&&this.onopen(),0)}send(d){this.sent.push(d)}close(code){this.closed=code||1000;this.readyState=3;setTimeout(()=>this.onclose&&this.onclose({code:this.closed,reason:''}),0)}push(m){this.onmessage&&this.onmessage({data:JSON.stringify(m)})}frames(){return this.sent.filter(d=>d instanceof ArrayBuffer)}texts(){return this.sent.filter(d=>typeof d==='string').map(d=>JSON.parse(d))}}
FakeWebSocket.OPEN=1;global.WebSocket=FakeWebSocket;
eval(require('fs').readFileSync(process.argv[2],'utf8')+'\n;globalThis.__cw={Recorder,state,useTicket:(f)=>{wsTicket=f},useWatch:(f)=>{startWatch=f}};');
const {Recorder,state,useTicket,useWatch}=globalThis.__cw;useTicket(async kind=>`tk-${kind}-${sockets.length+1}`);useWatch(async()=>{});state.sessionId='sess-1';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const header=buf=>{const dv=new DataView(buf);return {magic:dv.getUint16(0,true),ver:dv.getUint8(2),flags:dv.getUint8(3),seq:dv.getUint32(4,true),off:dv.getUint32(8,true),len:buf.byteLength-12}};
(async()=>{const rec=new Recorder(12);rec.seed=7;await rec.start();await sleep(10);const s1=sockets[0];assert.strictEqual(s1.url,'ws://test/ws/v1/ingest');assert.deepStrictEqual(s1.texts()[0],{t:'hello',ticket:'tk-ingest-1',proto:1,codec:'pcm16le',sample_rate:16000,chunk_ms:200,resume:false});s1.push({t:'welcome',epoch:1,ack_seq:0,credit:2,missing:[]});await sleep(90);assert.strictEqual(s1.frames().length,2);assert.deepStrictEqual(header(s1.frames()[0]),{magic:0x4357,ver:1,flags:4,seq:1,off:0,len:6400});s1.push({t:'ack',ack_seq:2,credit:3});await sleep(120);assert.strictEqual(s1.frames().length,5);assert.strictEqual(rec.ackSeq,2);s1.push({t:'ping',ts:42});assert.deepStrictEqual(s1.texts().pop(),{t:'pong',ts:42});rec.dropNetwork(20);assert.strictEqual(s1.closed,null);await sleep(50);const s2=sockets[1];assert.ok(s2);const h2=s2.texts()[0];assert.strictEqual(h2.resume,true);assert.strictEqual(h2.last_sent_seq,5);s2.push({t:'welcome',epoch:2,ack_seq:3,credit:50,missing:[[4,5]]});await sleep(100);assert.deepStrictEqual(s2.frames().slice(0,2).map(f=>header(f).seq),[4,5]);assert.strictEqual(rec.epoch,2);s2.push({t:'ack',ack_seq:11,credit:50});await sleep(350);assert.deepStrictEqual(s2.texts().find(m=>m.t==='end'),{t:'end',final_seq:12});s2.push({t:'nack',missing:[[12,12]]});await sleep(50);assert.strictEqual(header(s2.frames().pop()).seq,12);s2.push({t:'bye',reason:'ended',ack_seq:12});assert.strictEqual(rec.done,true);assert.strictEqual(rec.outcome,'ended');assert.strictEqual(rec.lastSentSeq,12);assert.ok(header(s2.frames().find(f=>header(f).seq===12)).flags&1);console.log(JSON.stringify({ok:true,reconnects:rec.reconnects}))})().catch(e=>{console.error(e.stack||e);process.exit(1)});
"""


def test_recorder_resume_credit_and_final_chunk(node: str, tmp_path: Path) -> None:
    js = tmp_path / "console.js"
    js.write_text(_script_without_boot(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(js)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"ok": True, "reconnects": 1}


EVIDENCE_HARNESS = r"""
const assert=require('assert');
const makeEl=()=>{const el={style:{},dataset:{},classList:{add(){},remove(){},toggle(){}},children:[]};el.addEventListener=()=>{};el.appendChild=c=>{el.children.push(c);return c};el.querySelector=()=>null;el.querySelectorAll=()=>[];el.setAttribute=()=>{};el.remove=()=>{};el.closest=()=>null;el.scrollIntoView=()=>{};Object.defineProperty(el,'innerHTML',{get:()=>'',set(){}});Object.defineProperty(el,'textContent',{get:()=>'',set(){}});el.value='';el.disabled=false;return el};
const els=new Map();
global.document={getElementById:id=>{if(!els.has(id))els.set(id,makeEl());return els.get(id)},querySelectorAll:()=>[],querySelector:()=>null,createElement:()=>makeEl()};
global.window={scrollTo(){},addEventListener(){}};
global.location={hostname:'localhost',origin:'http://test'};
global.performance={now:()=>Date.now()};
global.fetch=async()=>{throw new Error('no backend')};
global.crypto={randomUUID:()=>'00000000-0000-4000-8000-000000000000'};
global.WebSocket=class{constructor(){this.readyState=1}send(){}close(){}};
eval(require('fs').readFileSync(process.argv[2],'utf8')+'\n;globalThis.__cw={api,detectMode,state,preview,isPreview:()=>PREVIEW_MODE};');
// eval 한 스크립트와 같은 스코프라 이름이 겹치면 안 된다 — 전부 cw* 로 받는다.
const cwApi=globalThis.__cw.api, cwDetect=globalThis.__cw.detectMode;
const cwPreview=globalThis.__cw.preview, cwIsPreview=globalThis.__cw.isPreview;
const refused=async(method,path)=>{try{await cwApi(method,path);return null}catch(e){return e}};
(async()=>{
  // 백엔드가 없으면(fetch throw) 미리보기로 떨어진다.
  await cwDetect();
  assert.strictEqual(cwIsPreview(),true,'no backend must fall back to preview');

  // 증적 경로는 미리보기 답을 주지 않는다 — 지어낸 영수증이 화면에 찍히면 안 된다.
  for(const [m,p] of [['POST','/v1/purge-jobs'],['GET','/v1/purge-jobs/abc'],
                      ['POST','/v1/purge-jobs/abc/verify-decrypt']]){
    const e=await refused(m,p);
    assert.ok(e && e.needsBackend===true, `${m} ${p} must be refused in preview, got ${e}`);
  }

  // 증적이 아닌 경로는 미리보기에서도 화면 흐름을 위해 답한다.
  const sessions=await cwApi('GET','/v1/sessions?limit=5');
  assert.ok(sessions && Array.isArray(sessions.items),'non-evidence routes still answer in preview');

  // 동의 철회는 추적할 영수증 id 를 지어내지 않는다.
  const patient=await cwApi('POST','/v1/patients',{name:'가상환자-0001'});
  const consent=await cwApi('POST',`/v1/patients/${patient.id}/consents`,{scopes:['recording']});
  const revoked=await cwApi('POST',`/v1/consents/${consent.id}/revoke`,{reason:'test'});
  assert.strictEqual(revoked.purge_job_id,null,'preview revoke must not invent a purge job id');

  // 미리보기 영수증 객체에 'verified' 도장이 남아 있으면 안 된다.
  for(const job of cwPreview.purge.values()){
    assert.notStrictEqual(job.state,'verified','preview must never stamp a purge job verified');
  }
  console.log(JSON.stringify({ok:true}));
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
"""


def test_preview_mode_never_fabricates_purge_evidence(node: str, tmp_path: Path) -> None:
    """'VERIFIED' · 재계산 해시 · DEK 파기는 규제 증적의 언어다 — 백엔드 없이 그리면 안 된다."""
    js = tmp_path / "console.js"
    js.write_text(_script_without_boot(), encoding="utf-8")
    harness = tmp_path / "evidence.js"
    harness.write_text(EVIDENCE_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(js)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"ok": True}


def test_console_media_whitelist_matches_committed_images() -> None:
    from chartwire.api.app import CONSOLE_MEDIA

    for name, (source, mime) in CONSOLE_MEDIA.items():
        assert (ROOT / "docs" / "images" / source).is_file(), name
        assert mime in ("image/png", "image/gif")
