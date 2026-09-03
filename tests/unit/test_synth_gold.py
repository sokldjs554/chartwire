"""Session-level gold aggregation and the synth CLI."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from chartwire.synth import gold
from chartwire.synth.cli import app
from chartwire.synth.scripts import GoldPii, Script, generate_set


def _positive_script() -> Script:
    return next(s for s in generate_set("eval", 40) if "positive" in s.meta.risk_kinds)


def test_session_gold_record() -> None:
    script = _positive_script()
    record = gold.session_gold(script)
    assert record["script_ref"] == script.script_ref and record["n_utterances"] == len(script.utterances)
    assert record["expected_alerts"], "positive session must expect at least one alert"
    for idx in record["expected_alerts"]:
        risk = script.utterances[idx].gold.risk
        assert risk is not None and risk.kind == "positive" and risk.severity >= 1
    spans = {r["idx"]: r for r in record["risk"]}
    assert all(spans[idx]["alert"] for idx in record["expected_alerts"])
    assert not any(r["alert"] for r in record["risk"] if r["kind"] != "positive")
    assert all(isinstance(f["type"], str) for f in record["facts"])
    assert len({json.dumps(f, sort_keys=True) for f in record["facts"]}) == len(record["facts"])


def test_redaction_tokens() -> None:
    pii = [GoldPii(kind="phone", value="010-1234-5678"), GoldPii(kind="name", value="김뫼온")]
    assert (
        gold.redact("제 번호는 010-1234-5678예요. 김뫼온 선생님이요.", pii)
        == "제 번호는 [전화]예요. [이름] 선생님이요."
    )
    script = next(s for s in generate_set("eval", 40) if any(u.gold.pii for u in s.utterances))
    item = gold.pii_items(script)[0]
    assert item["token"] in gold.redact(
        script.utterances[item["idx"]].text, script.utterances[item["idx"]].gold.pii
    )


def _mounted() -> typer.Typer:
    root = typer.Typer()
    root.add_typer(app, name="synth")  # exactly how chartwire.cli mounts it
    return root


def test_cli_writes_demo_profile(tmp_path: Path) -> None:
    out = tmp_path / "scripts"
    result = CliRunner().invoke(
        _mounted(), ["synth", "scripts", "--profile", "demo", "--n", "3", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "3개 스크립트" in result.output
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert [row["script_ref"] for row in index["scripts"]] == ["s01", "s02", "s03"]
    assert all(row["seed"] == 1 for row in index["scripts"])
    assert (out / "gold.jsonl").read_text(encoding="utf-8").count("\n") == 3


def test_cli_rejects_unknown_profile(tmp_path: Path) -> None:
    result = CliRunner().invoke(_mounted(), ["synth", "scripts", "--profile", "prod", "--out", str(tmp_path)])
    assert result.exit_code != 0
