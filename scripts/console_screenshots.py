"""Drive the demo console (``/console``) end-to-end with Playwright and save screenshots.

    chartwire serve all --embedded --port 8000 &          # seeded demo (make demo)
    python scripts/console_screenshots.py [--base http://127.0.0.1:8000] [--out docs/images] [--speed 4]

Flow (spec §13.3): login as the demo clinician → create a session (가상환자-NNNN; ``--script auto`` picks the
first demo script whose catalog entry expects an alert, and the recorder length follows that script's
``total_ms``) → open the viewer → start the JS recorder → risk banner → session end → SOAP draft →
consent revoke → **re-login as the demo admin** → purge receipt → "복호화 시도" → Ops. The role switch is not cosmetic: ``rbac.MATRIX``
lets a clinician revoke a consent but only ``admin``/``auditor`` may read ``GET /v1/purge-jobs/{id}`` and
call ``verify-decrypt`` (a clinician gets 403), and the Ops panel needs ``admin`` for the DLQ list.

``--video-dir`` records the whole run as WebM (Playwright ``record_video_dir``); ``docs/images/demo.gif``
is produced from it with a **full** ffmpeg build (``imageio-ffmpeg``) — the Playwright-bundled binary
(``/opt/pw-browsers/ffmpeg-*/ffmpeg-linux``) has only the ``scale`` filter, no ``fps``/palette filters
and no gif muxer, so it fails with ``No such filter: 'fps'``. The exact palettegen/paletteuse command
is in ``docs/dev/e2e.md`` §5. Every browser console error /
page error is collected and printed; the exit code is 1 when any occurred. All data is synthetic.
Requires ``playwright`` and a Chromium build (``PLAYWRIGHT_BROWSERS_PATH`` or ``--chromium``).
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

SHOTS = ("00_intro", "01_recorder", "02_live_alert", "03_soap_draft", "04_purge_receipt", "05_ops")


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


def pick_script(page: Page, base: str, script: str, duration_s: int | None) -> tuple[str, int]:
    """Resolve ``auto`` against ``/console/scripts.json`` (the catalog ``seed --demo`` wrote).

    ``auto`` → the first script with an expected alert (the flow waits for the risk banner); a missing
    duration → that script's ``total_ms`` plus a 2 s tail, so the recorder covers the whole consultation.
    """
    catalog: dict[str, dict[str, int]] = {}
    try:
        res = page.request.get(f"{base}/console/scripts.json")
        if res.ok:
            catalog = {row["script_ref"]: row for row in res.json().get("scripts", [])}
    except Exception:
        catalog = {}  # the catalog is optional: fall back to s01 / 180 s
    if script == "auto":
        script = next(
            (ref for ref in sorted(catalog) if catalog[ref].get("n_expected_alerts", 0) >= 1), "s01"
        )
    if duration_s is None:
        duration_s = int(catalog[script]["total_ms"] / 1000) + 2 if script in catalog else 180
    return script, duration_s


def run(
    base: str,
    out: Path,
    speed: float,
    duration_s: int | None,
    chromium: str | None,
    patient: str,
    admin_email: str = "admin@demo.clinic",
    video_dir: Path | None = None,
    shots: bool = True,
    script: str = "auto",
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium, args=["--lang=ko-KR"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="ko-KR",
            record_video_dir=str(video_dir) if video_dir else None,
            record_video_size={"width": 1440, "height": 1000} if video_dir else None,
        )
        page = context.new_page()
        script, duration_s = pick_script(page, base, script, duration_s)

        def shot(name: str, *, keep_scroll: bool = False) -> None:
            if not shots:
                return
            if not keep_scroll:
                page.evaluate("window.scrollTo(0, 0)")  # a click may have scrolled a panel into view
            page.wait_for_timeout(150)
            page.screenshot(path=str(out / f"{name}.png"))

        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
        )
        page.goto(f"{base}/console", wait_until="load")
        page.wait_for_selector("#banner")
        page.wait_for_timeout(500)
        shot(SHOTS[0])  # 로그인 전 소개 화면 (5분 투어 · 계정 · 알아 둘 것)

        # login (clinician@demo.clinic / demo1234! are the console defaults)
        page.click("#loginBtn")
        wait_text(page, "#who", "clinician", 10)

        # a fresh session for the run: patient by exact blind-index name, the chosen script
        page.click("details > summary")
        page.fill("#patientName", patient)
        page.click("#findPatient")
        wait_text(page, "#patientInfo", "consent=", 10)
        page.select_option("#scriptSel", script)
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
        shot(SHOTS[1])

        # live chart: transcript lines and the risk banner (the chosen script expects at least one alert)
        page.click("button[data-tab='live']")
        page.wait_for_selector("#liveTranscript .seg", timeout=30_000)
        page.wait_for_selector("#risk.show", timeout=int((duration_s / speed + 30) * 1000))
        page.wait_for_timeout(500)
        shot(SHOTS[2])
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
        shot(SHOTS[3])

        # side panel: revoke as the clinician (rbac: clinician|staff|admin) …
        page.click("#loadConsents")
        wait_text(page, "#consentSummary", "v", 10)
        page.click("#revokeBtn")
        job_id = wait_until(lambda: page.input_value("#purgeJobId") or None, 20)

        # … then re-login as the admin: GET /v1/purge-jobs/{id} and verify-decrypt are {admin, auditor}
        page.fill("#email", admin_email)
        page.click("#loginBtn")
        wait_text(page, "#who", "admin", 10)
        page.click("#trackPurge")
        wait_until(lambda: page.locator("#verifyDecrypt").is_enabled() or None, 60)
        page.click("#verifyDecrypt")
        wait_text(page, "#verifyOut", "failed", 15)
        page.wait_for_timeout(400)
        if shots:
            # 토스트는 5 s 뒤 사라지는 순간적인 안내라 정적 캡처에서는 걷어내고, 영수증과 판정이 화면에 들어오게 스크롤한다
            page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
            # 영수증은 문서다: 960 px 아래의 1열 레이아웃에서 전체 폭으로 찍어야 표가 읽힌다 (사이드 패널 360 px 는 좁다).
            # 영상(--no-shots)에서는 뷰포트를 바꾸지 않는다 — 녹화 프레임은 1440×1000 으로 고정이라 검은 여백만 남는다.
            page.set_viewport_size(
                {"width": 900, "height": 1400}
            )  # 합계 · 검증 · 해시 · 판정까지 한 장에 (단계는 접혀 있다)
            page.locator("#verifyOut").scroll_into_view_if_needed()
            page.evaluate(
                "() => window.scrollTo(0, window.scrollY + document.getElementById('receipt').getBoundingClientRect().top - 44)"
            )  # 44 px = 고정 배너 높이 — 영수증 제목이 배너 밑에 숨지 않게
            page.wait_for_timeout(300)
            shot(SHOTS[4], keep_scroll=True)  # 영수증 위치를 그대로 찍는다
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.evaluate("() => window.scrollTo(0, 0)")
        else:
            page.locator("#verifyOut").scroll_into_view_if_needed()  # 영상에는 판정표가 보이게만 한다
            page.wait_for_timeout(1_500)

        page.click("button[data-tab='ops']")
        page.check("#opsPoll")
        wait_text(page, "#opsInfo", "samples", 10)
        page.wait_for_timeout(500)
        shot(SHOTS[5])
        page.wait_for_timeout(1_500)
        video = page.video
        video_path = video.path() if video else None
        context.close()  # the WebM is only flushed on context close
        browser.close()

    summary = {
        "script": script,
        "duration_s": duration_s,
        "session_id": session_id,
        "purge_job_id": job_id,
        "video": str(video_path) if video_path else None,
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
    parser.add_argument(
        "--script",
        default="auto",
        help="demo script_ref; auto = first script whose catalog entry expects an alert",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="recorder length in seconds (default: the script's total_ms + 2 s)",
    )
    parser.add_argument("--chromium", default=None, help="Chromium executable (default: Playwright's)")
    parser.add_argument("--patient", default="가상환자-0002")
    parser.add_argument("--admin", default="admin@demo.clinic", help="purge receipt / Ops 는 admin 권한")
    parser.add_argument("--video-dir", type=Path, default=None, help="Playwright record_video_dir")
    parser.add_argument("--no-shots", action="store_true", help="PNG 를 쓰지 않고 영상만 남긴다")
    args = parser.parse_args()
    return run(
        args.base,
        args.out,
        args.speed,
        args.duration,
        args.chromium,
        args.patient,
        admin_email=args.admin,
        video_dir=args.video_dir,
        shots=not args.no_shots,
        script=args.script,
    )


if __name__ == "__main__":
    sys.exit(main())
