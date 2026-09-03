"""``lexicon_tag(text) -> list[str]`` — canonical terms for the consent-gated ``terms[]`` index.

``segment_search.terms`` is queried with ``terms @> ARRAY[:query]`` through a GIN index
(§4.2, Q2d), so each tag must be a stable, human-typed keyword: symptom canonical forms,
generic drug names and the four risk categories in Korean. Tags reflect *mentions* — a negated
or quoted mention still tags (search is "지난 6개월 이 환자의 불면 언급"); the alert decision is the
detector's job, not the index's.
"""

from __future__ import annotations

from chartwire.risk.lexicon_ko import PHRASES, Category

SYMPTOM_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "불면",
        (
            "불면",
            "잠드는 데",
            "잠이 안 와",
            "잠이 안 들",
            "잠을 못",
            "못 자요",
            "못 잤",
            "깨서 다시",
            "새벽에 깨",
        ),
    ),
    ("불면증", ("불면증",)),
    ("과다수면", ("과다수면", "낮에 너무 졸", "하루 종일 자", "잠만 자")),
    ("식욕저하", ("식욕저하", "입맛이 없", "식욕이 없", "식욕 저하", "밥을 못 먹")),
    ("체중감소", ("체중감소", "kg 빠졌", "살이 빠졌", "체중이 줄", "살이 빠져")),
    ("폭식", ("폭식",)),
    ("우울", ("우울", "기분이 계속 가라앉", "가라앉아요", "눈물이 자꾸", "눈물이 나")),
    ("무쾌감", ("무쾌감", "재미가 없", "흥미가 없", "재미있던 게", "즐겁지 않")),
    ("무기력", ("무기력", "아무것도 하기 싫", "기운이 하나도 없", "기운이 없", "의욕이 없")),
    ("불안", ("불안", "걱정이 멈추질", "걱정이 많", "초조")),
    ("공황", ("공황", "가슴이 두근거리고 숨이 막", "숨이 막혀", "죽을 것 같은 공포", "발작")),
    ("집중저하", ("집중저하", "집중이 안", "집중력", "실수를 자꾸", "산만")),
    ("강박", ("강박", "확인을 반복", "손을 자꾸 씻", "자꾸 확인")),
    ("음주", ("음주", "소주", "맥주", "술을", "술 마", "술이", "술 때문")),
    ("부작용", ("부작용", "속이 울렁", "울렁거려", "손이 떨", "입이 마르")),
    ("복약누락", ("복약누락", "약을 며칠 빼먹", "약을 빼먹", "약을 안 먹", "약을 끊", "약을 안 챙겨")),
    ("수면제", ("수면제",)),
    ("스트레스", ("스트레스",)),
    ("피로", ("피로", "피곤", "지쳐")),
)

DRUG_TERMS: tuple[str, ...] = (
    "에스시탈로프람",
    "서트랄린",
    "플루옥세틴",
    "파록세틴",
    "플루복사민",
    "벤라팍신",
    "데스벤라팍신",
    "둘록세틴",
    "부프로피온",
    "미르타자핀",
    "트라조돈",
    "아고멜라틴",
    "보티옥세틴",
    "아미트립틸린",
    "노르트립틸린",
    "리튬",
    "발프로산",
    "라모트리진",
    "카바마제핀",
    "쿠에티아핀",
    "올란자핀",
    "리스페리돈",
    "아리피프라졸",
    "팔리페리돈",
    "루라시돈",
    "클로자핀",
    "할로페리돌",
    "알프라졸람",
    "로라제팜",
    "클로나제팜",
    "디아제팜",
    "부스피론",
    "졸피뎀",
    "조피클론",
    "멜라토닌",
    "프로프라놀롤",
    "메틸페니데이트",
    "아토목세틴",
    "클로니딘",
    "날트렉손",
    "아캄프로세이트",
    "디설피람",
    "프레가발린",
    "가바펜틴",
    "하이드록시진",
    "라멜테온",
    "수보렉산트",
)

RISK_TERMS: dict[Category, str] = {
    "suicidal_ideation": "자살사고",
    "self_harm": "자해",
    "harm_to_others": "타해사고",
    "substance_acute": "급성음주",
}


def lexicon_tag(text: str) -> list[str]:
    """Sorted, de-duplicated tags for one segment; empty list when nothing matches."""
    if not text:
        return []
    tags: set[str] = set()
    for term, patterns in SYMPTOM_TERMS:
        if any(p in text for p in patterns):
            tags.add(term)
    for drug in DRUG_TERMS:
        if drug in text:
            tags.add(drug)
    for phrase in PHRASES:
        if phrase.text in text:
            tags.add(RISK_TERMS[phrase.category])
    return sorted(tags)
