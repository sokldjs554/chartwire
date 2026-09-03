"""Vocabulary (§10.1) completeness and the synthetic identity generators."""

from __future__ import annotations

import random
import re

import pytest

from chartwire.synth import vocab_ko as v


def test_chief_complaints_are_the_eight_templates() -> None:
    names = [c.name for c in v.CHIEF_COMPLAINTS]
    assert names == [
        "초진 우울",
        "재진 약물조정",
        "불안/공황",
        "불면",
        "성인 ADHD 추적",
        "적응/스트레스",
        "알코올",
        "강박",
    ]


def test_drug_table_matches_spec() -> None:
    assert len(v.DRUGS) == 16
    assert v.DRUGS["에스시탈로프람"] == ("5", "10", "15", "20")
    assert v.DRUGS["알프라졸람"] == ("0.25", "0.5")
    assert v.DRUGS["메틸페니데이트"] == ("18", "36")


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [("리튬", "을/를", "을"), ("메틸페니데이트", "을/를", "를"), ("리튬", "은/는", "은")],
)
def test_josa(word: str, pair: str, expected: str) -> None:
    assert v.josa(word, pair) == expected


def test_risk_case_set_covers_every_kind_with_consistent_severity() -> None:
    assert set(v.RISK_CASES) == set(v.RISK_KINDS)
    for kind, cases in v.RISK_CASES.items():
        assert cases, kind
        for case in cases:
            assert case.kind == kind
            assert (case.severity >= 1) == (kind == "positive")
            assert (case.category is None) == (kind == "idiom")
            assert (case.speaker == "clinician") == (kind == "clinician_question")


def test_slot_templates_declare_every_placeholder() -> None:
    pools = (
        v.SLEEP
        + v.APPETITE
        + v.MOOD
        + v.ANXIETY
        + v.CONCENTRATION
        + v.MEDICATION
        + v.ALCOHOL
        + v.PLANS
        + v.DURATION
    )
    for t in pools:
        for name in re.findall(r"{(\w+)}", t.text):
            assert name in t.slots or name in {"drug", "dose", "drug_eul", "drug_eun"}, (t.text, name)


def test_synthetic_identities() -> None:
    rng = random.Random(0)
    assert len(set(v.SURNAMES)) == 50
    assert len(set(v.GIVEN_SYLLABLES)) == len(v.GIVEN_SYLLABLES)
    assert v.synth_name(rng)[0] in v.SURNAMES and len(v.synth_name(rng)) == 3
    assert re.fullmatch(r"010-\d{4}-\d{4}", v.synth_phone(rng))
    assert re.fullmatch(r"\S+시 \S+구 \S+로 \d+번길 \d+", v.synth_address(rng))
    assert v.pseudonym(7) == "가상환자-0007"
