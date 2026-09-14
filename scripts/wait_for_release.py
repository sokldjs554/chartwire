#!/usr/bin/env python3
"""Live deploy gate: wait until the public URL serves *this* commit, then verify what it serves.

    python scripts/wait_for_release.py --base https://chartwire.onrender.com --sha "$GITHUB_SHA" \\
        --timeout 1800 --out live-verification.json

Why the wait comes first: after a push to ``main`` the previous release keeps answering for several
minutes while Render builds the new image, and a free instance that fell asleep takes another minute
or two to wake. A smoke that starts right away verifies the *old* build and reports it as the new one.
So step 1 polls ``GET /v1/release`` until ``git_sha`` equals the commit that triggered the workflow;
everything after that is a statement about this build.

Checks (each is recorded in the JSON, pass or fail; the exit code is 0 only when all pass):

1. ``/v1/release`` — served ``git_sha`` == expected (the wait itself)
2. ``/readyz`` — 200 (PostgreSQL · Redis · workers up inside the container)
3. ``/`` — 302 to ``/console``
4. ``/console`` — 200, carries the SYNTHETIC notice and redesigned product-home landmarks
5. ``/console/media/00_intro.png`` — 200 ``image/png`` (the home shows its own screenshots)
6. ``POST /v1/auth/token`` with the demo clinician → a token; ``GET /v1/sessions?limit=1`` with it → 200
7. ``GET /v1/sessions`` without a token → 401 (the role gate is on)
8. ``GET /v1/purge-jobs/<random>`` with the clinician token → 403 (admin·auditor only, never 404 first)

Not done here on purpose: ``POST /v1/consultations`` — it would fill the real inbox on every push.
Stdlib only, so CI runs it without installing the package.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

DEMO_LOGIN = {"tenant_slug": "demo", "email": "clinician@demo.clinic", "password": "demo1234!"}
CONSOLE_LANDMARKS = ("SYNTHETIC", 'id="page-home"', "상담에 집중하세요.")
USER_AGENT = "chartwire-live-gate/1"


class Http:
    """Tiny urllib wrapper: never raises on an HTTP status, returns ``(status, headers, body)``."""

    def __init__(self, base: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        opener = (
            urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect)
        )
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def wait_for_sha(http: Http, expected: str, timeout_s: float, interval_s: float) -> tuple[bool, str, float]:
    """Poll ``/v1/release`` until it names ``expected``. Returns ``(matched, last_seen, waited_s)``."""
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last = "(no answer yet)"
    while True:
        try:
            status, _, body = http.request("GET", "/v1/release")
            if status == 200:
                last = str(json.loads(body).get("git_sha", "(missing)"))
                if last == expected:
                    return True, last, time.monotonic() - started
            else:
                last = f"(HTTP {status})"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"({type(exc).__name__}: {exc})"
        print(
            f"[wait] serving {last[:40]} · want {expected[:12]}… · {time.monotonic() - started:.0f}s",
            flush=True,
        )
        if time.monotonic() >= deadline:
            return False, last, time.monotonic() - started
        time.sleep(interval_s)


def run_checks(http: Http, expected_sha: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'ok' if ok else 'FAIL'}] {name} — {detail}", flush=True)

    status, _, body = http.request("GET", "/v1/release")
    served = json.loads(body).get("git_sha") if status == 200 else None
    add("release sha matches", served == expected_sha, f"{served} vs {expected_sha}")

    status, _, body = http.request("GET", "/readyz")
    add("readyz 200", status == 200, f"HTTP {status} {body[:80]!r}")

    status, headers, _ = http.request("GET", "/", follow_redirects=False)
    add(
        "root redirects to /console",
        status == 302 and headers.get("location", "").endswith("/console"),
        f"HTTP {status} → {headers.get('location')}",
    )

    status, headers, body = http.request("GET", "/console")
    html = body.decode("utf-8", errors="replace")
    missing = [m for m in CONSOLE_LANDMARKS if m not in html]
    add(
        "console renders the home",
        status == 200 and not missing,
        f"HTTP {status}, missing={missing}, {len(html)} bytes",
    )
    add(
        "console is self-contained",
        "cdn." not in html.lower() and "<script src=" not in html,
        "no CDN, no external script",
    )

    status, headers, body = http.request("GET", "/console/media/00_intro.png")
    add(
        "home screenshot served",
        status == 200 and headers.get("content-type", "").startswith("image/png") and len(body) > 10_000,
        f"HTTP {status} {headers.get('content-type')} {len(body)} bytes",
    )

    status, _, body = http.request("POST", "/v1/auth/token", body=DEMO_LOGIN)
    token = json.loads(body).get("access_token") if status == 200 else None
    add("demo clinician logs in", bool(token), f"HTTP {status}")

    if token:
        status, _, _ = http.request("GET", "/v1/sessions?limit=1", token=token)
        add("sessions readable with token", status == 200, f"HTTP {status}")
        status, _, body = http.request("GET", f"/v1/purge-jobs/{uuid.uuid4()}", token=token)
        add(
            "purge receipt is admin·auditor only (403, not 404)",
            status == 403,
            f"HTTP {status} {body[:60]!r}",
        )

    status, _, _ = http.request("GET", "/v1/sessions")
    add("anonymous is rejected (401)", status == 401, f"HTTP {status}")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 배포본이 이 커밋인지 기다렸다가 검증합니다")
    parser.add_argument("--base", required=True, help="공개 URL, 예: https://chartwire.onrender.com")
    parser.add_argument("--sha", required=True, help="워크플로를 발생시킨 커밋 (GITHUB_SHA)")
    parser.add_argument("--timeout", type=float, default=1800, help="sha 일치를 기다리는 최대 초 (기본 30분)")
    parser.add_argument("--interval", type=float, default=20, help="폴링 간격 초")
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=60,
        help="요청 하나의 타임아웃 초 (잠든 인스턴스가 깨는 시간)",
    )
    parser.add_argument("--out", default="live-verification.json", help="검증 결과 JSON")
    args = parser.parse_args(argv)

    http = Http(args.base, args.request_timeout)
    matched, last_seen, waited = wait_for_sha(http, args.sha, args.timeout, args.interval)
    checks = (
        run_checks(http, args.sha)
        if matched
        else [
            {
                "name": "release sha matches",
                "ok": False,
                "detail": f"timed out after {waited:.0f}s; last served {last_seen}",
            }
        ]
    )
    verified = all(c["ok"] for c in checks)
    result = {
        "base_url": args.base,
        "expected_sha": args.sha,
        "served_sha": last_seen,
        "waited_s": round(waited, 1),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": checks,
        "verified": verified,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(
        f"[gate] {'VERIFIED' if verified else 'FAILED'} — {sum(c['ok'] for c in checks)}/{len(checks)} checks, waited {waited:.0f}s → {args.out}"
    )
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
