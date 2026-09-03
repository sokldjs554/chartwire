"""``scripts/eval_anthropic.py`` is importable without a key and replays the fixtures offline."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def script(monkeypatch_module: None):
    spec = importlib.util.spec_from_file_location("eval_anthropic", ROOT / "scripts" / "eval_anthropic.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_anthropic"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.delenv("ANTHROPIC_API_KEY", raising=False)
    mp.delenv("CHARTWIRE_ANTHROPIC_MODEL", raising=False)
    yield
    mp.undo()


def test_live_mode_requires_key_and_model(script, tmp_path: Path):
    assert script.main(["--scripts-dir", str(tmp_path), "--out", str(tmp_path / "r.json")]) == 2
    assert not (tmp_path / "r.json").exists()


def test_recorded_mode_writes_a_report_with_header_and_counts_only(script, tmp_path: Path):
    out = tmp_path / "anthropic.json"
    assert script.main(["--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    for key in ("seed", "git_sha", "generated_at", "cpu", "ram_gb", "python", "pg_version"):
        assert key in report
    assert report["mode"] == "recorded" and report["summary"]["sessions"] >= 5
    assert report["summary"]["status"]["verified"] >= 1 and report["summary"]["abstain_reason"]["schema"] == 1
    dumped = out.read_text(encoding="utf-8")
    assert "입맛이 없어요" not in dumped and "조현병" not in dumped  # counts only, never transcript text


def test_script_reader_builds_segment_views(script):
    segs = script.segments_from_script(ROOT / "tests" / "fixtures" / "scripts" / "t01.json")
    assert [s.seq for s in segs] == list(range(1, len(segs) + 1))
    assert {s.speaker for s in segs} <= {"clinician", "patient"}
