"""Drive the demo console (``/console``) end-to-end with Playwright and save screenshots.

    chartwire serve all --embedded --port 8000 &          # seeded demo (make demo)
    python scripts/console_screenshots.py [--base http://127.0.0.1:8000] [--out docs/images] [--speed 4]

Flow (spec §13.3): login as the demo clinician → create a session (가상환자-0002, script s01) → open the
viewer → start the JS recorder → risk banner → session end → SOAP draft → consent revoke → purge
receipt → "복호화 시도". Every browser console error / page error is collected and printed; the exit
code is 1 when any occurred. All data is synthetic. Requires ``playwright`` and a Chromium build
(``PLAYWRIGHT_BROWSERS_PATH`` or ``--chromium``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from playwright.sync_api import Page, sync_playwright

T = TypeVar("T")

SHOTS = ("01_recorder", "02_live_alert", "03_soap_draft", "04_purge_receipt", "05_ops")


def wait_until(probe: Callable[[], T | None], timeout_s: float) -> T:
    """Poll from the driver side (the console's CSP forbids ``unsafe-eval``, so no in-page predicates)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = probe()
        if value is not None:
            return value
        time.sleep(0.25)
    raise TimeoutError("condition not met in time")


def wait_text(page: Page, selector: str, needle: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = page.locator(selector).inner_text()
        if needle in text:
            return text
        page.wait_for_timeout(250)
    raise TimeoutError(f"{selector!r} never contained {needle!r} (last: {text[:120]!r})")


def run(base: str, out: Path, speed: float, duration_s: int, chromium: str | None, patient: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium, args=["--lang=ko-KR"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
        )
        page.goto(f"{base}/console", wait_until="load")
        page.wait_for_selector("#banner")

        # login (clinician@demo.clinic / demo1234! are the console defaults)
        page.click("#loginBtn")
        wait_text(page, "#who", "clinician", 10)

        # a fresh session for the run: patient by exact blind-index name, script s01
        page.click("details > summary")
        page.fill("#patientName", patient)
        page.click("#findPatient")
        wait_text(page, "#patientInfo", "consent=", 10)
        page.select_option("#scriptSel", "s01")
        page.click("#createSession")
        session_id = wait_until(lambda: page.input_value("#sessionSel") or None, 10)

        # viewer first, so the live tab shows partial → final as they happen
        page.click("button[data-tab='live']")
        page.click("#watchBtn")
        wait_text(page, "#liveLog", "welcome", 10)

        # recorder: speed slider + duration, then start
        page.click("button[data-tab='rec']")
        page.evaluate(
            "([s, d]) => { const r = document.getElementById('speed'); r.value = s; r.dispatchEvent(new Event('input'));"
            " document.getElementById('durationS').value = d; }",
            [speed, duration_s],
        )
        page.click("#startBtn")
        wait_text(page, "#recLog", "welcome", 10)
        page.wait_for_timeout(6_000)
        page.screenshot(path=str(out / f"{SHOTS[0]}.png"))

        # live chart: transcript lines and the risk banner (s01 carries one alert late in the script)
        page.click("button[data-tab='live']")
        page.wait_for_selector("#liveTranscript .seg", timeout=30_000)
        page.wait_for_selector("#risk.show", timeout=int((duration_s / speed + 30) * 1000))
        page.wait_for_timeout(500)
        page.screenshot(path=str(out / f"{SHOTS[1]}.png"))
        page.click("#riskAck")

        # wait for the recorder to end and the stt-worker to finish; the draft arrives as a
        # ``note.status`` toast (§6.3) — poll ``notes/latest`` from the SOAP tab until it exists
        wait_text(page, "#recLog", "종료: ended", duration_s / speed + 60)
        wait_text(page, "#liveState", "transcribed", 90)
        page.click("button[data-tab='soap']")
        deadline = time.monotonic() + 90
        while True:
            page.click("#loadNote")
            page.wait_for_timeout(1_500)
            if page.locator("#statements .stmt").count() > 0:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("note draft never appeared in the SOAP tab")
        page.click("#statements .stmt >> nth=0")
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / f"{SHOTS[2]}.png"))

        # side panel: revoke → the console tracks the purge job → receipt → verify-decrypt
        page.click("#loadConsents")
        wait_text(page, "#consentSummary", "v", 10)
        page.click("#revokeBtn")
        wait_until(lambda: page.locator("#verifyDecrypt").is_enabled() or None, 60)
        page.click("#verifyDecrypt")
        wait_text(page, "#verifyOut", "failed", 15)
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / f"{SHOTS[3]}.png"))

        page.click("button[data-tab='ops']")
        page.check("#opsPoll")
        wait_text(page, "#opsInfo", "samples", 10)
        page.wait_for_timeout(500)
        page.screenshot(path=str(out / f"{SHOTS[4]}.png"))
        browser.close()

    summary = {
        "session_id": session_id,
        "screenshots": [str(out / f"{name}.png") for name in SHOTS],
        "browser_errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=Path("docs/images"))
    parser.add_argument("--speed", type=float, default=4.0)
    parser.add_argument("--duration", type=int, default=152, help="recorder length in seconds (s01 ≈ 151 s)")
    parser.add_argument("--chromium", default=None, help="Chromium executable (default: Playwright's)")
    parser.add_argument("--patient", default="가상환자-0002")
    args = parser.parse_args()
    return run(args.base, args.out, args.speed, args.duration, args.chromium, args.patient)


if __name__ == "__main__":
    sys.exit(main())
