"""Scope rules for a lexicon hit (spec §9.4).

A hit is examined inside its *sentence*; proximity rules count **syllables** (non-space
characters), 12 around the hit unless stated otherwise. The flags are independent except that a
``past`` match consumes the negation belonging to its present-tense clause (``지금은 아니에요``),
so the same sentence is not also reported as ``negated``.

Suppression: ``negated``, ``hypothetical``, ``third_person``, ``clinician_question`` and ``idiom``
suppress the alert; ``past`` lowers severity by one and keeps the alert if the result is ≥ 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chartwire.risk.lexicon_ko import IDIOMS_CONTEXT, IDIOMS_OVERLAP

WINDOW_SYLLABLES = 12
SUBJECT_SYLLABLES = 8

_SENTENCE_BOUNDARY = re.compile(r"[.?!…]+\s*|(?<=[요다죠까네])\s+")
_NEGATION = re.compile(r"않|없(?!이)|아니|안 |못 ")  # 없이 ("without") is not a negation of the hit
_HYPOTHETICAL_BEFORE = re.compile(r"만약|라면|다면|가정|면 어떻")
_HYPOTHETICAL_SUFFIX = re.compile(r"^\S{0,3}(다면|라면)")
_PAST_MARKER = re.compile(
    r"예전|작년|재작년|그때|그 때|했었|었었|전에는|옛날|지난|어렸을|어릴 때|학생 때|한때"
)
_PRESENT_MARKER = re.compile(r"지금은|요즘은|이제는|이젠|현재는|지금엔|요즘엔")
_THIRD_SUBJECT = re.compile(
    r"(친구|동생|형|누나|언니|오빠|엄마|아빠|어머니|아버지|지인|동료|아는 사람|남편|아내|아들|딸|할머니|할아버지"
    r"|선배|후배|그 사람|그분|같은 반|직장 상사|룸메이트|사촌|삼촌|이모|고모)(가|이|는|은|도|께서)"
)
_FIRST_PERSON = re.compile(r"제가|내가|저는|나는|저도|나도|저 자신|제 자신|스스로")
_QUESTION_ENDING = re.compile(r"(세요|나요|십니까|습니까|까요|가요|던가요|죠)$")


@dataclass(frozen=True)
class ScopeFlags:
    negated: bool = False
    hypothetical: bool = False
    past: bool = False
    third_person: bool = False
    clinician_question: bool = False
    idiom: bool = False

    @property
    def suppressed(self) -> bool:
        return self.negated or self.hypothetical or self.third_person or self.clinician_question or self.idiom


def sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """``[s, e)`` of the sentence containing the span; trailing punctuation is kept inside."""
    s, e = 0, len(text)
    for m in _SENTENCE_BOUNDARY.finditer(text):
        if m.end() <= start:
            s = m.end()
        elif m.start() >= end:
            e = m.end()
            break
    return s, e


def syllable_window(text: str, start: int, end: int, n: int, *, before: bool) -> tuple[int, int]:
    """Bounds of the ``n`` non-space characters preceding ``start`` or following ``end``."""
    if before:
        i, count = start, 0
        while i > 0 and count < n:
            i -= 1
            if not text[i].isspace():
                count += 1
        return i, start
    i, count = end, 0
    while i < len(text) and count < n:
        if not text[i].isspace():
            count += 1
        i += 1
    return end, i


def classify(text: str, start: int, end: int, speaker: str) -> ScopeFlags:
    """Compute the scope flags for the lexicon hit ``text[start:end]`` spoken by ``speaker``."""
    s_start, s_end = sentence_bounds(text, start, end)
    sentence = text[s_start:s_end]
    b0, _ = syllable_window(text, start, end, WINDOW_SYLLABLES, before=True)
    _, a1 = syllable_window(text, start, end, WINDOW_SYLLABLES, before=False)
    before = text[max(b0, s_start) : start]
    after = text[end : min(a1, s_end)]
    after_sentence = text[end:s_end]

    past, negation_limit = _past(sentence, start - s_start, after_sentence)
    negation_region = after if negation_limit is None else after[:negation_limit]
    negated = _NEGATION.search(negation_region) is not None
    hypothetical = (
        _HYPOTHETICAL_BEFORE.search(before) is not None
        or _HYPOTHETICAL_SUFFIX.search(after) is not None
        or "면 어떻" in after_sentence
    )
    third_person = _third_person(text, start, s_start)
    clinician_question = speaker == "clinician" and _is_question(sentence)
    idiom = _idiom(text, start, end, s_start, s_end)
    return ScopeFlags(
        negated=negated,
        hypothetical=hypothetical,
        past=past,
        third_person=third_person,
        clinician_question=clinician_question,
        idiom=idiom,
    )


def _past(sentence: str, hit_offset: int, after_sentence: str) -> tuple[bool, int | None]:
    """Past ideation with a present-tense negation. Returns ``(past, negation_window_limit)``.

    When the pattern holds, negation is only searched *before* the present marker so the
    ``지금은 아니에요`` clause does not also count as negating the hit.
    """
    has_past = (
        _PAST_MARKER.search(sentence[:hit_offset]) is not None
        or _PAST_MARKER.search(after_sentence) is not None
    )
    present = _PRESENT_MARKER.search(after_sentence)
    if not has_past or present is None:
        return False, None
    if _NEGATION.search(after_sentence[present.end() :]) is None:
        return False, None
    return True, present.start()


def _third_person(text: str, start: int, s_start: int) -> bool:
    w0, _ = syllable_window(text, start, start, SUBJECT_SYLLABLES, before=True)
    lo = max(w0, s_start)
    # the subject may begin before the window as long as its particle ends inside it
    region_start = max(s_start, lo - 8)
    for m in _THIRD_SUBJECT.finditer(text, region_start, start):
        if m.end() < lo:
            continue
        if _FIRST_PERSON.search(text, m.end(), start) is None:
            return True
    return False


def _is_question(sentence: str) -> bool:
    stripped = sentence.rstrip()
    if stripped.endswith("?"):
        return True
    core = stripped.rstrip(".!…")
    return _QUESTION_ENDING.search(core) is not None


def _idiom(text: str, start: int, end: int, s_start: int, s_end: int) -> bool:
    sentence = text[s_start:s_end]
    for phrase in IDIOMS_OVERLAP:
        i = sentence.find(phrase)
        while i != -1:
            ps, pe = s_start + i, s_start + i + len(phrase)
            if ps < end and pe > start:
                return True
            i = sentence.find(phrase, i + 1)
    return any(ctx in sentence for ctx in IDIOMS_CONTEXT)
