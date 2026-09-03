"""Text normalisation shared by the verifier and the mock providers (spec §9.3 rules 2 and 3).

All functions are pure and deterministic. ``normalize`` is the *only* relaxation the quote
matcher allows — there is no similarity fallback — so its definition is kept tiny and literal:
NFC, drop a fixed punctuation set, drop all whitespace.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PUNCTUATION_RE = re.compile(r"[.,!?~…'\"“”‘’()\[\]·]")
_WHITESPACE_RE = re.compile(r"\s+")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def normalize(text: str) -> str:
    """NFC → strip punctuation → remove all whitespace (rule 2, ``method='normalized'``)."""
    return _WHITESPACE_RE.sub("", PUNCTUATION_RE.sub("", nfc(text)))


@dataclass(frozen=True)
class NormalizedText:
    """``text`` normalised plus, for every normalised character, its offset in the NFC source."""

    text: str
    offsets: tuple[int, ...]

    def span(self, start: int, length: int) -> tuple[int, int]:
        """Map a match at ``start`` of ``length`` normalised chars back to NFC-source offsets."""
        if length == 0:
            return self.offsets[start] if start < len(self.offsets) else 0, 0
        return self.offsets[start], self.offsets[start + length - 1] + 1


def normalize_with_offsets(text: str) -> NormalizedText:
    source = nfc(text)
    chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(source):
        if ch.isspace() or PUNCTUATION_RE.match(ch):
            continue
        chars.append(ch)
        offsets.append(i)
    return NormalizedText("".join(chars), tuple(offsets))


# --------------------------------------------------------------------------- numbers (rule 3)

UNITS: tuple[str, ...] = (
    "개월",
    "시간",
    "mg",
    "㎎",
    "kg",
    "g",
    "정",
    "알",
    "주",
    "일",
    "달",
    "년",
    "분",
    "회",
    "번",
    "잔",
    "병",
    "%",
    "점",
)
_UNIT_ALT = "|".join(re.escape(u) for u in UNITS)  # longest first so ``mg``/``kg`` win over ``g``

_ONES = {
    "하나": 1,
    "한": 1,
    "둘": 2,
    "두": 2,
    "셋": 3,
    "세": 3,
    "넷": 4,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
}
_TENS = {"열": 10, "스물": 20, "서른": 30, "마흔": 40, "쉰": 50}
_ONES_ALT = "|".join(sorted(_ONES, key=len, reverse=True))
_TENS_ALT = "|".join(sorted(_TENS, key=len, reverse=True))
# ``열두`` → 12, ``스물한`` → 21, ``열`` → 10, ``두`` → 2. Tens are tried first so ``열한`` is 11,
# not ``열`` + ``한``.
_NUMBER_WORD_RE = re.compile(rf"(?:(?P<tens>{_TENS_ALT})(?P<ones>{_ONES_ALT})?|(?P<only>{_ONES_ALT}))")
_HALF_AFTER_UNIT_RE = re.compile(rf"(\d+)(\s*)({_UNIT_ALT})\s*반")
_HALF_BEFORE_UNIT_RE = re.compile(rf"반(?=\s*(?:{_UNIT_ALT}))")


def _number_word(m: re.Match[str]) -> str:
    if m.group("only") is not None:
        return str(_ONES[m.group("only")])
    value = _TENS[m.group("tens")]
    if m.group("ones") is not None:
        value += _ONES[m.group("ones")]
    return str(value)


def normalize_numbers_ko(text: str) -> str:
    """Korean number words → digits (rule 3): ``두 시간`` → ``2 시간``, ``열두 알`` → ``12 알``,
    ``한 시간 반`` → ``1.5 시간``, ``반 알`` → ``0.5 알``.

    The substitution is applied symmetrically to statement and quotes before tokenising, so
    incidental hits inside ordinary words (``한꺼번에`` → ``1꺼번에``) cancel out — only a
    ``digits + unit`` pair becomes a token.
    """
    out = _NUMBER_WORD_RE.sub(_number_word, text)
    out = _HALF_AFTER_UNIT_RE.sub(lambda m: f"{m.group(1)}.5{m.group(2)}{m.group(3)}", out)
    return _HALF_BEFORE_UNIT_RE.sub("0.5", out)


NUMERIC_UNIT_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_ALT})")


def numeric_tokens(text: str) -> set[str]:
    """Canonical ``<number><unit>`` tokens of ``text`` after :func:`normalize_numbers_ko`.

    ``10 mg``, ``10mg`` and ``10㎎`` all become ``10mg``; ``2.0주`` becomes ``2주``.
    """
    tokens: set[str] = set()
    for number, unit in NUMERIC_UNIT_RE.findall(normalize_numbers_ko(text)):
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        tokens.add(f"{number}{'mg' if unit == '㎎' else unit}")
    return tokens
