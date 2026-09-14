from pathlib import Path
import hashlib
import re

p = Path("console/index.html")
text = p.read_text(encoding="utf-8")

text = text.replace("<!doctype html>", "<!DOCTYPE html>", 1)
text = text.replace(
    '<div class="sidebar-foot"><div class="demo-badge"><span class="dot" id="connDot"></span><span id="connLabel">API 연결 전</span></div><p>합성 데이터 전용 데모<br>진단·치료 자동화 없음</p></div>',
    '<div class="sidebar-foot"><div class="demo-badge"><span class="dot" id="connDot"></span><span id="connLabel">API 연결 전</span></div><p>모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음<br>진단·치료 자동화 없음</p></div>',
    1,
)
text = text.replace(
    "@media(max-width:1080px){",
    "@media (prefers-color-scheme: dark){html{color-scheme:dark light}}\n@media(max-width:1080px){",
    1,
)

wire = "const WIRE={MAGIC:0x4357,VER:1,FLAG_LAST:1,FLAG_SIM:4,HEADER:12,CHUNK_MS:200,CHUNK_BYTES:6400};"
close_codes = (
    "const CLOSE_CODES={1000:'normal',1006:'abnormal',1012:'service_restart',4000:'heartbeat_timeout',"
    "4001:'unauthorized',4003:'forbidden',4004:'session_not_found',4005:'bad_hello',4008:'seq_gap_unrecoverable',"
    "4009:'credit_violation',4010:'bad_frame',4011:'consent_missing',4012:'session_ended',4013:'viewer_too_slow',"
    "4409:'superseded',4503:'dependency_unavailable'};\n"
    "const RESUMABLE_CLOSE=new Set([1006,1012,4000,4503]);"
)
if close_codes not in text:
    text = text.replace(wire, wire + "\n" + close_codes, 1)

start = text.index("async function startWatch(){")
end = text.index("\nfunction renderSegment", start)
watch = r'''async function startWatch(){
  if(state.watch)try{state.watch.close()}catch{}
  const ticket=await wsTicket('watch');
  const ws=new WebSocket(`${WS_BASE}/ws/v1/watch`);state.watch=ws;
  ws.onopen=()=>ws.send(JSON.stringify({t:'hello',ticket}));
  ws.onmessage=ev=>{
    if(typeof ev.data!=='string')return;
    const m=JSON.parse(ev.data);
    if(m.t==='ping'){ws.send(JSON.stringify({t:'pong',ts:m.ts}));return}
    if(m.t==='transcript.partial'){if($('liveLabel'))$('liveLabel').textContent='실시간 전사 중…';return}
    if(m.t==='transcript.final')renderSegment(m);
    if(m.t==='risk.alert'){
      state.riskAlert=m;toast('검토가 필요한 표현이 감지되었습니다.',true);
      $('sessionStatus').textContent='검토 필요';$('sessionStatus').className='status review';
      $('riskBanner').classList.add('show');$('riskTitle').textContent='검토가 필요한 표현이 감지되었습니다.';
      $('riskMeta').textContent=`${m.category||'risk signal'} · transcript #${m.segment_seq??'–'} · 사람 확인이 필요합니다.`
    }
    if(m.t==='risk.ack'&&state.riskAlert&&state.riskAlert.risk_event_id===m.risk_event_id){
      state.riskAlert=null;$('riskBanner').classList.remove('show')
    }
    if(m.t==='risk.escalated'){
      $('riskBanner').classList.add('show');$('riskMeta').textContent='SLA를 넘겨 검토 우선순위가 올라갔습니다.'
    }
    if(m.t==='note.status')loadNote(true);
    if(m.t==='session.state'){$('sessionStatus').textContent=statusKo(m.state);$('sessionStatus').className=`status ${statusClass(m.state)}`}
    if(m.t==='viewer.lagged')opsLog(`viewer lagged · dropped partials ${m.dropped_partials??'?'}`);
    if(m.t==='viewer.degraded')opsLog('viewer degraded · replay로 복구');
    if(m.t==='viewer.presence')opsLog(`viewer presence · ${m.count??'?'}`);
  };
  ws.onclose=()=>{if($('liveLabel'))$('liveLabel').textContent='전사 연결 종료'};
}'''
text = text[:start] + watch + text[end:]

text = text.replace(
    "this.zombies=[];\n  }\n  log(msg)",
    "this.zombies=[];this.outcome='running';\n  }\n  get ackSeq(){return this.ack}\n  get lastSentSeq(){return this.lastSent}\n  log(msg)",
    1,
)
text = text.replace(
    "this.queueMissing(m.missing||[]);this.everConnected=true;\n        $('liveLabel').textContent=resume?'재연결 완료 · 기록 계속':'실시간 기록 중';",
    "this.queueMissing(m.missing||[]);this.everConnected=true;\n        if(resume)this.log(`RESUME OK · missing ${JSON.stringify(m.missing||[])}`);\n        $('liveLabel').textContent=resume?'재연결 완료 · 기록 계속':'실시간 기록 중';",
    1,
)
text = text.replace(
    "}else if(m.t==='credit'){\n        this.credit=m.credit||0;this.tick();\n      }else if(m.t==='pause'){",
    "}else if(m.t==='credit'){\n        this.credit=m.credit||0;this.tick();\n      }else if(m.t==='ping'){\n        ws.send(JSON.stringify({t:'pong',ts:m.ts}));\n      }else if(m.t==='pause'){",
    1,
)
text = text.replace(
    "const resumable=[1006,1012,4000,4503].includes(ev.code);",
    "const resumable=RESUMABLE_CLOSE.has(ev.code);",
    1,
)
text = text.replace(
    "finish(reason){if(this.done)return;this.done=true;clearTimeout(this.timer);$('runRealtime').disabled=false;$('stopRealtime').disabled=true;this.log(`종료 · ${reason}`);state.rec=null}",
    "finish(reason){if(this.done)return;this.done=true;this.outcome=reason;clearTimeout(this.timer);$('runRealtime').disabled=false;$('stopRealtime').disabled=true;this.log(`종료 · ${reason}`);if(state.rec===this)state.rec=null}",
    1,
)
text = text.replace("5초 연결 끊기</button>", "네트워크 끊기 5초</button>", 1)
text = text.replace("Assessment · 사람이 작성</label>", "Assessment · 사람이 작성 (AI가 작성하지 않는 영역)</label>", 1)

p.write_text(text, encoding="utf-8")

b = p.read_bytes()
blob = hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()
expected = "c9aec07ecaca055d1c8295d5734bc73d042b4990"
if blob != expected:
    raise SystemExit(f"V4.1 blob mismatch: {blob} != {expected}")

scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
if len(scripts) != 1:
    raise SystemExit(f"expected one inline script, got {len(scripts)}")
Path("/tmp/chartwire-console.js").write_text(scripts[0], encoding="utf-8")
print(f"V4.1 exact blob verified: {blob}")
