"""Ending transforms used by the extractive and paraphrase providers."""

from __future__ import annotations

import pytest

from chartwire.notes.korean import report_form, transform_ending


@pytest.mark.parametrize(
    ("utterance", "report", "plain", "formal"),
    [
        (
            "잠드는 데 두 시간쯤 걸려요",
            "잠드는 데 두 시간쯤 걸린다고 함",
            "잠드는 데 두 시간쯤 걸린다",
            "잠드는 데 두 시간쯤 걸립니다",
        ),
        (
            "새벽 4시에 깨서 다시 못 자요",
            "새벽 4시에 깨서 다시 못 잔다고 함",
            "새벽 4시에 깨서 다시 못 잔다",
            "새벽 4시에 깨서 다시 못 잡니다",
        ),
        ("입맛이 없어요.", "입맛이 없다고 함", "입맛이 없다", "입맛이 없습니다"),
        (
            "3주 동안 3kg 빠졌어요",
            "3주 동안 3kg 빠졌다고 함",
            "3주 동안 3kg 빠졌다",
            "3주 동안 3kg 빠졌습니다",
        ),
        (
            "기분이 계속 가라앉아요",
            "기분이 계속 가라앉는다고 함",
            "기분이 계속 가라앉는다",
            "기분이 계속 가라앉습니다",
        ),
        (
            "걱정이 멈추질 않아요",
            "걱정이 멈추질 않는다고 함",
            "걱정이 멈추질 않는다",
            "걱정이 멈추질 않습니다",
        ),
        (
            "회사에서 집중이 안 돼요",
            "회사에서 집중이 안 된다고 함",
            "회사에서 집중이 안 된다",
            "회사에서 집중이 안 됩니다",
        ),
        ("자꾸 폭식을 해요", "자꾸 폭식을 한다고 함", "자꾸 폭식을 한다", "자꾸 폭식을 합니다"),
        ("요즘 너무 피곤해요", "요즘 너무 피곤하다고 함", "요즘 너무 피곤하다", "요즘 너무 피곤합니다"),
        ("낮에 너무 졸려요", "낮에 너무 졸리다고 함", "낮에 너무 졸리다", "낮에 너무 졸립니다"),
        ("머리가 아파요", "머리가 아프다고 함", "머리가 아프다", "머리가 아픕니다"),
        ("잘 몰라요", "잘 모른다고 함", "잘 모른다", "잘 모릅니다"),
        ("회사원이에요", "회사원이라고 함", "회사원이다", "회사원입니다"),
        ("그건 아니에요", "그건 아니라고 함", "그건 아니다", "그건 아닙니다"),
        ("숨이 막혀요", "숨이 막힌다고 함", "숨이 막힌다", "숨이 막힙니다"),
    ],
)
def test_endings(utterance: str, report: str, plain: str, formal: str):
    assert report_form(utterance) == report
    assert transform_ending(utterance, "plain") == plain
    assert transform_ending(utterance, "formal") == formal


@pytest.mark.parametrize("utterance", ["네네", "힘드네요", "약을 먹고 있거든요", "그게요…", "음"])
def test_unknown_endings_fall_back_to_direct_quotation(utterance: str):
    assert transform_ending(utterance, "plain") is None
    assert report_form(utterance) == f"“{utterance.rstrip('…')}”라고 함"


def test_transform_never_touches_the_body_before_the_ending():
    body = "에스시탈로프람 10mg 먹고 나서 잠은 좀 나아졌"
    for form in ("report", "plain", "formal"):
        assert transform_ending(body + "어요", form).startswith(body)  # type: ignore[union-attr]
