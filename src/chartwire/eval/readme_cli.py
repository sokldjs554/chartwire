"""``chartwire readme-numbers --write|--check`` — thin wrapper around ``scripts/readme_numbers.py``.

The registry (``KEYS``) deliberately lives in the stdlib-only script so CI can
run it without installing the package; this module locates that script
relative to the editable checkout.  Mounted by ``chartwire.cli`` as
``app.add_typer(chartwire.eval.readme_cli.app, name="readme-numbers")``; the
options live on the group callback so the command takes no sub-command.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import typer

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "readme_numbers.py"
app = typer.Typer(invoke_without_command=True)


def load_script(path: Path = SCRIPT) -> ModuleType:
    """Import ``scripts/readme_numbers.py`` as a module (it is not part of the package)."""
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} 가 없습니다 — 저장소 체크아웃(editable 설치)에서만 사용할 수 있습니다"
        )
    spec = importlib.util.spec_from_file_location("readme_numbers", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve postponed annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@app.callback()
def readme_numbers(
    write: bool = typer.Option(False, "--write", help="마커를 채우고 미측정 행을 삭제"),
    check: bool = typer.Option(False, "--check", help="README가 최신인지 검사"),
    readme: Path = typer.Option(Path("README.md"), "--readme"),
    root: Path = typer.Option(REPO_ROOT, "--root", help="JSON 리포트 기준 디렉터리"),
) -> None:
    """README 숫자 마커를 docs/{eval,loadtest,perf}/*.json 에서 채웁니다."""
    if write == check:
        raise typer.BadParameter("--write 또는 --check 중 하나를 지정하세요")
    argv = ["--write" if write else "--check", "--readme", str(readme), "--root", str(root)]
    code = load_script().main(argv)
    if code:
        sys.exit(code)
