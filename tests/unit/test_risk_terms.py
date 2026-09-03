"""``lexicon_tag`` produces stable, searchable keywords for the ``terms[]`` GIN index."""

from __future__ import annotations

from chartwire.risk.terms import DRUG_TERMS, RISK_TERMS, SYMPTOM_TERMS, lexicon_tag


def test_tags_symptoms_drugs_and_risk_terms():
    tags = lexicon_tag("에스시탈로프람 10mg 먹고 있는데 잠드는 데 두 시간쯤 걸리고 자해 생각은 없어요")
    assert tags == ["불면", "에스시탈로프람", "자해"]


def test_tags_are_sorted_unique_and_empty_for_plain_text():
    assert lexicon_tag("") == []
    assert lexicon_tag("네네 알겠습니다") == []
    tags = lexicon_tag("불면증 때문에 불면 얘기를 했고 소주도 맥주도 마셔요")
    assert tags == sorted(set(tags)) == ["불면", "불면증", "음주"]


def test_risk_terms_cover_every_category_and_reflect_mentions_not_alerts():
    assert set(RISK_TERMS) == {"suicidal_ideation", "self_harm", "harm_to_others", "substance_acute"}
    # negated mention still tags: the index answers "언급", the detector answers "경보"
    assert "자살사고" in lexicon_tag("죽고 싶다는 생각은 전혀 없어요")
    assert "급성음주" in lexicon_tag("어제 필름이 끊겼어요")


def test_term_tables_are_well_formed():
    canon = [t for t, _ in SYMPTOM_TERMS]
    assert len(canon) == len(set(canon))
    assert all(patterns for _, patterns in SYMPTOM_TERMS)
    assert len(DRUG_TERMS) == len(set(DRUG_TERMS)) >= 40
