"""Detector behaviour on every §10.2 risk kind, severity ordering and the one-hit rule."""

from __future__ import annotations

import pytest

from chartwire.risk.detector import DETECTOR_VERSION, RiskHit, scan
from chartwire.risk.scope import ScopeFlags


def one(text: str, speaker: str = "patient") -> RiskHit:
    hits = scan(text, speaker)
    assert len(hits) == 1, hits
    return hits[0]


def flags_on(hit: RiskHit) -> set[str]:
    return {k for k, v in vars(hit.scope).items() if v}


def test_detector_version_is_frozen_contract():
    assert DETECTOR_VERSION == "lex-1"


# --------------------------------------------------------------------------- positive


@pytest.mark.parametrize(
    ("text", "category", "severity"),
    [
        ("요즘은 그냥 사라지고 싶어요", "suicidal_ideation", 2),
        ("약을 모아두고 있어요", "suicidal_ideation", 3),
        ("살아야 할 이유를 모르겠어요", "suicidal_ideation", 1),
        ("손목을 긋고 싶은 충동이 들어요", "self_harm", 3),
        ("죽고 싶데이", "suicidal_ideation", 2),
        ("살기 싫어예", "suicidal_ideation", 2),
        ("필름이 끊겼어요", "substance_acute", 2),
        ("술을 마시고 약을 한꺼번에 먹었어요", "substance_acute", 3),
        ("남편을 죽이고 싶을 만큼 미워요", "harm_to_others", 2),
    ],
)
def test_positive_kind_alerts(text: str, category: str, severity: int):
    hit = one(text)
    assert (hit.category, hit.severity) == (category, severity)
    assert hit.alerts and not hit.suppressed
    assert flags_on(hit) == set()
    assert text[hit.start : hit.end] == hit.phrase


# --------------------------------------------------------------------------- suppressed kinds


@pytest.mark.parametrize(
    "text",
    [
        "죽고 싶다는 생각까지는 없어요",
        "자해 같은 건 전혀 안 해요",
        "죽고 싶은 마음은 없어요",
        "손목을 긋고 싶다는 생각은 한 번도 안 해봤어요",
    ],
)
def test_negated_kind_is_flagged_and_suppressed(text: str):
    hit = one(text)
    assert flags_on(hit) == {"negated"}
    assert not hit.alerts


def test_hypothetical_kind():
    assert scan("만약 그런 생각이 들면 어떻게 해야 하죠", "patient") == []  # no lexicon phrase at all
    hit = one("만약 죽고 싶어지면 어떻게 하죠")
    assert flags_on(hit) == {"hypothetical"} and not hit.alerts
    hit = one("죽고 싶다면 어떻게 해야 하나요")
    assert hit.scope.hypothetical and not hit.alerts


def test_past_kind_lowers_severity_and_keeps_alert():
    hit = one("작년엔 죽고 싶었는데 지금은 아니에요")
    assert flags_on(hit) == {"past"}
    assert hit.severity == 1 and hit.alerts
    hit = one("예전엔 희망이 없다고 느꼈는데 요즘은 안 그래요")
    assert hit.scope.past and hit.severity == 0 and not hit.alerts


def test_third_person_kind():
    hit = one("친구가 자해를 한다고 해서 걱정돼요")
    assert flags_on(hit) == {"third_person"} and not hit.alerts
    # a first-person subject between the relative and the hit keeps the hit on the speaker
    hit = one("엄마는 제가 죽고 싶다고 하면 우세요")
    assert not hit.scope.third_person and hit.alerts


def test_clinician_question_kind():
    for text in (
        "혹시 자해를 하신 적이 있으세요?",
        "혹시 죽고 싶다는 생각이 드세요?",
        "죽고 싶다는 생각이 드시나요",
    ):
        hit = one(text, "clinician")
        assert hit.scope.clinician_question and not hit.alerts
    # the same words from the patient are a positive
    assert one("혹시 죽고 싶다는 생각이 드세요?", "patient").alerts
    # a clinician statement (not a question) still alerts — the safety net does not trust roles
    assert one("환자분이 약을 모아두셨다고요", "clinician").alerts


@pytest.mark.parametrize(
    "text",
    [
        "피곤해 죽겠어요",
        "죽을 만큼 맛있는 걸 먹고 싶어요",
        "자살 예방 교육을 받았어요",
        "드라마에서 자살 장면을 봤어요",
    ],
)
def test_idiom_kind_never_alerts(text: str):
    hits = scan(text, "patient")
    assert all(h.scope.idiom and not h.alerts for h in hits)


# --------------------------------------------------------------------------- ordering / one hit


def test_highest_severity_wins_within_segment():
    hit = one("죽고 싶어서 약을 모아뒀어요")
    assert hit.phrase == "약을 모아" and hit.severity == 3


def test_alertable_hit_preferred_over_suppressed_higher_severity():
    hit = one("죽고 싶다는 생각은 없는데 사는 게 지옥 같아요")
    assert hit.alerts and hit.severity == 1 and hit.phrase == "사는 게 지옥"


def test_more_specific_phrase_at_same_position_wins():
    hit = one("자살 계획을 세웠어요")
    assert hit.phrase == "자살 계획" and hit.severity == 3


def test_at_most_one_hit_even_with_many_phrases():
    assert len(scan("죽고 싶고 자해도 하고 싶고 유서도 썼어요", "patient")) == 1


def test_empty_and_benign_text():
    assert scan("", "patient") == []
    for text in (
        "잠드는 데 두 시간쯤 걸려요",
        "갑자기 죽을 것 같은 공포가 와요",
        "기운이 하나도 없어요",
        "약을 며칠 빼먹었어요",
        "에스시탈로프람 10mg 먹고 있어요",
        "일주일에 3번 소주 1병 마셔요",
        "오늘 표정이 좀 어두워 보이시네요",
    ):
        assert scan(text, "patient") == [], text


def test_risk_hit_alert_property_matches_spec_rule():
    quiet = ScopeFlags()
    assert RiskHit("self_harm", 1, "자해", 0, 2, quiet).alerts
    assert not RiskHit("self_harm", 0, "자해", 0, 2, quiet).alerts
    assert not RiskHit("self_harm", 3, "자해", 0, 2, ScopeFlags(idiom=True)).alerts
