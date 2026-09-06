"""Deterministic ending transforms for Korean patient utterances (spec §9.2).

The extractive provider turns ``잠을 못 자요`` into the reported form ``잠을 못 잔다고 함`` and the
paraphrasing mock turns it into the plain ``잠을 못 잔다`` or formal ``잠을 못 잡니다`` form. Only the
sentence ending changes; everything before the final verb is kept byte-for-byte, which is what
keeps the verifier's numeric/entity/negation rules symmetric between statement and quote.

The conjugator is a small table plus Hangul jamo arithmetic — not a morphological analyser. An
utterance whose ending it cannot handle is rendered as a direct quotation (``“…”라고 함``), which is
always grammatical and always verifiable.
"""

from __future__ import annotations

import re
from typing import Literal

Form = Literal["report", "plain", "formal"]

_HANGUL_BASE = 0xAC00
_MEDIALS = 21
_FINALS = 28
_FINAL_NIEUN = 4
_FINAL_BIEUP = 17
_FINAL_SSANG_SIOT = 20
# contracted vowel (stem + 아/어) → stem vowel: ㅕ→ㅣ, ㅘ→ㅗ, ㅝ→ㅜ, ㅙ→ㅚ; ㅏ ㅓ ㅐ ㅔ absorb the ending.
_DECONTRACT = {6: 20, 9: 8, 14: 13, 10: 11, 0: 0, 4: 4, 1: 1, 5: 5}
# ㅡ-dropping and 하다 stems whose contracted syllable is not recoverable by vowel arithmetic.
_CONTRACTED_STEMS = {
    "해": "하",
    "아파": "아프",
    "바빠": "바쁘",
    "슬퍼": "슬프",
    "기뻐": "기쁘",
    "나빠": "나쁘",
    "예뻐": "예쁘",
    "써": "쓰",
    "커": "크",
    "몰라": "모르",
    "빨라": "빠르",
    "달라": "다르",
    "불러": "부르",
    "그래": "그렇",
    "어때": "어떻",
}
# stems (after decontraction) that take 다고/다 rather than ㄴ다고/는다고.
_ADJECTIVE_STEMS = frozenset(
    {"졸리", "아프", "슬프", "기쁘", "바쁘", "나쁘", "예쁘", "크", "다르", "그렇", "어떻"}
)
_ADJECTIVE_HA_NOUNS = frozenset(
    {
        "피곤",
        "우울",
        "불안",
        "답답",
        "무기력",
        "예민",
        "초조",
        "불편",
        "편",
        "편안",
        "행복",
        "미안",
        "이상",
        "심",
        "조용",
        "허전",
        "막막",
        "지루",
        "어색",
        "멍",
        "심심",
        "울적",
        "착잡",
        "불쾌",
        "무서",
    }
)
_ADJECTIVE_BATCHIM_STEMS = frozenset(
    {"없", "있", "싫", "좋", "많", "같", "적", "작", "낮", "높", "괜찮", "싶", "힘들", "멀", "길", "짧", "늦"}
)
# endings whose stem is not recoverable by the rules below (ㄹ-drop before 네, sentence-final
# particles, the promissive 게요); they fall back to the direct-quotation form.
_UNHANDLED_ENDINGS = ("네요", "거든요", "잖아요", "데요", "고요", "까요", "게요", "죠")
# 조사로 끝나는 명사구 + 요 (``4주 전부터요``, ``작년까지요``): 마지막 음절이 열린 음절이라 아래의 어간 규칙이
# 동사로 오해해 ``부턴다고 함`` 을 만든다. 용언이 아니므로 활용하지 않고 인용형 ``…라고 함`` 으로 보고한다.
# 단음절 조사(가·나·이·로·도·만)는 동사 어간의 끝음절과 겹치므로(``가요`` 는 ``간다고 함``) 판정하지 않는다.
_PARTICLE_ENDINGS = (
    "부터", "까지", "에서", "처럼", "마다", "한테", "에게", "보다", "대로", "으로", "이나", "이랑", "랑",
)  # fmt: skip
_NEGATIVE_COPULA = {"report": "아니라고 함", "plain": "아니다", "formal": "아닙니다"}
_TRAILING_RE = re.compile(r"[\s.,!?~…'\"“”‘’]+$")


def _syllable(ch: str) -> tuple[int, int, int] | None:
    code = ord(ch) - _HANGUL_BASE
    if not 0 <= code < 11172:
        return None
    return code // (_MEDIALS * _FINALS), (code // _FINALS) % _MEDIALS, code % _FINALS


def _compose(initial: int, medial: int, final: int) -> str:
    return chr(_HANGUL_BASE + (initial * _MEDIALS + medial) * _FINALS + final)


def _with_final(word: str, final: int) -> str:
    """Attach a final consonant to the (open) last syllable of ``word``."""
    parts = _syllable(word[-1])
    if parts is None or parts[2] != 0:
        return word
    return word[:-1] + _compose(parts[0], parts[1], final)


def _stem(body: str) -> tuple[str, bool] | None:
    """``body`` is the utterance without its final ``요``. Returns ``(stem, is_adjective)``."""
    if not body:
        return None
    if body[-1] in "어아" and len(body) >= 2 and (parts := _syllable(body[-2])) is not None and parts[2] != 0:
        stem = body[:-1]
        adjective = parts[2] == _FINAL_SSANG_SIOT or any(stem.endswith(s) for s in _ADJECTIVE_BATCHIM_STEMS)
        return stem, adjective
    for contracted, plain in _CONTRACTED_STEMS.items():
        if body.endswith(contracted):
            stem = body[: -len(contracted)] + plain
            adjective = plain in _ADJECTIVE_STEMS or (
                plain == "하" and any(stem[:-1].endswith(n) for n in _ADJECTIVE_HA_NOUNS)
            )
            return stem, adjective
    parts = _syllable(body[-1])
    if parts is None or parts[2] != 0 or parts[1] not in _DECONTRACT:
        return None
    stem = body[:-1] + _compose(parts[0], _DECONTRACT[parts[1]], 0)
    return stem, any(stem.endswith(s) for s in _ADJECTIVE_STEMS)


def _predicate(stem: str, adjective: bool, form: Form) -> str:
    open_syllable = (parts := _syllable(stem[-1])) is not None and parts[2] == 0
    if form == "formal":
        return _with_final(stem, _FINAL_BIEUP) + "니다" if open_syllable else stem + "습니다"
    ending = "다고 함" if form == "report" else "다"
    if adjective:
        return stem + ending
    if open_syllable:
        return _with_final(stem, _FINAL_NIEUN) + ending
    return stem + "는" + ending


def _noun_phrase(body: str, form: Form) -> str:
    """조사로 끝나는 명사구: ``X부터요`` → ``X부터라고 함`` / ``X부터다`` / ``X부터입니다``."""
    parts = _syllable(body[-1])
    closed = parts is not None and parts[2] != 0
    if form == "formal":
        return body + "입니다"
    if form == "plain":
        return body + ("이다" if closed else "다")
    return body + ("이라고 함" if closed else "라고 함")


def _copula(body: str, form: Form) -> str:
    """``X예요`` / ``X이에요`` → ``X라고 함`` / ``X다`` / ``X입니다``."""
    noun = body[:-1] if body.endswith("이") else body
    if form == "formal":
        return noun + "입니다"
    if form == "plain":
        return noun + ("이다" if body.endswith("이") else "다")
    return noun + ("이라고 함" if body.endswith("이") else "라고 함")


def transform_ending(utterance: str, form: Form) -> str | None:
    """Rewrite the ``요`` ending of ``utterance`` into ``form``; ``None`` when the ending is unknown."""
    text = _TRAILING_RE.sub("", utterance)
    if not text.endswith("요") or len(text) < 2 or text.endswith(_UNHANDLED_ENDINGS):
        return None
    body = text[:-1]
    if body.endswith("아니에"):
        return body[:-3] + _NEGATIVE_COPULA[form]
    if body.endswith(("예", "이에")):
        return _copula(body[:-1] if body.endswith("예") else body[:-2] + "이", form)
    if body.endswith(_PARTICLE_ENDINGS):
        return _noun_phrase(body, form)
    stem = _stem(body)
    if stem is None:
        return None
    return _predicate(stem[0], stem[1], form)


def report_form(utterance: str) -> str:
    """``…요`` → ``…다고 함``; otherwise the direct-quotation form ``“…”라고 함``."""
    return transform_ending(utterance, "report") or f"“{_TRAILING_RE.sub('', utterance)}”라고 함"
