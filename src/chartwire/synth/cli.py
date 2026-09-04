"""``chartwire synth`` sub-commands.

Mounted by ``chartwire.cli`` as ``app.add_typer(chartwire.synth.cli.app, name="synth")``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from chartwire.synth import gold as gold_mod
from chartwire.synth.scripts import PROFILE_SEED, Profile, Script, generate_set

app = typer.Typer(help="합성 데이터 생성 (모든 데이터는 합성입니다)", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """합성 데이터 생성: scripts (스크립트 세트), bulk (성능 연구용 대량 적재)."""


def dump_json(payload: Any) -> str:
    """Canonical JSON text: UTF-8 Korean kept readable, stable key order, trailing newline."""
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_scripts(scripts: list[Script], out: Path) -> list[dict[str, Any]]:
    """Write ``<ref>.json`` per script plus ``index.json`` and ``gold.jsonl``; return the index rows."""
    out.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    with (out / "gold.jsonl").open("w", encoding="utf-8") as gold_file:
        for script in scripts:
            (out / f"{script.script_ref}.json").write_text(dump_json(script.model_dump()), encoding="utf-8")
            record = gold_mod.session_gold(script)
            gold_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            index.append(
                {
                    "script_ref": script.script_ref,
                    "seed": script.seed,
                    "template": script.template,
                    "total_ms": script.total_ms,
                    "n_utterances": record["n_utterances"],
                    "risk_kinds": record["risk_kinds"],
                    "n_expected_alerts": len(record["expected_alerts"]),
                    "injection": script.meta.injection,
                }
            )
    (out / "index.json").write_text(
        dump_json({"generator": "chartwire.synth", "scripts": index}), encoding="utf-8"
    )
    return index


@app.command("scripts")
def scripts_cmd(
    out: Path = typer.Option(..., "--out", help="출력 디렉터리 (<ref>.json, index.json, gold.jsonl)"),
    n: int | None = typer.Option(None, "--n", min=1, help="스크립트 수 (기본: demo 20, eval 200)"),
    seed: int | None = typer.Option(None, "--seed", help="기본 시드 (기본: demo 1, eval 42)"),
    profile: str = typer.Option("eval", "--profile", help="eval | demo"),
) -> None:
    """합성 상담 스크립트를 생성합니다 (같은 시드 → 바이트 단위로 동일한 출력)."""
    if profile not in PROFILE_SEED:
        raise typer.BadParameter("--profile 은 eval 또는 demo 여야 합니다")
    prof: Profile = "demo" if profile == "demo" else "eval"
    count = n if n is not None else (20 if prof == "demo" else 200)
    scripts = generate_set(prof, count, seed)
    index = write_scripts(scripts, out)
    alert_sessions = sum(1 for row in index if row["n_expected_alerts"])
    typer.echo(
        f"{len(index)}개 스크립트 생성 → {out} (경보 예상 세션 {alert_sessions}개, 시드 {scripts[0].seed})"
    )


@app.command("bulk")
def bulk_cmd(
    seed: int = typer.Option(7, "--seed", help="데이터셋 시드 (§10.3: 7)"),
    segments: int = typer.Option(2_000_000, "--segments", min=800, help="세그먼트 수 (세션당 100)"),
    tenants: int = typer.Option(8, "--tenants", min=1, help="테넌트 수"),
    owner_url: str | None = typer.Option(None, "--owner-url", help="기본값: CHARTWIRE_DATABASE_OWNER_URL"),
    out: Path | None = typer.Option(None, "--out", help="적재 리포트 JSON (예: docs/perf/bulk.json)"),
    vacuum: bool = typer.Option(
        True, "--vacuum/--no-vacuum", help="적재 뒤 VACUUM ANALYZE (성능 연구 전 필수)"
    ),
) -> None:
    """성능 연구용 대량 합성 데이터 적재 (§4.6). owner 역할로 COPY 하되 테넌트마다 app.tenant_id 를 설정합니다."""
    import asyncio

    from chartwire.core.config import get_settings
    from chartwire.eval.report import build_report
    from chartwire.synth import bulk

    settings = get_settings()
    plan = bulk.BulkPlan.build(seed=seed, segments=segments, tenants=tenants)
    typer.echo(
        f"bulk seed={plan.seed}: tenants={plan.tenants} sessions={plan.sessions:,} segments={plan.segments:,} "
        f"patients={plan.patients:,} (모든 데이터는 합성입니다)"
    )
    try:
        report = asyncio.run(
            bulk.load(
                owner_url or settings.database_owner_url,
                plan,
                kek_master=settings.kek_master_bytes,
                log=typer.echo,
                vacuum=vacuum,
            )
        )
    except bulk.BulkAlreadyLoaded as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump_json(build_report(seed, report.as_dict())), encoding="utf-8")
        typer.echo(f"리포트 → {out}")
