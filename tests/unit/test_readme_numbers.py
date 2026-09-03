"""README number pipeline (§11.4): marker filling, row deletion, --check staleness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartwire.eval import readme_cli

rn = readme_cli.load_script()

README = """# 제목
<!-- row:load.A.n200 -->| N=200 ack p95 | <!-- num:load.A.n200.ack_p95_ms -->—<!-- /num --> ms |
<!-- /row -->
<!-- row:load.A.n50 -->| N=50 ack p95 | <!-- num:load.A.n50.ack_p95_ms -->—<!-- /num --> ms |
<!-- /row -->
<!-- row:eval.risk_heldout -->| held-out recall | <!-- num:eval.risk_heldout.recall -->—<!-- /num --> |
<!-- /row -->
끝
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / "docs" / "loadtest").mkdir(parents=True)
    (tmp_path / "docs" / "eval").mkdir(parents=True)
    (tmp_path / "docs" / "loadtest" / "A.json").write_text(
        json.dumps({"runs": [{"n": 50, "ack_p95_ms": 41.26}, {"n": 200, "ack_p95_ms": None}]})
    )
    (tmp_path / "docs" / "eval" / "risk_heldout.json").write_text(json.dumps({"recall": 0.7135}))
    return tmp_path


def test_write_fills_markers_and_deletes_unmeasured_rows(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rn.main(["--write", "--root", str(repo)]) == 0
    text = (repo / "README.md").read_text(encoding="utf-8")
    assert "<!-- num:load.A.n50.ack_p95_ms -->41<!-- /num -->" in text
    assert "<!-- num:eval.risk_heldout.recall -->0.71<!-- /num -->" in text
    assert "load.A.n200" not in text  # null value → whole row deleted
    assert text.startswith("# 제목\n<!-- row:load.A.n50") and text.endswith("끝\n")
    assert "미측정 행 load.A.n200" in capsys.readouterr().out
    assert rn.main(["--check", "--root", str(repo)]) == 0  # idempotent


def test_check_detects_stale_value(repo: Path) -> None:
    rn.main(["--write", "--root", str(repo)])
    report = repo / "docs" / "eval" / "risk_heldout.json"
    report.write_text(json.dumps({"recall": 0.9}))
    assert rn.main(["--check", "--root", str(repo)]) == 1
    report.unlink()  # report gone → row must be deleted → README stale
    assert rn.main(["--check", "--root", str(repo)]) == 1


def test_unknown_key_and_unwrapped_missing_number_are_errors(repo: Path) -> None:
    readme = repo / "README.md"
    readme.write_text("<!-- num:load.A.n200.ack_p95_ms -->—<!-- /num -->\n", encoding="utf-8")
    assert rn.main(["--check", "--root", str(repo)]) == 2
    readme.write_text("<!-- num:nope.key -->—<!-- /num -->\n", encoding="utf-8")
    assert rn.main(["--write", "--root", str(repo)]) == 2


def test_path_resolution_and_formatting() -> None:
    doc = {
        "runs": [{"n": 50, "x": 1}, {"n": 200, "x": 2.5, "flag": True}],
        "queries": [{"id": "Q2b", "ms": 3}],
    }
    assert rn.resolve(doc, "$.runs[?n==200].x") == 2.5
    assert rn.resolve(doc, "$.runs[1].flag") is True
    assert rn.resolve(doc, "$.queries[?id==Q2b].ms") == 3
    with pytest.raises(rn.Missing):
        rn.resolve(doc, "$.runs[?n==300].x")
    with pytest.raises(ValueError, match="잘못된 경로"):
        rn.parse_path("$.runs[?n=200]")
    assert rn.format_value(2.456, ".1f") == "2.5" and rn.format_value(7, "") == "7"
    assert rn.format_value(0.5, "") == "0.50" and rn.format_value(True, "") == "true"


def test_registry_paths_parse_and_files_live_under_docs() -> None:
    for name, key in rn.KEYS.items():
        assert key.file.startswith("docs/") and key.file.endswith(".json"), name
        assert rn.parse_path(key.path), name
    assert rn.main(["--list"]) == 0


def test_typer_wrapper_requires_exactly_one_mode(repo: Path) -> None:
    import typer
    from typer.testing import CliRunner

    root = typer.Typer()
    root.add_typer(readme_cli.app, name="readme-numbers")  # exactly how chartwire.cli mounts it
    runner = CliRunner()
    assert runner.invoke(root, ["readme-numbers", "--root", str(repo)]).exit_code != 0
    assert runner.invoke(root, ["readme-numbers", "--write", "--root", str(repo)]).exit_code == 0
    assert runner.invoke(root, ["readme-numbers", "--check", "--root", str(repo)]).exit_code == 0
