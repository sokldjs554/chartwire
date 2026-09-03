"""``chartwire serve api|worker|stt-worker|all [--embedded] [--host] [--port]`` (§13.1, §12.2).

Mounted lazily by ``chartwire.cli`` (``LAZY_SUBAPPS["serve"]``); the Dockerfile runs
``chartwire serve ${CHARTWIRE_ROLE:-api}`` and render.yaml ``chartwire serve all --embedded``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Awaitable
from typing import Any

import typer

from chartwire.core.config import get_settings
from chartwire.core.logging import configure
from chartwire.worker import main as worker_main

ROLES = ("api", "worker", "stt-worker", "all")

app = typer.Typer(
    help="프로세스 실행: api · worker · stt-worker · all(--embedded)",
    # ``chartwire serve api --port 8000``: options may follow the role argument (click groups default to
    # treating anything after a positional as a sub-command name).
    context_settings={"allow_interspersed_args": True},
)


def _missing(module: str, wp: str) -> typer.Exit:
    typer.echo(f"{module} 모듈이 아직 없습니다 ({wp} 작업 패키지). 통합 후 다시 실행하세요.", err=True)
    return typer.Exit(2)


def serve_api(host: str, port: int) -> None:
    import uvicorn

    try:
        from chartwire.api.app import create_app
    except ModuleNotFoundError as exc:
        raise _missing("chartwire.api.app", "WP-E") from exc
    uvicorn.run(create_app(get_settings()), host=host, port=port, log_level="info")


def serve_stt_worker() -> int:
    try:
        module = importlib.import_module("chartwire.stt.worker")
    except ModuleNotFoundError as exc:
        raise _missing("chartwire.stt.worker", "WP-C") from exc
    result = module.main(get_settings())
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return int(result or 0)


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


@app.callback(invoke_without_command=True)
def serve(
    role: str = typer.Argument("api", help="api | worker | stt-worker | all"),
    embedded: bool = typer.Option(False, "--embedded", help="all: 한 프로세스에 api+worker+stt-worker"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    if role not in ROLES:
        typer.echo(f"알 수 없는 역할 {role!r}; 선택: {', '.join(ROLES)}", err=True)
        raise typer.Exit(2)
    configure(log_level)
    logging.getLogger(__name__).info("serve", extra={"role": role, "embedded": embedded})
    if role == "api":
        serve_api(host, port)
    elif role == "worker":
        raise typer.Exit(worker_main.main(get_settings()))
    elif role == "stt-worker":
        raise typer.Exit(serve_stt_worker())
    else:
        if not embedded:
            typer.echo(
                "`serve all` 은 --embedded 로만 동작합니다 (한 프로세스, 풀 5); 그렇게 실행합니다.", err=True
            )
        raise typer.Exit(asyncio.run(worker_main.run_all(get_settings(), host=host, port=port)))
