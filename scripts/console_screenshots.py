"""Drive the demo console (``/console``) end-to-end with Playwright and save screenshots.

    chartwire serve all --embedded --port 8000 &          # seeded demo (make demo)
    python scripts/console_screenshots.py [--base http://127.0.0.1:8000] [--out docs/images] [--speed 4]

Flow: the console auto-logs-in as the demo clinician → 새 상담 (creates 가상환자-NNNN, consent and a
session; ``--script auto`` picks the first demo script whose catalog entry expects an alert, and the
recorder length follows that script's ``total_ms``) → live transcript → risk banner → 초안·근거 →
데이터 관리 (consent revoke → purge → receipt → "복호화 시도") → 시스템 상세 (Ops).

The driver asserts the console did **not** fall back to preview mode: ``/console`` probes ``/v1/release``
at boot and only mocks the API when the backend does not answer, so a screenshot taken in preview would
show invented data. Role switching is the console's job, not the driver's — ``rbac.MATRIX`` lets a
clinician revoke a consent but only ``admin``/``auditor`` may read ``GET /v1/purge-jobs/{id}`` and call
``verify-decrypt``, and the console re-logs-in for those two calls. A clean run ends with **zero**
browser errors.

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
from playwright.sync_api import TimeoutError as PlaywrightTimeout

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
        page.goto(f"{base}/console", wait_until="domcontentloaded")
        page.wait_for_selector("#safetyBar")
        # 콘솔은 부팅 때 /v1/release 로 백엔드를 확인하고 데모 clinician 으로 스스로 로그인한다.
        # 그 두 가지가 끝나야 아래가 실제 API 를 탄다 — 미리보기로 떨어진 채로 찍으면 가짜 화면이 남는다.
        wait_text(page, "#connLabel", "API 연결됨", 60)
        wait_text(page, "#authChip", "clinician", 20)
        preview = page.evaluate("() => document.body.classList.contains('is-preview')")
        assert not preview, "console fell back to preview mode — the backend did not answer /v1/release"
        page.wait_for_timeout(600)
        shot(SHOTS[0])  # 홈 — 히어로 · 업무 흐름 · 검증된 범위

        # 새 상담: 대본을 고르고 실행하면 환자·동의·세션을 만들고 WebSocket 기록을 시작한다
        page.click('.nav button[data-page="session"]')
        page.select_option("#scriptSelect", script)
        page.click("#runRealtime")
        page.wait_for_selector("#transcript .utterance", timeout=60_000)
        page.wait_for_timeout(4_000)
        shot(SHOTS[1])  # 실시간 기록이 쌓이는 중

        # 위험 발화 경보 — 고른 대본은 최소 1건을 기대한다
        page.wait_for_selector("#riskBanner.show", timeout=int((duration_s / speed + 60) * 1000))
        page.wait_for_timeout(600)
        shot(SHOTS[2])
        page.click("#riskAck")

        # 기록이 끝나면 워커가 초안을 만든다. 조르지 않고 기다렸다가, 못 받았을 때만 한 번 요청한다.
        page.click("#stopRealtime")
        # 기록을 끝내면 stt-worker 가 전사를 마치고 초안을 만든다. 초안이 준비되면 콘솔이 ``note.status``
        # 를 받아 스스로 불러오므로 *기다린다*. 1.5 s 마다 조르면 아직 없는 초안을 부르게 되고, 그 404 가
        # 브라우저 콘솔 오류로 잡혀 "오류 0" 게이트를 간헐적으로 빨갛게 만든다. 클릭은 이벤트를 놓쳤을
        # 때의 대비책으로만 남긴다 — 그때쯤이면 초안은 확실히 있다.
        try:
            page.wait_for_selector("#notePane .note-box", timeout=180_000)
        except PlaywrightTimeout:
            page.click("#requestDraft")
            page.wait_for_timeout(3_000)
            page.click("#loadNote")
            page.wait_for_selector("#notePane .note-box", timeout=60_000)
        page.click("#notePane .note-box >> nth=0")  # 문장을 누르면 그 문장이 인용한 원문이 펼쳐진다
        page.wait_for_timeout(500)
        shot(SHOTS[3])

        # 데이터 관리: 동의 철회 → 파기 → 영수증 → 복호화 시도.
        # 영수증 조회와 verify-decrypt 는 admin·auditor 전용이라 콘솔이 스스로 역할을 바꿔 다시 로그인한다.
        page.click('.nav button[data-page="data"]')
        session_id = page.evaluate("() => state.sessionId") or ""
        page.click("#revokeConsent")
        wait_text(page, "#purgeStatus", "verified", 120)
        wait_until(lambda: page.locator("#verifyDecrypt").is_enabled() or None, 30)
        page.click("#verifyDecrypt")
        wait_text(page, "#decryptResult", "복호화 실패", 30)
        page.wait_for_timeout(400)
        # 영수증은 문서다 — 정적 캡처는 좁은 폭에서 전체를 담는다. 녹화 중에는 프레임이 1440×1000 으로
        # 고정이라 뷰포트를 줄이면 영상 끝에 회색 반쪽 화면이 남으므로 건너뛴다.
        if shots and video_dir is None:
            # 토스트는 몇 초 뒤 사라지는 순간적인 안내다 — 정적 캡처에서는 걷어낸다.
            page.evaluate("() => document.getElementById('toast').classList.remove('show')")
            page.set_viewport_size({"width": 900, "height": 1500})
            page.locator("#receiptBox").scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            shot(SHOTS[4], keep_scroll=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.evaluate("() => window.scrollTo(0, 0)")
        else:
            page.locator("#decryptResult").scroll_into_view_if_needed()
            page.wait_for_timeout(1_500)

        # 시스템 상세: /metrics 를 읽어 현재 운영 지표를 채운다
        job_id = page.input_value("#purgeJobInput") or ""

        page.click('.nav button[data-page="engineering"]')
        page.click("#refreshOps")
        page.wait_for_timeout(1_500)
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
