"""Psychotropic generic names the verifier treats as entities (spec §9.3 rule 4).

Patient-visible Korean generic names only; brand names are deliberately absent so the list stays
small and reviewable. Matching is longest-first substring (``데스벤라팍신`` is never counted as a
``벤라팍신`` mention).
"""

from __future__ import annotations

DRUGS: frozenset[str] = frozenset(
    {
        # SSRI / SNRI / other antidepressants
        "에스시탈로프람",
        "시탈로프람",
        "서트랄린",
        "플루옥세틴",
        "파록세틴",
        "플루복사민",
        "벤라팍신",
        "데스벤라팍신",
        "둘록세틴",
        "밀나시프란",
        "부프로피온",
        "미르타자핀",
        "트라조돈",
        "아고멜라틴",
        "보티옥세틴",
        "아미트립틸린",
        "노르트립틸린",
        "이미프라민",
        "클로미프라민",
        "티아넵틴",
        # mood stabilisers
        "리튬",
        "발프로산",
        "라모트리진",
        "카바마제핀",
        "옥스카바제핀",
        # antipsychotics
        "쿠에티아핀",
        "올란자핀",
        "리스페리돈",
        "아리피프라졸",
        "팔리페리돈",
        "루라시돈",
        "지프라시돈",
        "아미설프리드",
        "클로자핀",
        "할로페리돌",
        "브렉스피프라졸",
        # anxiolytics / hypnotics
        "알프라졸람",
        "로라제팜",
        "클로나제팜",
        "디아제팜",
        "에티졸람",
        "부스피론",
        "졸피뎀",
        "조피클론",
        "멜라토닌",
        "라멜테온",
        "수보렉산트",
        "하이드록시진",
        "프로프라놀롤",
        # ADHD
        "메틸페니데이트",
        "아토목세틴",
        "클로니딘",
        # addiction
        "날트렉손",
        "아캄프로세이트",
        "디설피람",
        # other
        "프레가발린",
        "가바펜틴",
    }
)

DRUGS_LONGEST_FIRST: tuple[str, ...] = tuple(sorted(DRUGS, key=lambda s: (-len(s), s)))
