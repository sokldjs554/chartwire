"""Korean risk-phrase lexicon (spec §9.4). Deterministic, no model (ADR-0003).

Three severity tiers × four categories. Phrases are *stems*: ``죽고 싶`` matches ``죽고 싶어요``,
``죽고 싶다는``, ``죽고 싶은 마음`` … Matching is plain substring search in :mod:`detector`; scope
rules (negation, hypothetical, past, third person, clinician question, idiom) live in
:mod:`scope`. This is a high-recall safety net for the alert path, not a clinical classifier —
see ``docs/risk-detection.md``.

Severity: 3 = plan / means / attempt, 2 = active ideation or self-harm act (or acute risk),
1 = hopelessness / passive wish. Alerts fire for unsuppressed hits with severity ≥ 1 (§9.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["suicidal_ideation", "self_harm", "harm_to_others", "substance_acute"]
CATEGORIES: tuple[Category, ...] = ("suicidal_ideation", "self_harm", "harm_to_others", "substance_acute")


@dataclass(frozen=True)
class Phrase:
    text: str
    category: Category
    severity: int


def _tier(severity: int, category: Category, *texts: str) -> tuple[Phrase, ...]:
    return tuple(Phrase(t, category, severity) for t in texts)


# --------------------------------------------------------------------------- severity 3
_SI_PLAN = _tier(
    3,
    "suicidal_ideation",
    # 수단 준비 (약물)
    "약을 모아",
    "약을 모으",
    "수면제를 모아",
    "약을 한꺼번에",
    "약을 다 먹",
    "약을 털어",
    "약을 삼켰",
    "약을 잔뜩 먹",
    "약을 한 움큼",
    "약을 몽땅",
    # 수단 (추락·목맴·가스·농약·교통)
    "뛰어내리",
    "뛰어내릴",
    "옥상에 올라",
    "옥상에서",
    "다리 위에서",
    "한강에",
    "투신",
    "목을 매",
    "목을 맬",
    "목매",
    "끈을 샀",
    "줄을 샀",
    "밧줄",
    "번개탄",
    "연탄",
    "가스를 틀",
    "농약",
    "제초제",
    "차에 뛰어들",
    "선로에",
    "철로에",
    "동맥",
    # 계획·유서·정리
    "죽을 계획",
    "죽을 방법",
    "죽는 방법",
    "죽는 법을 검색",
    "자살 방법",
    "자살 계획",
    "죽을 날",
    "날짜를 정했",
    "언제 죽을지",
    "유서",
    "유언을 남",
    "신변 정리",
    "신변을 정리",
    "물건을 정리하고",
    "마지막 인사",
    "정리를 해놨",
    "죽으려고",
    # 시도
    "자살 시도",
    "자살을 시도",
    "자살하려고",
    "죽으려다",
    "약을 먹고 응급실",
)
_SH_MEANS = _tier(
    3,
    "self_harm",
    "손목을 그",
    "손목을 긋",
    "손목을 그었",
    "칼로 그",
    "칼로 긋",
    "칼로",
    "칼을 들었",
    "칼을 들고",
    "커터칼",
    "면도날",
    "피가 날 때까지",
    "담뱃불로",
    "자해 도구",
    "깊게 그",
)
_HO_MEANS = _tier(
    3,
    "harm_to_others",
    "칼로 찌르",
    "칼을 준비",
    "죽여버릴 거",
    "죽일 거",
    "죽일 계획",
    "죽이려고",
    "흉기",
    "불을 지르",
    "해칠 계획",
    "때려죽",
    "없애버릴 거",
    "총을",
)
_SA_MEANS = _tier(
    3,
    "substance_acute",
    "술을 마시고 약을",
    "술이랑 약을",
    "술과 약을",
    "술 마시고 수면제",
    "약을 술로",
    "술에 약을",
)

# --------------------------------------------------------------------------- severity 2
_SI_ACTIVE = _tier(
    2,
    "suicidal_ideation",
    # 직접 표현 (표준어·붙여쓰기·방언)
    "죽고 싶",
    "죽고싶",
    "죽고 싶데이",
    "죽고파",
    "죽어버리고 싶",
    "죽어버렸으면",
    "죽었으면 좋겠",
    "죽었으면 싶",
    "죽을래",
    "죽고만 싶",
    "자살",
    # 사라짐·중단
    "사라지고 싶",
    "사라졌으면",
    "없어지고 싶",
    "없어졌으면",
    "살기 싫",
    "살기 싫어예",
    "살기가 싫",
    "그만 살",
    "안 살고 싶",
    "살고 싶지 않",
    "살고싶지않",
    "살고 싶은 마음이 없",
    "살고 싶지가 않",
    "세상을 떠나",
    "떠나고 싶",
    "다 놓고 싶",
    "끝내고 싶",
    "끝내버리고 싶",
    "이대로 끝내",
    "삶을 끝",
    # 죽음 선호·수동 소망
    "죽는 게 낫",
    "죽는 게 편",
    "죽는 게 나",
    "죽는 편이",
    "차라리 죽어",
    "차라리 죽는",
    "눈을 안 떴으면",
    "아침에 안 깼으면",
    "안 깨어났으면",
    "잠들면 안 깨",
    "눈 감으면 끝",
    "안 태어났으면",
    "태어나지 말았",
    "사고로 죽었으면",
    "차에 치였으면",
    "차에 치이고 싶",
    "병에 걸려 죽",
    # 사고(思考)
    "죽음을 생각",
    "죽는 생각",
    "죽을 생각",
    "자살 생각",
    "자살 충동",
    "죽고 싶은 충동",
    "죽고 싶은 마음",
    "죽음만 생각",
    "죽는 상상",
    "죽는 꿈",
)
_SH_ACT = _tier(
    2,
    "self_harm",
    "자해",
    "손목",
    "긋고 싶",
    "그어버리고 싶",
    "그어버렸",
    "몸에 상처",
    "상처를 내",
    "상처를 냈",
    "머리를 벽에",
    "벽에 머리",
    "머리를 박",
    "스스로를 때리",
    "나를 때리",
    "내 몸을 때리",
    "꼬집어서 피",
    "손톱으로 긁",
    "화상을 입히",
    "할퀴",
    "머리카락을 뽑",
    "피를 보고 싶",
    "피를 보면 마음이",
    "아프게 하고 싶",
    "몸을 해치",
    "스스로를 해치",
    "자해하고 싶",
    "자해를 했",
    "자해했",
)
_HO_ACT = _tier(
    2,
    "harm_to_others",
    "때리고 싶",
    "죽이고 싶",
    "죽여버리고 싶",
    "해치고 싶",
    "목을 조르고 싶",
    "찌르고 싶",
    "패버리고 싶",
    "패고 싶",
    "밀어버리고 싶",
    "다치게 하고 싶",
    "부숴버리고 싶",
    "없애고 싶",
    "해코지",
    "복수하고 싶",
    "보복하고 싶",
    "칼을 들고 싶",
    "폭발할 것 같",
    "누구든 때릴",
)
_SA_ACUTE = _tier(
    2,
    "substance_acute",
    "필름이 끊",
    "블랙아웃",
    "술 먹고 기억이 없",
    "술 마시고 기억이 없",
    "기억이 끊",
    "술 없이 못 자",
    "술 없이는 못",
    "손이 떨려서 술",
    "술을 안 마시면 손이 떨",
    "아침부터 술",
    "해장술",
    "술 마시고 넘어져",
    "술 마시고 쓰러",
    "술 마시고 응급실",
    "약을 두 배로",
    "약을 더 먹었",
    "약을 많이 먹었",
    "정량보다 많이",
    "몰래 약을 더",
    "술을 매일",
    "매일 술",
)

# --------------------------------------------------------------------------- severity 1
_SI_HOPELESS = _tier(
    1,
    "suicidal_ideation",
    # 이유·가치·희망
    "살 이유가 없",
    "살 이유를 모르",
    "살아야 할 이유",
    "살아야 하는 이유",
    "살 가치가 없",
    "살아갈 자신이 없",
    "살아갈 힘이 없",
    "살아갈 이유",
    "희망이 없",
    "희망이 안 보",
    "미래가 없",
    "미래가 안 보",
    "앞이 안 보",
    "앞이 캄캄",
    "출구가 없",
    "벗어날 수 없",
    "빠져나갈 수 없",
    "나아질 것 같지 않",
    "좋아질 것 같지 않",
    "나아질 리가 없",
    "다 끝났으면",
    "끝이었으면",
    "이대로 끝났으면",
    "그냥 끝났으면",
    "다 끝내고",
    "의미가 없",
    "의미를 모르",
    "아무 소용없",
    "소용이 없",
    "부질없",
    "쓸모없",
    "쓸모가 없",
    "가치가 없",
    # 짐·부담
    "짐이 되",
    "짐만 되",
    "짐인 것 같",
    "부담만 되",
    "민폐만",
    "폐만 끼치",
    "나만 없으면",
    "내가 없어야",
    "내가 없어지면",
    "제가 없어지면",
    "없는 게 나",
    "없는 게 낫",
    "있으나 마나",
    "나 같은 건",
    "저 같은 건",
    "없어져도 아무도",
    "사라져도 아무도",
    "죽어도 아무도",
    "없어져도 모를",
    # 소진·포기
    "다 포기하고 싶",
    "포기하고 싶",
    "다 그만두고 싶",
    "더는 못 버티",
    "더 이상 못 버티",
    "못 버틸 것 같",
    "버틸 수가 없",
    "버티기 힘들",
    "견딜 수가 없",
    "견디기 힘들",
    "한계예요",
    "한계에 왔",
    "한계인 것 같",
    # 삶에 대한 태도
    "왜 사는지 모르",
    "왜 살아야 하는지",
    "뭐 하러 사는지",
    "사는 낙이 없",
    "사는 게 지옥",
    "사는 게 고통",
    "사는 게 무서",
    "사는 게 사는 게 아니",
    "살아도 사는 게 아니",
    "산송장",
    "죽은 것처럼 살",
    "다 잃었",
    "남은 게 없",
    "아무것도 남지 않",
    "아무도 필요로 하지 않",
    "아무도 신경 안",
    "혼자 남겨진",
)

PHRASES: tuple[Phrase, ...] = (
    _SI_PLAN + _SH_MEANS + _HO_MEANS + _SA_MEANS + _SI_ACTIVE + _SH_ACT + _HO_ACT + _SA_ACUTE + _SI_HOPELESS
)

# Idioms whose *span overlaps* a lexicon hit neutralise it (``자살 예방`` over ``자살``).
IDIOMS_OVERLAP: tuple[str, ...] = (
    "죽을 만큼",
    "죽겠다",
    "죽겠어",
    "죽겠네",
    "죽을 것 같이",
    "죽을 것 같아",
    "피곤해 죽",
    "힘들어 죽",
    "배고파 죽",
    "미치겠",
    "죽여주",
    "죽여줘",
    "죽여준다",
    "자살 예방",
    "자살예방",
    "자해 예방",
    "죽을 뻔",
    "죽는 줄 알",
    "죽도록",
    "죽어라",
    "죽인다 진짜",
    "죽을 힘을 다해",
    "죽을 둥 살 둥",
)

# Context idioms anywhere in the same sentence mark the whole sentence as quoted/educational.
IDIOMS_CONTEXT: tuple[str, ...] = (
    "드라마에서",
    "뉴스에서",
    "영화에서",
    "기사에서",
    "책에서",
    "소설에서",
    "노래 가사",
    "캠페인",
    "예방 교육",
    "교육을 받",
    "강의에서",
    "다큐멘터리",
    "웹툰에서",
)


def _check() -> None:
    seen: set[str] = set()
    for p in PHRASES:
        if not p.text or p.text != p.text.strip():
            raise ValueError(f"malformed lexicon phrase: {p.text!r}")
        if p.text in seen:
            raise ValueError(f"duplicate lexicon phrase: {p.text!r}")
        seen.add(p.text)
        if p.severity not in (1, 2, 3):
            raise ValueError(f"severity out of range for {p.text!r}")


_check()
