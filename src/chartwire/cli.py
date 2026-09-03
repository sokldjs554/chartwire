"""``chartwire`` command line. Sub-apps owned by other work packages are mounted lazily so the
CLI works while the tree is partially built and never imports optional dependencies eagerly."""

from __future__ import annotations

import base64
import importlib
import os

import typer

from chartwire import __version__
from chartwire.db.cli import app as db_app

app = typer.Typer(help="chartwire — SOAPY-class 음성차팅 제품의 밑바닥 백엔드 층", no_args_is_help=True)
dev_app = typer.Typer(help="개발 편의 명령", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(dev_app, name="dev")

LAZY_SUBAPPS: dict[str, str] = {
    "synth": "chartwire.synth.cli",
    "seed": "chartwire.synth.seed_cli",
    "eval": "chartwire.eval.cli",
    "perf": "chartwire.perf.cli",
    "serve": "chartwire.worker.cli",
    "simulate": "chartwire.loadtest.simulate_cli",
    "loadtest": "chartwire.loadtest.cli",
    "token": "chartwire.auth.cli",
    "outbox": "chartwire.outbox.cli",
    "purge": "chartwire.purge.cli",
    "audit": "chartwire.audit.cli",
    "readme-numbers": "chartwire.eval.readme_cli",
}
"""command name → module exposing ``app = typer.Typer()``; missing modules are skipped.

Single-verb commands (``seed``, ``serve``, ``simulate``) are sub-apps whose ``@app.callback(invoke_without_command=True)``
carries the options, so ``chartwire seed --demo --if-empty`` works without a nested command name."""


def _mount_lazy() -> list[str]:
    mounted: list[str] = []
    for name, module_path in LAZY_SUBAPPS.items():
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name and module_path.startswith(exc.name):
                continue  # the work package is not built yet
            raise
        sub = getattr(module, "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=name)
            mounted.append(name)
    return mounted


@dev_app.command("keygen")
def dev_keygen() -> None:
    """CHARTWIRE_KEK_MASTER / CHARTWIRE_JWT_SECRET 용 무작위 비밀값을 출력합니다."""
    typer.echo(f"CHARTWIRE_KEK_MASTER={base64.b64encode(os.urandom(32)).decode()}")
    typer.echo(f"CHARTWIRE_JWT_SECRET={base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')}")


@app.callback(invoke_without_command=False)
def main(
    version: bool = typer.Option(False, "--version", help="버전 출력", is_eager=True),
) -> None:
    if version:
        typer.echo(f"chartwire {__version__}")
        raise typer.Exit()


MOUNTED = _mount_lazy()

if __name__ == "__main__":
    app()
