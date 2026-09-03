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
    """합성 데이터 생성: scripts (Phase 1: bulk)."""


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
