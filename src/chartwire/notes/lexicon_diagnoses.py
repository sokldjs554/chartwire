"""Diagnosis names that count as verdict language in a statement (spec §9.3 rule 7).

A statement may contain one of these only when the same token appears verbatim inside one of
its quotes — a patient saying ``우울증 진단을 받았어요`` is reportable; a provider concluding
``주요우울장애`` is not. Matching is longest-first substring, case-insensitive for Latin tokens.
"""

from __future__ import annotations

DIAGNOSES: frozenset[str] = frozenset(
    {
        "주요우울장애",
        "우울장애",
        "우울증",
        "기분부전",
        "지속성우울장애",
        "조현병",
        "조현정동장애",
        "정신병",
        "양극성장애",
        "양극성",
        "조증",
        "경조증",
        "공황장애",
        "범불안장애",
        "사회불안장애",
        "사회공포증",
        "광장공포증",
        "특정공포증",
        "강박장애",
        "외상후스트레스장애",
        "급성스트레스장애",
        "PTSD",
        "ADHD",
        "주의력결핍",
        "과잉행동장애",
        "경계성",
        "인격장애",
        "성격장애",
        "섭식장애",
        "거식증",
        "폭식증",
        "신경성식욕부진",
        "알코올사용장애",
        "알코올의존",
        "물질사용장애",
        "불면증",
        "수면장애",
        "적응장애",
        "치매",
        "경도인지장애",
        "자폐",
        "자폐스펙트럼",
        "틱장애",
        "뚜렛",
        "신체증상장애",
        "건강염려증",
        "해리장애",
        "해리성",
        "망상장애",
        "품행장애",
    }
)

DIAGNOSES_LONGEST_FIRST: tuple[str, ...] = tuple(sorted(DIAGNOSES, key=lambda s: (-len(s), s)))
