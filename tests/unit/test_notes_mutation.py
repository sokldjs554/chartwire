"""§9.2 MutationProvider: per-class detection on a hand-written corpus (200 mutations per class).

The detection table is printed with ``-s`` and copied into ``docs/dev/handoff/wp-d.md``; the
authoritative numbers come from ``chartwire eval`` (WP-F) on the synthetic corpus.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from chartwire.notes.extractive import ExtractiveProvider, build_draft
from chartwire.notes.providers.mutation import (
    EXPECTED_REASON,
    MUTATION_CLASSES,
    MutationClass,
    MutationProvider,
    mutate,
)
from chartwire.notes.schema import SegmentView, parse_draft
from chartwire.notes.verifier import verify
from tests.unit.test_notes_support import CONSULTATION, context, session

PATIENT = [
    "잠드는 데 두 시간쯤 걸려요",
    "하루에 4시간밖에 못 자요",
    "입맛이 없어요",
    "기분이 계속 가라앉아요",
    "아무것도 하기 싫어요",
    "에스시탈로프람 10mg 약 먹고 있어요",
    "약 먹으면 속이 울렁거려요",
    "회사에서 집중이 안 돼요",
    "가슴이 두근거리고 숨이 막혀요",
    "걱정이 멈추질 않아요",
    "술을 일주일에 세 번 마셔요",
    "체중이 두 달 만에 4kg 빠졌어요",
    "낮에 너무 피곤해요",
    "기운이 하나도 없어요",
    "쿠에티아핀 25mg 약은 잠은 좀 오게 해요",
    "약을 며칠 빼먹었어요",
    "수면 시간이 5시간 정도예요",
    "불안해서 밖에 잘 안 나가요",
]
CLINICIAN = [
    "오늘 표정이 좀 어두워 보이시네요",
    "말씀하시는 속도가 느리신 것 같아요",
    "목소리에 힘이 없어 보입니다",
    "손을 계속 만지작거리시네요",
    "에스시탈로프람을 15mg으로 올려보겠습니다",
    "쿠에티아핀은 그대로 유지하겠습니다",
    "2주 뒤에 뵙겠습니다",
    "수면일지를 써 오세요",
    "혈액검사 한번 해보겠습니다",
    "인지행동치료 의뢰를 드리겠습니다",
    "요즘 잠은 어떠세요?",
]


def random_session(rng: random.Random) -> list[SegmentView]:
    n_p, n_c = rng.randint(4, 8), rng.randint(3, 6)
    utts = [("patient", t) for t in rng.sample(PATIENT, n_p)] + [
        ("clinician", t) for t in rng.sample(CLINICIAN, n_c)
    ]
    rng.shuffle(utts)
    return session(*utts)  # type: ignore[arg-type]


def detection_rates(per_class: int = 200, seed: int = 42) -> dict[MutationClass, tuple[float, Counter[str]]]:
    rng = random.Random(seed)
    out: dict[MutationClass, tuple[float, Counter[str]]] = {}
    for cls in MUTATION_CLASSES:
        detected, reasons = 0, Counter[str]()
        done = 0
        while done < per_class:
            segs = random_session(rng)
            result = mutate(build_draft(segs), cls, segs, rng)
            if result is None:
                continue
            draft, applied = result
            st = verify(draft, segs).statements[applied.index]
            detected += st.verdict == "unsupported"
            reasons[st.verdict_reason or "supported"] += 1
            done += 1
        out[cls] = (detected / per_class, reasons)
    return out


@pytest.fixture(scope="module")
def rates() -> dict[MutationClass, tuple[float, Counter[str]]]:
    table = detection_rates()
    print("\n| class | detected | reasons |\n|---|---|---|")
    for cls, (rate, reasons) in table.items():
        print(f"| {cls} | {rate:.3f} | {dict(reasons)} |")
    return table


def test_structural_classes_are_always_detected(rates):
    for cls in ("fabricated_statement", "evidence_seq_wrong", "diagnosis_insert"):
        assert rates[cls][0] == 1.0, cls
        assert set(rates[cls][1]) == {EXPECTED_REASON[cls]}


def test_number_and_drug_classes_detected_at_least_98_percent(rates):
    assert rates["number_change"][0] >= 0.98
    assert rates["drug_swap"][0] >= 0.98
    assert rates["number_change"][1]["numeric_mismatch"] > 0 and rates["drug_swap"][1]["entity_mismatch"] > 0


def test_negation_and_speaker_classes_are_reported_honestly(rates):
    assert rates["negation_flip"][0] > 0.9
    assert rates["speaker_swap"][0] > 0.9
    # a swapped S→clinician evidence may trip numeric/negation rules before rule 6; still unsupported.
    assert "supported" not in rates["speaker_swap"][1] or rates["speaker_swap"][1]["supported"] < 20


def test_mutations_are_deterministic_by_seed():
    a = detection_rates(per_class=20, seed=7)
    b = detection_rates(per_class=20, seed=7)
    assert {k: v[0] for k, v in a.items()} == {k: v[0] for k, v in b.items()}


async def test_provider_applies_each_class_once_on_distinct_statements():
    p = MutationProvider(ExtractiveProvider(), MUTATION_CLASSES, seed=3)
    raw = await p.draft(context(CONSULTATION))
    assert raw.provider == "mutation(extractive)"
    applied = p.last_applied
    assert {m.cls for m in applied} == set(MUTATION_CLASSES)
    assert len({m.index for m in applied}) == len(applied)
    v = verify(parse_draft(raw.text), CONSULTATION)
    assert all(v.statements[m.index].verdict == "unsupported" for m in applied)


def test_unmutated_statements_stay_supported():
    rng = random.Random(1)
    segs = random_session(rng)
    base = build_draft(segs)
    draft, applied = mutate(base, "number_change", segs, rng)  # type: ignore[misc]
    v = verify(draft, segs)
    assert all(st.verdict == "supported" for i, st in enumerate(v.statements) if i != applied.index)
