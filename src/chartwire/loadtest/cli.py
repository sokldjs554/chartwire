"""``chartwire loadtest A|B|C|D|H|results --sessions N --duration 60 --out docs/loadtest`` (spec §13.1).

A–D run :func:`chartwire.loadtest.runner.run`; ``H`` delegates to the outbox bench
(:func:`chartwire.outbox.bench.run_bench`); ``results`` re-renders ``results.md`` from the JSON files.
Mounted lazily by ``chartwire.cli`` (``LAZY_SUBAPPS["loadtest"]``) — a separate module from
``chartwire simulate`` (WP-A request).

Measurement discipline (§0, §11.2): run on an idle box, one scenario at a time; every number the README
shows comes from the JSON these commands write. Nothing here prints transcript text.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from chartwire.core.config import get_settings
from chartwire.core.logging import configure
from chartwire.loadtest.scenarios import SCENARIOS

app = typer.Typer(invoke_without_command=True, context_settings={"allow_interspersed_args": True})

CHOICES = (*SCENARIOS, "H", "results")


def _echo_json(data: dict[str, Any]) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def summary_of(scenario: str, report: dict[str, Any]) -> dict[str, Any]:
    """The headline fields of a report (what the README rows read), for the console."""
    if scenario == "A":
        runs = report.get("runs", [])
        return {
            "scenario": "A",
            "runs": [
                {
                    k: r.get(k)
                    for k in (
                        "n",
                        "ack_p50_ms",
                        "ack_p95_ms",
                        "ack_p99_ms",
                        "final_e2e_p95_ms",
                        "alert_e2e_p95_ms",
                        "chunks_per_s",
                        "credit_min",
                        "loss",
                        "dup",
                    )
                }
                for r in runs
            ],
        }
    keys = {
        "B": ("credit_zero_at_s", "pause_count", "stream_len_max", "api_rss_slope_mb_per_min", "loss"),
        "C": ("ack_p95_ms", "reference_ack_p95_ms", "ack_p95_delta_pct", "dropped_partials", "loss"),
        "D": ("resume_success_pct", "superseded_closes", "rebuild_count", "loss", "dup", "invariants"),
        "H": ("events", "workers", "tenants", "elapsed_s", "events_per_s", "dlq_count", "reclaimed"),
    }.get(scenario, ())
    return {"scenario": scenario, **{k: report.get(k) for k in keys}}


@app.callback()
def loadtest(
    scenario: str = typer.Argument(..., help="A | B | C | D | H | results"),
    sessions: int | None = typer.Option(None, "--sessions", min=1, help="세션 수 (기본: 시나리오 정의값)"),
    duration: int = typer.Option(60, "--duration", min=5, help="녹음 길이 (s); 청크 수 = duration × 5"),
    out: Path = typer.Option(Path("docs/loadtest"), "--out", help="리포트 디렉터리"),
    seed: int = typer.Option(42, "--seed"),
    api_port: int = typer.Option(8120, "--api-port"),
    worker_port: int = typer.Option(8121, "--worker-port"),
    stt_port: int = typer.Option(8122, "--stt-port"),
    cores_api: str = typer.Option("0-1", "--cores-api", help="taskset 코어 (api)"),
    cores_services: str = typer.Option("2-3", "--cores-services", help="taskset 코어 (worker, stt-worker)"),
    cores_client: str = typer.Option("3", "--cores-client", help="이 프로세스(부하 클라이언트)의 코어"),
    pin: bool = typer.Option(True, "--pin/--no-pin", help="taskset/affinity 고정"),
    ramp: float = typer.Option(2.0, "--ramp", help="세션 시작을 이 시간에 걸쳐 분산 (s)"),
    tail_wait: float = typer.Option(20.0, "--tail-wait", help="마지막 bye{ended} 뒤 stt/뷰어 꼬리 대기 (s)"),
    drain_wait: float = typer.Option(
        120.0,
        "--drain-wait",
        help="DB 대조 전에 stt 파이프라인이 밀린 것을 소화할 때까지 기다리는 상한 (s). "
        "§11.2 의 stt_offsets 불변식은 '최종' 불변식이므로 파이프라인이 따라잡은 뒤에 읽어야 한다",
    ),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="역할·DB 생성 + upgrade head (멱등)"),
    workdir: Path | None = typer.Option(None, "--workdir", help="오브젝트/스크립트/로그 디렉터리"),
    keep_workdir: bool = typer.Option(False, "--keep-workdir"),
    eval_dir: Path = typer.Option(Path("docs/eval"), "--eval-dir", help="A 가 alert_latency.json 을 쓰는 곳"),
    events: int = typer.Option(100_000, "--events", help="H: 사전 삽입 이벤트 수"),
    workers: int = typer.Option(2, "--workers", help="H: 워커 프로세스 수"),
    tenants: int = typer.Option(30, "--tenants", help="H: 테넌트 수"),
    kill_at: float = typer.Option(0.25, "--kill-at", help="H: 이 진행률에서 워커 하나 SIGKILL (0 = 안 함)"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """부하 시나리오 실행 → docs/loadtest/<scenario>.json + results.md. 모든 데이터는 합성(SYNTHETIC)입니다."""
    name = scenario.upper() if scenario.lower() != "results" else "results"
    if name not in CHOICES:
        typer.echo(f"알 수 없는 시나리오 {scenario!r}; 선택: {', '.join(CHOICES)}", err=True)
        raise typer.Exit(2)
    configure(log_level)
    settings = get_settings()
    if name == "results":
        from chartwire.loadtest.runner import write_results_md

        typer.echo(str(write_results_md(out)))
        return
    if name == "H":
        from chartwire.outbox.bench import run_bench

        report = asyncio.run(
            run_bench(
                settings,
                events=events,
                workers=workers,
                tenants=tenants,
                seed=seed,
                kill_at=kill_at or None,
                out=out / "H.json",
            )
        )
        from chartwire.loadtest.runner import write_results_md

        write_results_md(out)
        _echo_json(summary_of("H", report))
        raise typer.Exit(0 if report.get("dlq_count") == 0 else 1)

    from chartwire.loadtest.runner import RunConfig, run

    base = SCENARIOS[name]
    n = sessions or base.sessions
    cfg = RunConfig(
        scenario=replace(base, sessions=n, duration_s=duration),
        sessions=n,
        duration_s=duration,
        out_dir=out,
        seed=seed,
        api_port=api_port,
        worker_port=worker_port,
        stt_port=stt_port,
        cores_api=cores_api,
        cores_services=cores_services,
        cores_client=cores_client,
        pin=pin,
        ramp_s=ramp,
        tail_wait_s=tail_wait,
        drain_wait_s=drain_wait,
        migrate=migrate,
        workdir=workdir,
        keep_workdir=keep_workdir,
        eval_dir=eval_dir,
    )
    report = asyncio.run(run(settings, cfg))
    _echo_json(summary_of(name, report))
    loss = (
        report.get("loss")
        if name != "A"
        else next((r.get("loss") for r in report.get("runs", []) if r.get("n") == n), None)
    )
    raise typer.Exit(0 if loss == 0 else 1)
