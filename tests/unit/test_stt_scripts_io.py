"""Script JSON contract: fixtures load, contract violations are rejected at load time."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartwire.stt.scripts_io import Script, ScriptError, load_script, script_path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scripts"


@pytest.mark.parametrize("ref", ["t01", "t02", "t03"])
def test_fixtures_load_and_follow_the_contract(ref: str):
    script = load_script(script_path(FIXTURES, ref))
    assert script.script_ref == ref
    assert script.total_ms == script.utterances[-1].t_end_ms + 1000
    for prev, nxt in zip(script.utterances, script.utterances[1:], strict=False):
        assert prev.t_end_ms <= nxt.t_start_ms
    assert script.meta.generator and script.meta.version


def test_meta_passes_generator_extras_through():
    script = load_script(script_path(FIXTURES, "t02"))
    assert script.meta.model_extra == {"risk_kinds": [], "injection": False}
    assert script.utterances[0].gold.pii[0].kind == "name"


def _base() -> dict:
    return json.loads((FIXTURES / "t01.json").read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "x.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["utterances"].__setitem__(1, {**d["utterances"][1], "t_start_ms": 1000}),  # overlap
        lambda d: d["utterances"].__setitem__(2, {**d["utterances"][2], "idx": 7}),  # idx != position
        lambda d: d.__setitem__("total_ms", 100),  # shorter than the last utterance
        lambda d: d.__setitem__("bonus", 1),  # extra top-level key
        lambda d: d["utterances"][0]["gold"].__setitem__(
            "risk", {"category": "x", "severity": 2, "kind": "positive"}
        ),
        lambda d: d["utterances"].__setitem__(0, {**d["utterances"][0], "t_end_ms": 0}),  # end <= start
        lambda d: d.__setitem__("utterances", []),
        lambda d: d.__setitem__("script_ref", "../escape"),
    ],
)
def test_contract_violations_raise_script_error(tmp_path: Path, mutate):
    data = _base()
    mutate(data)
    with pytest.raises(ScriptError):
        load_script(_write(tmp_path, data))


def test_missing_file_and_bad_ref(tmp_path: Path):
    with pytest.raises(ScriptError):
        load_script(tmp_path / "nope.json")
    with pytest.raises(ScriptError):
        script_path(tmp_path, "../../etc/passwd")
    assert script_path(tmp_path, "s01") == tmp_path / "s01.json"


def test_model_is_usable_directly_for_generators():
    data = _base()
    script = Script.model_validate(data)
    assert script.utterances[3].gold.risk is not None
    assert script.utterances[3].gold.risk.kind == "positive"
