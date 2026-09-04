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


def test_past_with_present_denial_is_suppressed():
    """§9.4 as refined in ``docs/risk-detection.md`` §9 (quality pass 2)."""
    hit = one("작년엔 죽고 싶었는데 지금은 아니에요")
    assert flags_on(hit) == {"past", "present_denial"}
    assert hit.severity == 2 and not hit.alerts
    hit = one("예전엔 희망이 없다고 느꼈는데 요즘은 안 그래요")
    assert hit.scope.past and hit.scope.present_denial and not hit.alerts


@pytest.mark.parametrize(
    ("text", "severity"),
    [
        ("작년부터 죽고 싶었어요", 1),
        ("예전에 손목을 그은 적이 있어요", 2),
        ("어릴 때부터 자해를 했어요", 1),
    ],
)
def test_past_without_present_denial_alerts_one_severity_lower(text: str, severity: int):
    hit = one(text)
    assert hit.scope.past and not hit.scope.present_denial
    assert hit.severity == severity and hit.alerts


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


# --------------------------------------------------------------- quality pass 2: stem families


@pytest.mark.parametrize(
    ("family", "text", "category"),
    [
        # 방언 어미·표기 변형 — 어간은 접두이므로 어미(-데이/-예/-당께/-유)는 그대로 붙어 매칭된다
        ("dialect", "죽고 싶데이", "suicidal_ideation"),
        ("dialect", "살기 싫어예", "suicidal_ideation"),
        ("dialect", "죽고 싶당께요", "suicidal_ideation"),
        ("dialect", "죽구 싶어유", "suicidal_ideation"),
        ("dialect", "죽어불고 싶어라", "suicidal_ideation"),
        ("spelling", "죽고싶어요", "suicidal_ideation"),
        ("spelling", "사라지구 싶어요", "suicidal_ideation"),
        ("spelling", "뒤지고 싶어요", "suicidal_ideation"),
        # 간접 사고
        ("indirect", "살아서 뭐하나 싶어요", "suicidal_ideation"),
        ("indirect", "아침에 눈 안 떠졌으면 좋겠어요", "suicidal_ideation"),
        ("indirect", "잠들어서 안 깨고 싶어요", "suicidal_ideation"),
        ("indirect", "세상 뜨고 싶어요", "suicidal_ideation"),
        ("indirect", "이제 다 놓고 싶어요", "suicidal_ideation"),
        ("indirect", "그냥 끝내고 싶어요", "suicidal_ideation"),
        ("indirect", "제가 없어졌으면 좋겠어요", "suicidal_ideation"),
        ("indirect", "요즘 나쁜 생각이 자꾸 들어요", "suicidal_ideation"),
        ("indirect", "극단적인 선택을 생각해요", "suicidal_ideation"),
        # 수단·계획
        ("means", "약 모아두고 있어요", "suicidal_ideation"),
        ("means", "옥상에 올라가 봤어요", "suicidal_ideation"),
        ("means", "한강 다리에 가봤어요", "suicidal_ideation"),
        ("means", "목 매려고 끈을 샀어요", "suicidal_ideation"),
        ("means", "번개탄을 사놨어요", "suicidal_ideation"),
        ("means", "수면제 한꺼번에 먹으려고요", "suicidal_ideation"),
        ("means", "손목 그어요", "self_harm"),
        # 자해
        ("self_harm", "팔을 그었어요", "self_harm"),
        ("self_harm", "상처 내고 나면 좀 나아요", "self_harm"),
        ("self_harm", "머리 박고 싶어요", "self_harm"),
        ("self_harm", "자해 충동이 심해요", "self_harm"),
        ("self_harm", "피 보고 싶어요", "self_harm"),
        ("self_harm", "허벅지를 꼬집어요", "self_harm"),
        ("self_harm", "담뱃불로 지졌어요", "self_harm"),
        # 타해
        ("harm_to_others", "다 죽여버리고 싶어요", "harm_to_others"),
        ("harm_to_others", "때려죽이고 싶어요", "harm_to_others"),
        ("harm_to_others", "해치고 싶은 마음이 들어요", "harm_to_others"),
        ("harm_to_others", "칼로 찌르는 상상을 해요", "harm_to_others"),
        ("harm_to_others", "불 지르고 싶어요", "harm_to_others"),
        ("harm_to_others", "복수하고 싶어요", "harm_to_others"),
        ("harm_to_others", "다 죽여버릴까 생각해요", "harm_to_others"),
        # 급성 물질 사용
        ("substance_acute", "술 마시고 약 먹었어요", "substance_acute"),
        ("substance_acute", "소주에 약을 타서 먹었어요", "substance_acute"),
        ("substance_acute", "필름 끊긴 적 많아요", "substance_acute"),
        ("substance_acute", "블랙아웃이 자주 와요", "substance_acute"),
        ("substance_acute", "며칠째 술만 마시고 있어요", "substance_acute"),
        ("substance_acute", "술 마시고 운전했어요", "substance_acute"),
        ("substance_acute", "약 과다복용한 적 있어요", "substance_acute"),
    ],
)
def test_new_stem_families_alert(family: str, text: str, category: str):
    hit = one(text)
    assert hit.category == category, family
    assert hit.alerts and not hit.suppressed, family


@pytest.mark.parametrize(
    "text",
    [
        "손목시계를 새로 샀어요",
        "손목이 아파서 병원 갔어요",
        "손목 터널 증후군이래요",
        "칼로 사과를 깎다가 베었어요",
        "노트에 선을 그었어요",
        "밑줄 그어 놨어요",
        "가방을 뒤지고 싶었는데 참았어요",
        "정확히 꼬집어 말하기 어려워요",
        "성적에 목매고 싶지 않아요",
        "자살골을 넣었어요",
        "자살률 통계를 봤어요",
        "연탄구이 집에 갔어요",
        "죽을 맛이에요",
        "죽기 살기로 공부했어요",
        "죽고 못 사는 사이예요",
        "유튜브에서 자해 관련 영상을 봤어요",
    ],
)
def test_broadened_stems_do_not_alert_on_everyday_senses(text: str):
    hits = scan(text, "patient")
    assert not hits or not hits[0].alerts, hits


@pytest.mark.parametrize(
    ("spaced", "unspaced"),
    [("죽고 싶어요", "죽고싶어요"), ("손목 그어요", "손목그어요"), ("약 모아 뒀어요", "약모아뒀어요")],
)
def test_matching_ignores_whitespace(spaced: str, unspaced: str):
    a, b = one(spaced), one(unspaced)
    assert (a.phrase, a.category, a.severity) == (b.phrase, b.category, b.severity)
    assert b.alerts
