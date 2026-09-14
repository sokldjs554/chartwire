from __future__ import annotations

import re
from pathlib import Path

# One-time migration: keep evaluator guidance in README, not in the live product UI.
console_path = Path("console/index.html")
readme_path = Path("README.md")

html = console_path.read_text(encoding="utf-8")

# The public product UI should behave like a product, not explain how an interviewer
# should watch it. Keep the visual hierarchy improvements, but remove the evaluator-
# facing 60-second tour from the live console.
html, n = re.subn(
    r"\n\s*<section class=\"demo-journey\" id=\"demoJourney\".*?</section>\n",
    "\n",
    html,
    count=1,
    flags=re.S,
)
assert n == 1, "demo journey section not found"

# Remove the click wiring that existed only for the live guided-tour block.
html, n = re.subn(
    r"\ndocument\.querySelectorAll\(['\"]\.journey-step['\"]\).*?\$\(['\"]guidedDemoStart['\"]\)\.addEventListener\('click',\(\)=>\{.*?\n\}\);",
    "",
    html,
    count=1,
    flags=re.S,
)
assert n == 1, "guided demo JS block not found"

# Remove journey-only styles while keeping the general visual-hierarchy upgrade
# (hero depth, varied feature cards, panels, stats, lifecycle colors).
html = re.sub(
    r"\n\.demo-journey\{.*?\.journey-foot strong\{[^\n]*\}\n",
    "\n",
    html,
    count=1,
    flags=re.S,
)
html = re.sub(r"\n@media\(max-width:1180px\)\{\.journey-track.*?\}\n", "\n", html, count=1)
html = re.sub(r"\n@media\(max-width:720px\)\{\.journey-head.*?\}\n", "\n", html, count=1)
html = html.replace(
    "/* story-ui-v1: recruiter-facing guided demo + visual hierarchy */",
    "/* visual-hierarchy-v2: product-facing visual hierarchy */",
)

for forbidden in (
    "60 sec recommended demo",
    "면접관에게는 이 흐름만 보여주세요",
    'id="guidedDemoStart"',
    'class="demo-journey"',
):
    assert forbidden not in html, forbidden

assert "파기 영수증 확인" in html
assert "visual-hierarchy-v2" in html
console_path.write_text(html, encoding="utf-8")

readme = readme_path.read_text(encoding="utf-8")
heading = "## 추천 시연 경로 (약 60초)"
if heading not in readme:
    anchor = "<!-- 링크는 배포가 실제로 동작하는 동안에만 둔다 (spec §12.2). 내려갔으면 이 두 줄을 지운다. -->\n"
    block = """

## 추천 시연 경로 (약 60초)

공개 데모: <https://chartwire.onrender.com>

`새 상담 → 실시간 기록 → 초안·근거 → 사람 검토 → 파기 → 파기 영수증`

1. **00–10초 · 새 상담** — 합성 대본을 선택하고 세션을 시작합니다.
2. **10–25초 · 실시간 기록** — WebSocket 전사와 위험 신호/SLA를 확인합니다.
3. **25–35초 · 초안·근거** — 초안 문장을 눌러 원문 evidence를 펼칩니다.
4. **35–45초 · 사람 검토** — 문장 결정을 남기고 Assessment를 작성한 뒤 서명합니다.
5. **45–55초 · 파기** — 동의 철회 또는 admin 파기 작업의 진행 상태를 확인합니다.
6. **55–60초 · 파기 영수증** — 삭제·키 폐기 증적과 복호화 실패 검증을 확인합니다.

> 제품 흐름을 먼저 보여주고, `seq/ack/credit/resume`, RLS, outbox/DLQ, chaos 결과는 질문이 들어오면 **시스템 상세**와 아래 기술 문서에서 보여주는 구성을 권장합니다.
"""
    assert anchor in readme, "README insertion anchor not found"
    readme = readme.replace(anchor, anchor + block, 1)

readme_path.write_text(readme, encoding="utf-8")
