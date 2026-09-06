"""Script generator: determinism, contract schema, sizes and gold distributions (§10.2–10.3)."""

from __future__ import annotations

import json
import random
import re
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


def test_demo_profile_covers_every_chief_complaint_in_order() -> None:
    demo = generate_set("demo", 20)
    names = [c.name for c in grammar.v.CHIEF_COMPLAINTS]
    assert [s.template for s in demo] == [names[i % 8] for i in range(20)]
    # the eval profile draws the template from the script RNG instead — every template still appears
    assert {s.template for s in generate_set("eval", 40)} == set(names)


@pytest.mark.parametrize("chief", grammar.v.CHIEF_COMPLAINTS, ids=lambda c: c.name)
def test_dialogue_follows_the_chief_complaint(chief: grammar.v.ChiefComplaint) -> None:
    """The greeting probes the complaint, its detail lines are spoken, brief phases get no follow-up."""
    for seed in range(5):
        utts = grammar.build_session(random.Random(seed), chief, None)
        texts = [u.text for u in utts]
        assert texts[0] == grammar.v.QUESTIONS["greeting"][1 if chief.revisit else 0]
        assert any(t in chief.probes for t in texts), chief.name
        rendered_detail = {grammar.render(t, random.Random(0), chief.drugs)[0] for t in chief.detail}
        detail_prefixes = tuple(t.text.split("{")[0] for t in chief.detail)
        assert sum(t.startswith(detail_prefixes) for t in texts) >= 2, (chief.name, rendered_detail)
        plan_prefixes = tuple(t.text.split("{")[0] for t in chief.plans)
        assert any(t.startswith(plan_prefixes) for t in texts), chief.name
        for key in chief.brief:
            answers = {t.text for t in grammar.v.BRIEF_ANSWERS[key]}
            idx = next(i for i, t in enumerate(texts) if t in answers)
            assert utts[idx + 1].speaker == "clinician" and utts[idx + 1].text not in grammar.v.FOLLOW_UPS
        if not chief.on_medication:
            assert not any(t.endswith("그대로 유지하겠습니다") or "올려보겠습니다" in t for t in texts)
            assert texts.count(grammar.v.QUESTIONS["medication"][0]) == 0
        for u in utts:
            for fact in u.facts:
                if fact["type"] in ("medication", "plan_medication") and chief.drugs:
                    assert fact["name"] in chief.drugs


def test_two_chief_complaints_read_differently() -> None:
    """Same seed, different complaint → the patient talks about *that* complaint (not just the opening)."""
    names = {c.name: c for c in grammar.v.CHIEF_COMPLAINTS}

    def patient_lines(name: str) -> list[str]:
        utts = grammar.build_session(random.Random(3), names[name], None)
        return [u.text for u in utts if u.speaker == "patient"]

    insomnia, alcohol = patient_lines("불면"), patient_lines("알코올")
    sleep, drink = re.compile(r"잠|자요|수면|깨서"), re.compile(r"술|소주|맥주")
    assert sum(bool(sleep.search(t)) for t in insomnia) >= 5
    assert sum(bool(drink.search(t)) for t in alcohol) >= 5
    assert sum(bool(drink.search(t)) for t in insomnia) <= 2  # one standard 음주 answer at most
    assert sum(bool(sleep.search(t)) for t in alcohol) <= 4  # a standard 수면 phase, no more


def test_plan_action_and_starting_dose() -> None:
    assert grammar.plan_action("리튬을 600mg으로 올려보겠습니다") == "increase"
    assert grammar.plan_action("리튬을 300mg으로 줄여보겠습니다") == "decrease"
    assert grammar.plan_action("리튬을 300mg부터 처방하겠습니다") == "start"
    assert grammar.plan_action("리튬은 그대로 유지하겠습니다") == "keep"
    start = next(t for c in grammar.v.CHIEF_COMPLAINTS for t in c.plans if "처방" in t.text)
    for seed in range(20):
        _text, facts = grammar.render(start, random.Random(seed), ("에스시탈로프람", "서트랄린"))
        assert facts[0]["action"] == "start" and facts[0]["name"] in ("에스시탈로프람", "서트랄린")
        assert facts[0]["dose"] == grammar.v.DRUGS[str(facts[0]["name"])][0] + "mg"


def test_render_fills_drug_slots_with_particles() -> None:
    rng = random.Random(3)
    template = next(t for t in grammar.v.PLANS if "{drug_eul}" in t.text)
    text, facts = grammar.render(template, rng)
    assert facts[0]["type"] == "plan_medication" and facts[0]["action"] == "increase"
    assert f"{facts[0]['name']}을 " in text or f"{facts[0]['name']}를 " in text
    assert text.endswith("mg으로 올려보겠습니다") and str(facts[0]["dose"]).endswith("mg")
