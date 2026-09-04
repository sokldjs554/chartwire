"""``chartwire simulate --script s01 --speed 4 [--session <id>] [--drop-at 30s]`` (spec §13.1).

One protocol-compliant recorder (:class:`~chartwire.loadtest.client.RecorderClient`) plus one viewer
(:class:`~chartwire.loadtest.client.ViewerClient`) against a running api, driving the seeded demo
session of ``--script`` through the real pipeline: ticket → hello → frames → ack → end → stt-worker
finals/alerts → viewer. The command logs in over REST when no ``--token`` is given (demo clinician by
default), picks the ``created`` session whose ``script_ref`` matches when ``--session`` is omitted, and
prints one JSON summary (counts and latencies only — never transcript text) when the recorder ends.

``--drop-at 30s`` aborts the socket without a close frame at that wall-clock offset (§6.4 rule 4 resume
path, the console's "네트워크 끊기"). Exit code 0 = ``bye{ended}`` with ``loss == 0``.

Mounted lazily by ``chartwire.cli`` (``LAZY_SUBAPPS["simulate"]``) — a separate module from the
scenario runner ``chartwire loadtest`` so the two command trees never collide (WP-A request).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import typer

from chartwire.core.config import get_settings
from chartwire.loadtest.client import (
    HttpTicketSource,
    RecorderClient,
    RecorderConfig,
    SessionStats,
    ViewerClient,
    ViewerConfig,
    WebsocketsTransport,
)
from chartwire.stt.scripts_io import ScriptError, load_script, script_path

app = typer.Typer(invoke_without_command=True, context_settings={"allow_interspersed_args": True})

DEFAULT_API = "http://127.0.0.1:8000"
DEMO_TENANT = "demo"
DEMO_EMAIL = "clinician@demo.clinic"
DEMO_PASSWORD = "demo1234!"
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$")


def parse_duration_s(value: str) -> float:
    """``'30s'`` → 30.0, ``'1500ms'`` → 1.5, ``'2m'`` → 120.0, bare number = seconds."""
    m = _DURATION_RE.match(value)
    if m is None:
        raise typer.BadParameter(f"기간 형식이 아닙니다: {value!r} (예: 30s, 1500ms, 2m)")
    amount, unit = float(m.group(1)), m.group(2) or "s"
    return amount * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]


def total_chunks(total_ms: int, chunk_ms: int) -> int:
    """Number of ``chunk_ms`` frames covering the script (§10.4: last chunk carries the tail)."""
    return max(1, math.ceil(total_ms / chunk_ms))


def ws_url(api_base: str, kind: str) -> str:
    scheme, sep, rest = api_base.rstrip("/").partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}{sep}{rest}/ws/v1/{kind}"


async def _login(client: Any, tenant: str, email: str, password: str) -> str:
    resp = await client.post(
        "/v1/auth/token", json={"tenant_slug": tenant, "email": email, "password": password}
    )
    if resp.status_code != 200:
        raise typer.Exit(_fail(f"로그인 실패 ({resp.status_code}): {resp.text[:200]}"))
    return str(resp.json()["access_token"])


async def _pick_session(client: Any, headers: dict[str, str], script: str) -> dict[str, Any]:
    resp = await client.get("/v1/sessions", params={"state": "created", "limit": 200}, headers=headers)
    resp.raise_for_status()
    for item in resp.json()["items"]:
        if item.get("script_ref") == script:
            return dict(item)
    raise typer.Exit(
        _fail(f"script_ref={script!r} 인 created 세션이 없습니다 (chartwire seed --demo 실행 여부 확인)")
    )


async def _get_session(client: Any, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    if resp.status_code != 200:
        raise typer.Exit(_fail(f"세션 조회 실패 ({resp.status_code}): {resp.text[:200]}"))
    return dict(resp.json())


def _fail(message: str) -> int:
    typer.echo(message, err=True)
    return 2


async def _drop_later(recorder: RecorderClient, at_s: float) -> None:
    await asyncio.sleep(at_s)
    typer.echo(f"[simulate] 네트워크 끊기 (close frame 없음) at {at_s:.1f}s", err=True)
    recorder.abort_connection()


async def run_simulation(
    *,
    api_base: str,
    token: str | None,
    tenant: str,
    email: str,
    password: str,
    script: str,
    session_id: str | None,
    speed: float,
    chunks: int | None,
    drop_at_s: float | None,
    scripts_dir: Path,
    watch: bool,
    seed: int,
) -> dict[str, Any]:
    import httpx

    settings = get_settings()
    async with httpx.AsyncClient(base_url=api_base, timeout=15.0) as http:
        token = token or await _login(http, tenant, email, password)
        headers = {"Authorization": f"Bearer {token}"}
        sess = (
            await _get_session(http, headers, session_id)
            if session_id
            else await _pick_session(http, headers, script)
        )
        if sess.get("script_ref") not in (None, script):
            typer.echo(
                f"[simulate] 경고: 세션 script_ref={sess.get('script_ref')!r} ≠ --script {script!r}; "
                "stt-worker 는 세션의 script_ref 로 전사합니다",
                err=True,
            )
        if sess.get("state") not in ("created", "recording", "paused"):
            raise typer.Exit(_fail(f"세션 상태 {sess.get('state')!r} 는 녹음을 받을 수 없습니다"))
        chunk_ms = settings.chunk_ms
        if chunks is None:
            try:
                loaded = load_script(script_path(scripts_dir, sess.get("script_ref") or script))
            except ScriptError as exc:
                raise typer.Exit(
                    _fail(f"스크립트를 읽을 수 없습니다: {exc} (--chunks 로 개수를 직접 지정)")
                ) from exc
            chunks = total_chunks(loaded.total_ms, chunk_ms)
        sid = str(sess["id"])
        tickets = HttpTicketSource(api_base, token, client=http)
        stats = SessionStats(sid)
        recorder = RecorderClient(
            RecorderConfig(
                session_id=sid,
                ingest_url=ws_url(api_base, "ingest"),
                total_chunks=chunks,
                chunk_ms=chunk_ms,
                speed=speed,
                seed=seed,
            ),
            connect=WebsocketsTransport.connect,
            tickets=tickets,
            stats=stats,
        )
        viewer = (
            ViewerClient(
                ViewerConfig(session_id=sid, watch_url=ws_url(api_base, "watch")),
                connect=WebsocketsTransport.connect,
                tickets=tickets,
                stats=stats,
            )
            if watch
            else None
        )
        typer.echo(
            f"[simulate] session={sid} script={sess.get('script_ref')} chunks={chunks} "
            f"chunk_ms={chunk_ms} speed={speed}x (~{chunks * chunk_ms / 1000 / speed:.0f}s)",
            err=True,
        )
        tasks: list[asyncio.Task[Any]] = [asyncio.create_task(recorder.run(), name="recorder")]
        if viewer is not None:
            tasks.append(asyncio.create_task(viewer.run(), name="viewer"))
        if drop_at_s is not None:
            tasks.append(asyncio.create_task(_drop_later(recorder, drop_at_s), name="drop"))
        await tasks[0]
        if viewer is not None:
            # the viewer stops itself on ``bye{ended}``; give the tail (finals after the last ack,
            # ``session.state``) a moment, then stop it
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(tasks[1], timeout=10.0)
            viewer.stop()
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        return stats.summary()


@app.callback()
def simulate(
    script: str = typer.Option("s01", "--script", help="스크립트 참조 (seed --demo 의 s01..s20)"),
    speed: float = typer.Option(4.0, "--speed", min=0.1, help="실시간 대비 배속 (200 ms 청크 간격 / speed)"),
    session_id: str | None = typer.Option(
        None, "--session", help="세션 id (생략 시 script_ref 가 맞는 created 세션)"
    ),
    api: str = typer.Option(DEFAULT_API, "--api", help="api 베이스 URL"),
    token: str | None = typer.Option(
        None, "--token", help="JWT (생략 시 --tenant/--email/--password 로 로그인)"
    ),
    tenant: str = typer.Option(DEMO_TENANT, "--tenant"),
    email: str = typer.Option(DEMO_EMAIL, "--email"),
    password: str = typer.Option(DEMO_PASSWORD, "--password"),
    chunks: int | None = typer.Option(
        None, "--chunks", min=1, help="청크 수 (기본: 스크립트 total_ms / chunk_ms)"
    ),
    drop_at: str | None = typer.Option(
        None, "--drop-at", help="이 시점에 소켓을 close frame 없이 끊는다 (예: 30s)"
    ),
    scripts_dir: Path | None = typer.Option(
        None, "--scripts-dir", help="스크립트 디렉터리 (기본: Settings.scripts_dir)"
    ),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="뷰어 소켓도 열어 final/alert 를 센다"),
    seed: int = typer.Option(0, "--seed", help="청크 페이로드 시드"),
) -> None:
    """녹음기 시뮬레이션: 실제 api 에 §6 프로토콜대로 청크를 보낸다. 모든 데이터는 합성(SYNTHETIC)입니다."""
    summary = asyncio.run(
        run_simulation(
            api_base=api,
            token=token,
            tenant=tenant,
            email=email,
            password=password,
            script=script,
            session_id=session_id,
            speed=speed,
            chunks=chunks,
            drop_at_s=None if drop_at is None else parse_duration_s(drop_at),
            scripts_dir=scripts_dir or get_settings().stt_scripts_path,
            watch=watch,
            seed=seed,
        )
    )
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    ok = summary.get("outcome") == "ended" and summary.get("loss") == 0
    raise typer.Exit(0 if ok else 1)
