"""Scope rule primitives (sentence bounds, syllable windows, individual flags)."""

from __future__ import annotations

from chartwire.risk.scope import ScopeFlags, classify, sentence_bounds, syllable_window


def test_sentence_bounds_split_on_punctuation_and_soft_endings():
    text = "죽고 싶어요. 아무것도 못 하겠어요"
    s, e = sentence_bounds(text, 0, 4)
    assert text[s:e] == "죽고 싶어요. "
    s, e = sentence_bounds(text, 8, 12)
    assert text[s:e] == "아무것도 못 하겠어요"
    soft = "살기 싫어요 그래도 약은 먹어요"
    s, e = sentence_bounds(soft, 0, 4)
    assert soft[s:e] == "살기 싫어요 "


def test_syllable_window_counts_non_space_characters():
    text = "가 나 다 라 마 바 사 아 자 차 카 타 파 하"
    lo, hi = syllable_window(text, 14, 14, 3, before=True)
    assert text[lo:hi] == "마 바 사 "
    lo, hi = syllable_window(text, 0, 1, 2, before=False)
    assert text[lo:hi] == " 나 다"


def test_negation_outside_window_or_sentence_does_not_count():
    # negation 14 syllables later is outside the 12-syllable window
    text = "죽고 싶어요 요즘 밥도 잘 먹고 잠도 잘 자고 약도 안 빼먹어요"
    assert not classify(text, 0, 4, "patient").negated
    # negation in the next sentence does not count
    text = "죽고 싶어요. 약은 안 먹었어요"
    assert not classify(text, 0, 4, "patient").negated


def test_past_with_present_denial_consumes_the_negation():
    text = "작년엔 죽고 싶었는데 지금은 아니에요"
    f = classify(text, 4, 8, "patient")
    assert f.past and f.present_denial and not f.negated and f.suppressed
    # past marker without a present-tense denial: past, not suppressed (detector drops severity)
    text = "작년부터 죽고 싶었어요"
    f = classify(text, 5, 9, "patient")
    assert f.past and not f.present_denial and not f.negated and not f.suppressed


def test_third_person_requires_particle_within_eight_syllables():
    text = "친구가 자해를 한다고 해서 걱정돼요"
    assert classify(text, 4, 6, "patient").third_person
    far = "친구가 지난주에 우리 집에 와서 밤새 얘기하다가 자해를 했대요"
    assert not classify(far, far.index("자해"), far.index("자해") + 2, "patient").third_person
    # 'first person' between subject and hit cancels the rule
    text = "동생은 제가 자해를 할까 봐 걱정해요"
    assert not classify(text, 8, 10, "patient").third_person


def test_clinician_question_only_for_clinician_and_question_forms():
    text = "죽고 싶다는 생각이 드세요"
    assert classify(text, 0, 4, "clinician").clinician_question
    assert not classify(text, 0, 4, "patient").clinician_question
    assert classify("자해를 하신 적이 있습니까?", 0, 2, "clinician").clinician_question
    assert not classify("자해 흔적이 보입니다", 0, 2, "clinician").clinician_question


def test_idiom_overlap_and_context():
    text = "자살 예방 교육을 들었어요"
    assert classify(text, 0, 2, "patient").idiom
    text = "뉴스에서 자살 사건을 봤어요"
    assert classify(text, 5, 7, "patient").idiom
    # a context idiom in a *different* sentence does not neutralise the hit
    text = "뉴스에서 봤어요. 저도 자살 생각을 해요"
    assert not classify(text, text.index("자살"), text.index("자살") + 2, "patient").idiom


def test_scope_flags_suppression_rule():
    assert not ScopeFlags().suppressed
    assert not ScopeFlags(past=True).suppressed
    assert not ScopeFlags(present_denial=True).suppressed  # a denial needs the past frame
    assert ScopeFlags(past=True, present_denial=True).suppressed
    for name in ("negated", "hypothetical", "third_person", "clinician_question", "idiom"):
        assert ScopeFlags(**{name: True}).suppressed
