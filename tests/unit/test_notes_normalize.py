"""§9.3 rule 2/3 helpers: normalisation, Korean number words, numeric tokens."""

from __future__ import annotations

import pytest

from chartwire.notes.normalize import normalize, normalize_numbers_ko, normalize_with_offsets, numeric_tokens


def test_normalize_strips_punctuation_and_whitespace_after_nfc():
    decomposed = "입맛이 없어요"  # NFD form of 입맛이 없어요
    assert normalize(decomposed) == "입맛이없어요"
    assert normalize("“잠을… 못, 자요!?” (아침에)") == "잠을못자요아침에"


def test_offsets_map_back_to_nfc_source():
    n = normalize_with_offsets("아, 잠을 못 자요.")
    idx = n.text.find("잠을못")
    start, end = n.span(idx, 3)
    assert "아, 잠을 못 자요."[start:end] == "잠을 못"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("두 시간", "2 시간"),
        ("열두 알", "12 알"),
        ("스물한 살", "21 살"),
        ("한 시간 반", "1.5 시간"),
        ("반 알", "0.5 알"),
        ("마흔 번", "40 번"),
        ("서른 개월", "30 개월"),
    ],
)
def test_normalize_numbers_ko(text: str, expected: str):
    assert normalize_numbers_ko(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("에스시탈로프람 10mg 먹고 있어요", {"10mg"}),
        ("10 ㎎ 으로 올리겠습니다", {"10mg"}),
        ("3주 동안 3kg 빠졌어요", {"3주", "3kg"}),
        ("하루 두 시간 반 자요", {"2.5시간"}),
        ("일주일에 두 번 소주 한 병", {"2번", "1병"}),
        ("0.5정씩 2.0주", {"0.5정", "2주"}),
        ("한꺼번에 먹었어요", set()),
        ("네네 그게요", set()),
    ],
)
def test_numeric_tokens(text: str, expected: set[str]):
    assert numeric_tokens(text) == expected
