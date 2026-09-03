"""Script generator: determinism, contract schema, sizes and gold distributions (§10.2–10.3)."""

from __future__ import annotations

import json
import random
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest

from chartwire.synth import gold, grammar
from chartwire.synth.cli import write_scripts
from chartwire.synth.scripts import (
    CHUNK_MS,
    MS_PER_CHAR,
    PAUSE_MS,
    TAIL_MS,
    Script,
    generate_script,
    generate_set,
    script_refs,
)
from chartwire.synth.vocab_ko import RISK_KINDS

EVAL_N = 200


@pytest.fixture(scope="module")
def eval_set() -> list[Script]:
    return generate_set("eval", EVAL_N)


def test_same_seed_is_byte_identical(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_scripts(generate_set("demo", 5), a)
    write_scripts(generate_set("demo", 5), b)
    names = sorted(p.name for p in a.iterdir())
    assert names == ["gold.jsonl", "index.json", "s01.json", "s02.json", "s03.json", "s04.json", "s05.json"]
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_different_seed_or_ref_changes_output() -> None:
    assert generate_script("s01", 1) != generate_script("s01", 2)
    assert generate_script("s01", 1) != generate_script("s02", 1)
    assert generate_script("e0010", 42, inject=True) == generate_set("eval", 10)[9]


def test_written_files_validate_against_contract(tmp_path: Path) -> None:
    scripts = generate_set("eval", 12)
    index = write_scripts(scripts, tmp_path)
    for row in index:
        text = (tmp_path / f"{row['script_ref']}.json").read_text(encoding="utf-8")
        script = Script.model_validate_json(text)
        assert script.meta.generator == "chartwire.synth" and script.meta.version == "1"
        assert script.chunk_ms == CHUNK_MS
        assert script.script_ref == row["script_ref"] and script.seed == 42
    with pytest.raises(ValueError):
        Script.model_validate({**scripts[0].model_dump(), "extra": 1})
    gold_lines = (tmp_path / "gold.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["script_ref"] for line in gold_lines] == script_refs("eval", 12)
    assert [
        row["script_ref"] for row in json.loads((tmp_path / "index.json").read_text())["scripts"]
    ] == script_refs("eval", 12)


def test_profiles_name_and_seed_scripts() -> None:
    demo = generate_set("demo", 20)
    assert [s.script_ref for s in demo][:2] == ["s01", "s02"] and demo[-1].script_ref == "s20"
    assert {s.seed for s in demo} == {1}
    assert not any(s.meta.injection for s in demo)
    assert generate_set("eval", 3, seed=7)[0].seed == 7


def test_timeline_invariants(eval_set: list[Script]) -> None:
    for script in eval_set:
        utts = script.utterances
        assert 40 <= len(utts) <= 120
        assert [u.idx for u in utts] == list(range(len(utts)))
        assert utts[0].t_start_ms == 0
        for prev, cur in pairwise(utts):
            assert cur.t_start_ms >= prev.t_end_ms
        for u in utts:
            speech = MS_PER_CHAR * len(u.text)
            assert speech + PAUSE_MS[0] <= u.t_end_ms - u.t_start_ms <= speech + PAUSE_MS[1]
        assert script.total_ms == utts[-1].t_end_ms + TAIL_MS
        turns = sum(a.speaker != b.speaker for a, b in pairwise(utts))
        assert turns / (len(utts) - 1) > 0.5, script.script_ref


def test_risk_kind_distribution(eval_set: list[Script]) -> None:
    kinds = Counter(u.gold.risk.kind for s in eval_set for u in s.utterances if u.gold.risk)
    assert set(kinds) == set(RISK_KINDS)
    suppressed = sum(n for k, n in kinds.items() if k != "positive")
    assert 150 <= kinds["positive"] <= 300  # spec §10.3: ≈220
    assert 250 <= suppressed <= 450  # spec §10.3: ≈330
    alert_sessions = sum(bool(gold.expected_alerts(s)) for s in eval_set)
    assert 80 <= alert_sessions <= 120  # §11.1 alert_latency: 100 sessions with risk utterances
    suppressed_sessions = sum(any(k != "positive" for k in s.meta.risk_kinds) for s in eval_set)
    assert 0.15 * EVAL_N <= suppressed_sessions <= 0.35 * EVAL_N
    # every session asks the risk question exactly as the clinician template says
    for s in eval_set:
        assert any(
            u.gold.risk and u.gold.risk.kind == "clinician_question" and u.speaker == "clinician"
            for u in s.utterances
        )


def test_gold_labels_are_consistent(eval_set: list[Script]) -> None:
    for s in eval_set:
        for u in s.utterances:
            if u.speaker == "clinician":
                assert u.gold.section_label in ("O", "P", "none")
            else:
                assert u.gold.section_label in ("S", "none")
            for fact in u.gold.facts:
                assert fact.type
                if fact.type == "medication":
                    assert fact.model_dump()["name"] in u.text
            for item in u.gold.pii:
                assert item.value in u.text
            if u.gold.risk and u.gold.risk.kind == "positive":
                assert u.speaker == "patient" and u.gold.risk.severity >= 1


def test_pii_and_injection_rates(eval_set: list[Script]) -> None:
    n = sum(len(s.utterances) for s in eval_set)
    with_pii = sum(1 for s in eval_set for u in s.utterances if u.gold.pii)
    assert 0.03 <= with_pii / n <= 0.07
    injection = [s for s in eval_set if s.meta.injection]
    assert [s.script_ref for s in injection][:2] == ["e0010", "e0020"] and len(injection) == 20
    for s in injection:
        assert s.meta.injection_utterances
        for idx in s.meta.injection_utterances:
            u = s.utterances[idx]
            assert u.speaker == "patient" and u.gold.section_label == "none" and u.gold.risk is None


def test_render_fills_drug_slots_with_particles() -> None:
    rng = random.Random(3)
    template = next(t for t in grammar.v.PLANS if "{drug_eul}" in t.text)
    text, facts = grammar.render(template, rng)
    assert facts[0]["type"] == "plan_medication" and facts[0]["action"] == "increase"
    assert f"{facts[0]['name']}을 " in text or f"{facts[0]['name']}를 " in text
    assert text.endswith("mg으로 올려보겠습니다") and str(facts[0]["dose"]).endswith("mg")
