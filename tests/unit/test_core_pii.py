"""``core/pii``: 결정적 리댁션이 합성 말뭉치의 골드와 정확히 같은 답을 내는가 (spec §10.2).

이 테스트가 존재하는 이유: `segment_search.text` 는 테넌트 전체를 훑는 **평문** 색인이라
전화번호·주소·이름이 그대로 들어가면 안 되는데, 규칙 기반 리댁터는 조용히 빗나가기 쉽다.
합성 대본은 발화의 5 %에 PII 를 심으면서 그 값과 토큰을 골드에 남기므로(`synth/gold.py`),
런타임 리댁터를 골드와 대조하면 "어느 발화를 놓쳤는지"까지 드러난다.
"""

from __future__ import annotations

import pytest

from chartwire.core.pii import find_names, has_identifier, redact
from chartwire.synth.gold import redact as gold_redact
from chartwire.synth.scripts import generate_set

DEMO_SCRIPTS = generate_set("demo", 20, 1)
PII_UTTERANCES = [(sc.script_ref, u) for sc in DEMO_SCRIPTS for u in sc.utterances if u.gold.pii]
CLEAN_UTTERANCES = [(sc.script_ref, u) for sc in DEMO_SCRIPTS for u in sc.utterances if not u.gold.pii]


def test_the_demo_corpus_actually_carries_pii() -> None:
    """빈 말뭉치를 대상으로 통과하는 시험이 되지 않게 한다 (PII_P = 5 %)."""
    assert len(PII_UTTERANCES) >= 40
    kinds = {item.kind for _, u in PII_UTTERANCES for item in u.gold.pii}
    assert kinds == {"name", "phone", "address"}


@pytest.mark.parametrize(("ref", "utt"), PII_UTTERANCES, ids=[f"{r}#{u.idx}" for r, u in PII_UTTERANCES])
def test_redaction_matches_the_gold_for_every_pii_utterance(ref: str, utt) -> None:
    assert redact(utt.text) == gold_redact(utt.text, utt.gold.pii)


@pytest.mark.parametrize(("ref", "utt"), CLEAN_UTTERANCES, ids=[f"{r}#{u.idx}" for r, u in CLEAN_UTTERANCES])
def test_no_false_positive_on_utterances_without_pii(ref: str, utt) -> None:
    """오탐은 진료 내용을 지운다 — 20개 대본 전체에서 한 건도 없어야 한다."""
    assert redact(utt.text) == utt.text


@pytest.mark.parametrize(
    "text",
    [
        "사장님이 그러세요",
        "이모님이 오셨어요",
        "부모님께서 걱정하세요",
        "선생님, 잠을 못 자요",
        "정신과 선생님이 그러셨어요",
        "주치의님이 약을 바꾸셨어요",
        "우리 선생님이 약을 바꿔 주셨어요",
        "에스시탈로프람 10mg 먹고 있어요",
        "하루에 2번 20mg 먹어요",
    ],
)
def test_titles_and_doses_are_not_names(text: str) -> None:
    assert find_names(text) == [] and has_identifier(text) is False and redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("백예봄님도 그렇게 말씀하셨죠.", "[이름]님도 그렇게 말씀하셨죠."),
        ("박온솔 선생님이 소개해 주셨어요.", "[이름] 선생님이 소개해 주셨어요."),
        ("남궁예봄님께서 말씀하셨어요", "[이름]님께서 말씀하셨어요"),  # 복성 4음절
        ("제 번호는 010-1234-5678예요.", "제 번호는 [전화]예요."),
        ("연락처는 01012345678이에요", "연락처는 [전화]이에요"),
        ("집은 가온시 라온구 새벽로 13번길 17예요", "집은 [주소]예요"),
        ("주소가 새벽시 담결구 누리로 8 맞으시죠?", "주소가 [주소] 맞으시죠?"),
    ],
)
def test_each_identifier_kind(text: str, expected: str) -> None:
    assert redact(text) == expected and has_identifier(text) is True
