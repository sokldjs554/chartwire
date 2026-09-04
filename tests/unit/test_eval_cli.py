"""``chartwire eval`` CLI: reports carry the header, aggregators skip loudly, --check thresholds."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from chartwire.eval import cli

runner = CliRunner()


def _load(out: Path, name: str) -> dict:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_eval_all_writes_every_computed_report_with_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # aggregator inputs (var/eval/*.json) do not exist here
    result = runner.invoke(
        cli.app, ["all", "--seed", "7", "--n", "20", "--per-class", "10", "--out", str(tmp_path / "eval")]
    )
    assert result.exit_code == 0, result.output
    out = tmp_path / "eval"
    names = {p.stem for p in out.glob("*.json")}
    assert names == {"risk_heldout", "risk_ingrammar", "grounding", "inject", "injection", "paraphrase"}
    for name in names:
        report = _load(out, name)
        assert report["seed"] == 7 and "git_sha" in report and "generated_at" in report and "cpu" in report
    assert _load(out, "risk_heldout")["frozen_sha256"] and _load(out, "risk_ingrammar")["n_scripts"] == 20
    assert _load(out, "inject")["n_per_class"] == 10 and _load(out, "injection")["n_sessions"] == 2
    assert "purge          건너뜀" in result.output and "rls            건너뜀" in result.output


def test_single_commands_and_aggregator_with_source(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    assert runner.invoke(cli.app, ["grounding", "--n", "10", "--out", str(out)]).exit_code == 0
    assert (out / "grounding.json").is_file()
    src = tmp_path / "purge.json"
    src.write_text(
        json.dumps({k: 0 for k in cli.purge_eval.REQUIRED} | {"receipts_verified_pct": 100.0}),
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["purge", "--out", str(out), "--source", str(src)])
    assert result.exit_code == 0 and _load(out, "purge")["receipts_verified_pct"] == 100.0
    result = runner.invoke(
        cli.app, ["protocol", "--out", str(out), "--no-run-tests", "--chaos-report", str(tmp_path / "x.json")]
    )
    assert result.exit_code == 0 and "건너뜀" in result.output and not (out / "protocol.json").exists()


def test_smoke_thresholds() -> None:
    ok = cli.smoke_failures(
        {"recall": 0.7, "precision": 0.8},
        {"injection_leaks": 0},
        {"false_rejection_rate": 0.02},
        {"residual_rows": 0, "residual_objects": 0, "residual_keys": 0},
        {"leaks": 0},
    )
    assert ok == []
    bad = cli.smoke_failures(
        {"recall": 0.5, "precision": 0.5},
        {"injection_leaks": 2},
        {"false_rejection_rate": 0.08},
        {"residual_rows": 1, "residual_objects": 0, "residual_keys": 0},
        {"leaks": 3},
    )
    assert len(bad) == 6 and "recall" in bad[0] and "precision" in bad[1] and "leaks" in bad[2]
    # the aggregate inputs are optional: absent purge/rls measurements are not a failure
    assert (
        cli.smoke_failures(
            {"recall": 0.9, "precision": 0.9}, {"injection_leaks": 0}, {"false_rejection_rate": 0.0}, None
        )
        == []
    )


def test_smoke_thresholds_track_the_measured_floor() -> None:
    """The held-out gate is the measured floor, not the §11.1 target — see SMOKE_THRESHOLDS."""
    assert cli.SMOKE_THRESHOLDS["heldout_recall_min"] <= 0.6
    assert cli.SMOKE_THRESHOLDS["injection_leaks_max"] == 0
    assert cli.SMOKE_THRESHOLDS["purge_residual_max"] == 0
    assert cli.SMOKE_THRESHOLDS["rls_leaks_max"] == 0
