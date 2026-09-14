from pathlib import Path
import re

p = Path("console/index.html")
s = p.read_text(encoding="utf-8")

css_marker = "\n\n/* receipt-paper-v2 */"
assert css_marker in s
css = r'''

/* story-ui-v1: recruiter-facing guided demo + visual hierarchy */
.hero{position:relative;overflow:hidden;isolation:isolate}
.hero:before{content:'';position:absolute;width:360px;height:360px;border-radius:50%;right:-140px;top:-190px;background:radial-gradient(circle,rgba(66,151,123,.18),rgba(66,151,123,0) 68%);z-index:-1}
.hero:after{content:'';position:absolute;width:240px;height:240px;border-radius:50%;left:42%;bottom:-210px;background:radial-gradient(circle,rgba(230,179,92,.14),rgba(230,179,92,0) 70%);z-index:-1}
.hero-visual{background:linear-gradient(155deg,#17372e 0%,#1d493d 58%,#19352e 100%);box-shadow:0 18px 34px rgba(23,55,46,.18),inset 0 0 0 1px rgba(255,255,255,.07)}
.hero-visual .mini-row{border:1px solid rgba(255,255,255,.045);transition:transform .18s ease,background .18s ease}
.hero-visual .mini-row:hover{transform:translateX(3px);background:#285447}

.demo-journey{margin-top:18px;padding:22px;background:linear-gradient(145deg,#17322b 0%,#21483d 100%);border:1px solid #294d43;border-radius:22px;color:#fff;box-shadow:0 16px 34px rgba(23,50,43,.14)}
.journey-head{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:18px}
.journey-head h2{margin:0;font-size:19px;color:#fff}.journey-head p{margin:5px 0 0;color:#b8cbc4;font-size:12px;line-height:1.55}
.journey-badge{display:inline-flex;align-items:center;gap:7px;margin-bottom:7px;padding:5px 9px;border-radius:999px;background:rgba(120,211,174,.14);border:1px solid rgba(136,220,187,.18);color:#9fe2c5;font-size:10px;font-weight:850;letter-spacing:.06em;text-transform:uppercase}
.journey-track{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:9px}
.journey-step{position:relative;min-width:0;text-align:left;border:1px solid rgba(255,255,255,.09);background:rgba(255,255,255,.055);color:#fff;border-radius:15px;padding:13px 12px 12px;transition:transform .18s ease,background .18s ease,border-color .18s ease}
.journey-step:hover{transform:translateY(-3px);background:rgba(255,255,255,.105);border-color:rgba(159,226,197,.32)}
.journey-step:not(:last-child):after{content:'›';position:absolute;right:-8px;top:50%;transform:translateY(-50%);color:#76a393;font-size:20px;font-weight:400;z-index:2}
.journey-no{width:27px;height:27px;border-radius:9px;display:grid;place-items:center;margin-bottom:11px;background:#2f6656;color:#bdf0da;font-weight:900;font-size:11px}
.journey-step:nth-child(2) .journey-no{background:#315f70;color:#c8ebf6}.journey-step:nth-child(3) .journey-no{background:#675d33;color:#fae7a8}.journey-step:nth-child(4) .journey-no{background:#604b70;color:#ead8f5}.journey-step:nth-child(5) .journey-no{background:#704b46;color:#f5d3ce}.journey-step:nth-child(6) .journey-no{background:#34614b;color:#d4f0df}
.journey-step b{display:block;font-size:12px;line-height:1.35}.journey-step small{display:block;margin-top:5px;color:#abc0b9;font-size:9.5px;line-height:1.45}.journey-time{display:inline-block;margin-top:9px;color:#82bda8;font:750 9px ui-monospace,SFMono-Regular,Menlo,monospace}
.journey-cta{white-space:nowrap;background:#fff;color:#17322b;border:0;border-radius:11px;padding:10px 14px;font-weight:850;box-shadow:0 8px 18px rgba(0,0,0,.12)}
.journey-foot{display:flex;align-items:center;gap:8px;margin-top:13px;color:#94afa5;font-size:10px}.journey-foot strong{color:#c7ddd5}

.feature{position:relative;overflow:hidden;transition:transform .18s ease,box-shadow .18s ease}.feature:hover{transform:translateY(-4px);box-shadow:0 16px 34px rgba(31,65,55,.10)}
.feature:before{content:'';position:absolute;left:0;right:0;top:0;height:3px;background:var(--accent)}
.feature:nth-child(2):before{background:#477c93}.feature:nth-child(3):before{background:#9a7931}.feature:nth-child(4):before{background:#8a5b68}
.feature:nth-child(2) .feature-icon{background:#edf5f8;color:#477c93}.feature:nth-child(3) .feature-icon{background:#faf4e5;color:#8c6b24}.feature:nth-child(4) .feature-icon{background:#f8eef1;color:#8a5b68}
.stats .stat{position:relative;overflow:hidden;background:linear-gradient(145deg,#fff 45%,#f4faf7)}
.stats .stat:nth-child(2){background:linear-gradient(145deg,#fff 45%,#f2f7fb)}.stats .stat:nth-child(3){background:linear-gradient(145deg,#fff 45%,#fff8e9)}
.stats .stat:after{content:'';position:absolute;width:74px;height:74px;border-radius:50%;right:-26px;bottom:-32px;background:rgba(35,115,94,.07)}
.stats .stat:nth-child(2):after{background:rgba(71,124,147,.08)}.stats .stat:nth-child(3):after{background:rgba(154,121,49,.09)}
.workspace>.panel:first-child{border-top:3px solid #4b8e78}.workspace>.panel:last-child{border-top:3px solid #6d8398}
.review-layout>.panel:last-child{border-top:3px solid #9a7931}
.summary-strip .item{position:relative;overflow:hidden}.summary-strip .item:before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:#83b8a6}.summary-strip .item:nth-child(2):before{background:#d1a951}.summary-strip .item:nth-child(3):before{background:#6d8398}
.lifecycle .life:nth-child(1){background:#f8fbfa}.lifecycle .life:nth-child(2){background:#f6fafc}.lifecycle .life:nth-child(3){background:#fbf9f1}.lifecycle .life:nth-child(4){background:#fbf5f3}.lifecycle .life:nth-child(5){background:#f4faf6}
.table-card{box-shadow:0 12px 28px rgba(31,65,55,.065)}
.page-title{padding:2px 2px 4px}.page-title h1{letter-spacing:-.025em}

@media(max-width:1180px){.journey-track{grid-template-columns:repeat(3,1fr)}.journey-step:nth-child(3):after{display:none}}
@media(max-width:720px){.journey-head{align-items:flex-start;flex-direction:column}.journey-track{grid-template-columns:1fr 1fr}.journey-step:after{display:none!important}.journey-cta{width:100%}.demo-journey{padding:18px}}
'''
s = s.replace(css_marker, css + css_marker, 1)

old_guide = '''        <details class="demo-guide"><summary>5단계 데모 가이드 보기</summary><ol><li><b>대본 선택 · 상담 실행</b> — 합성 대본 중 하나를 고릅니다.</li><li><b>라이브 전사 · 위험 경보</b> — 위험 신호가 생기면 SLA 카운트다운과 확인 버튼이 표시됩니다.</li><li><b>초안 · 근거 · 사람 검토</b> — 문장을 누르면 원문 근거를 확인하고 Assessment는 사람이 작성합니다.</li><li><b>동의 철회 · 파기</b> — Outbox를 거쳐 파기 단계와 로그가 진행됩니다.</li><li><b>상세 영수증 · 복호화 검증</b> — 삭제 건수, 단계, 검증, 해시와 원본 JSON까지 확인합니다.</li></ol></details>'''
new_guide = '''        <section class="demo-journey" id="demoJourney" aria-label="60초 추천 데모 경로">
          <div class="journey-head">
            <div><span class="journey-badge">60 sec recommended demo</span><h2>면접관에게는 이 흐름만 보여주세요</h2><p>서비스 사용 흐름을 먼저 이해시키고, 필요한 순간에 백엔드 증거를 보여주는 60초 경로입니다.</p></div>
            <button class="journey-cta" id="guidedDemoStart">60초 데모 시작 →</button>
          </div>
          <div class="journey-track">
            <button class="journey-step" data-tour-page="session" data-tour-action="new"><span class="journey-no">01</span><b>새 상담</b><small>합성 대본을 고르고 세션을 준비합니다.</small><span class="journey-time">00–10s</span></button>
            <button class="journey-step" data-tour-page="session"><span class="journey-no">02</span><b>실시간 기록</b><small>WebSocket 전사와 위험 신호를 확인합니다.</small><span class="journey-time">10–25s</span></button>
            <button class="journey-step" data-tour-page="session"><span class="journey-no">03</span><b>초안 · 근거</b><small>초안 문장에서 원문 evidence를 펼칩니다.</small><span class="journey-time">25–35s</span></button>
            <button class="journey-step" data-tour-page="review"><span class="journey-no">04</span><b>사람 검토</b><small>자동 확정 대신 결정과 서명을 남깁니다.</small><span class="journey-time">35–45s</span></button>
            <button class="journey-step" data-tour-page="data"><span class="journey-no">05</span><b>파기</b><small>철회 또는 admin 파기 작업의 진행을 봅니다.</small><span class="journey-time">45–55s</span></button>
            <button class="journey-step" data-tour-page="data"><span class="journey-no">06</span><b>파기 영수증</b><small>검증 증적과 복호화 실패까지 확인합니다.</small><span class="journey-time">55–60s</span></button>
          </div>
          <div class="journey-foot"><strong>추천 원칙</strong><span>제품 흐름 먼저 · protocol/RLS/outbox/chaos는 질문이 들어오면 「시스템 상세」에서 보여줍니다.</span></div>
        </section>'''
assert old_guide in s
s = s.replace(old_guide, new_guide, 1)

# Make the receipt button unambiguous everywhere in the visible UI.
s = s.replace('>영수증 확인<', '>파기 영수증 확인<')

handler_marker = "$('startDemo').addEventListener('click',prepareNewSession);"
assert handler_marker in s
handlers = r'''document.querySelectorAll('[data-tour-page]').forEach(el=>el.addEventListener('click',()=>{
  const page=el.dataset.tourPage;
  if(el.dataset.tourAction==='new')prepareNewSession();else showPage(page);
  const title=el.querySelector('b')?.textContent||'다음 단계';
  toast(`60초 데모 · ${title}`);
}));
$('guidedDemoStart').addEventListener('click',()=>{
  prepareNewSession();
  toast('60초 데모 시작 · 대본을 선택하고 「합성 상담 실행」을 눌러주세요.');
});
'''
s = s.replace(handler_marker, handlers + handler_marker, 1)

# Guard the new recruiter-facing story UI from accidental regression.
required = [
    '60 sec recommended demo',
    'id="guidedDemoStart"',
    'data-tour-page="review"',
    'data-tour-page="data"',
    '파기 영수증',
    'story-ui-v1',
]
for token in required:
    assert token in s, token

# Keep exactly one HTML id per newly added control.
assert s.count('id="guidedDemoStart"') == 1

# Check script syntax with a temporary JS extraction in workflow separately.
p.write_text(s, encoding="utf-8")
print('story UI upgrade applied')
