"""Lexicon shape: size, tiers, categories, spec-mandated phrases, no duplicates."""

from __future__ import annotations

from collections import Counter

from chartwire.risk import lexicon_ko as lex


def test_lexicon_size_and_uniqueness():
    assert len(lex.PHRASES) >= 220
    assert len({p.text for p in lex.PHRASES}) == len(lex.PHRASES)
    assert not set(lex.IDIOMS_OVERLAP) & {p.text for p in lex.PHRASES}


def test_three_tiers_and_four_categories_are_populated():
    by_sev = Counter(p.severity for p in lex.PHRASES)
    assert set(by_sev) == {1, 2, 3} and all(v >= 10 for v in by_sev.values())
    by_cat = Counter(p.category for p in lex.PHRASES)
    assert set(by_cat) == set(lex.CATEGORIES)
    assert {(p.category, p.severity) for p in lex.PHRASES} >= {(c, s) for c in lex.CATEGORIES for s in (2, 3)}


def test_spec_listed_phrases_have_spec_severity():
    sev = {p.text: p.severity for p in lex.PHRASES}
    for text in (
        "약을 모아",
        "약을 한꺼번에",
        "뛰어내리",
        "목을 매",
        "유서",
        "죽을 계획",
        "번개탄",
        "손목을 그",
        "칼로",
    ):
        assert sev[text] == 3, text
    for text in (
        "죽고 싶",
        "죽어버리고 싶",
        "사라지고 싶",
        "살기 싫",
        "그만 살",
        "없어지고 싶",
        "자살",
        "자해",
        "손목",
        "때리고 싶",
        "죽이고 싶",
    ):
        assert sev[text] == 2, text
    for text in ("살 이유가 없", "희망이 없", "다 끝났으면", "의미가 없", "짐이 되", "없는 게 나"):
        assert sev[text] == 1, text
    cat = {p.text: p.category for p in lex.PHRASES}
    assert cat["때리고 싶"] == cat["죽이고 싶"] == "harm_to_others"
    assert cat["술을 마시고 약을"] == cat["필름이 끊"] == "substance_acute"


def test_spec_listed_idioms_present():
    for idiom in ("죽을 만큼", "죽겠다", "죽을 것 같이", "피곤해 죽", "미치겠", "죽여주", "자살 예방"):
        assert idiom in lex.IDIOMS_OVERLAP
    for ctx in ("드라마에서", "뉴스에서", "영화에서", "기사에서", "캠페인", "예방 교육"):
        assert ctx in lex.IDIOMS_CONTEXT
