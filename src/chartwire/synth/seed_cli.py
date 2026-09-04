"""``chartwire seed --demo [--if-empty] [--seed 1]`` (mounted by ``chartwire.cli`` as ``seed``).

The options live on the group callback (``invoke_without_command=True``) so the command takes no
sub-command name — ``docker-compose.yml`` and ``scripts/dev_up.sh`` call it exactly as above.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from chartwire.core.config import get_settings
from chartwire.synth.seed import DEMO_PASSWORD, DEMO_USERS, seed_demo

app = typer.Typer(invoke_without_command=True, context_settings={"allow_interspersed_args": True})


@app.callback()
def seed(
    demo: bool = typer.Option(
        False, "--demo", help="데모 테넌트(demo) + 사용자 5명 + 환자 20명 + 스크립트 s01–s20"
    ),
    if_empty: bool = typer.Option(False, "--if-empty", help="demo 테넌트가 이미 있으면 아무것도 하지 않음"),
    seed_value: int = typer.Option(1, "--seed", help="스크립트·환자 식별자 시드 (demo 프로파일 기본 1)"),
    scripts_dir: Path | None = typer.Option(
        None,
        "--scripts-dir",
        help="스크립트 출력 디렉터리 (기본: $CHARTWIRE_STT_SCRIPTS_DIR 또는 var/scripts)",
    ),
) -> None:
    """합성 데모 데이터를 적재합니다 (멱등). 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음."""
    if not demo:
        raise typer.BadParameter("--demo 를 지정하세요 (현재 지원하는 유일한 시드 프로파일)")
    result = asyncio.run(
        seed_demo(get_settings(), seed=seed_value, if_empty=if_empty, scripts_dir=scripts_dir)
    )
    typer.echo(result.summary)
    if not result.skipped:
        typer.echo(
            f"로그인: tenant=demo, password={DEMO_PASSWORD}, users=" + ", ".join(u[1] for u in DEMO_USERS)
        )
