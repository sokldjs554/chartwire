"""§9.2 ParaphrasingMockProvider: verbatim quotes, per-transform false-rejection rate.

The table is printed with ``-s`` and copied into the handoff; ``chartwire eval`` (WP-F) produces
the authoritative ``paraphrase.json``.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from chartwire.notes.extractive import build_draft
from chartwire.notes.providers.paraphrase import TRANSFORMS, ParaphrasingMockProvider, Transform, paraphrase
from chartwire.notes.schema import parse_draft
from chartwire.notes.verifier import verify
from tests.unit.test_notes_mutation import random_session
from tests.unit.test_notes_support import CONSULTATION, context, session


def false_rejections(n_sessions: int = 300, seed: int = 42) -> dict[Transform, tuple[int, int, Counter[str]]]:
    """transform → (rejected, total, reasons)."""
    rng = random.Random(seed)
    out: dict[Transform, tuple[int, int, Counter[str]]] = {t: (0, 0, Counter()) for t in TRANSFORMS}
    for _ in range(n_sessions):
        segs = random_session(rng)
        draft, applied = paraphrase(build_draft(segs), rng)
        v = verify(draft, segs)
        for a in applied:
            st = v.statements[a.index]
            rejected, total, reasons = out[a.transform]
            if st.verdict == "unsupported":
                reasons[st.verdict_reason or "?"] += 1
            out[a.transform] = (rejected + (st.verdict == "unsupported"), total + 1, reasons)
    return out


@pytest.fixture(scope="module")
def table() -> dict[Transform, tuple[int, int, Counter[str]]]:
    t = false_rejections()
    print("\n| transform | rejected/total | rate | reasons |\n|---|---|---|---|")
    for name, (rej, tot, reasons) in t.items():
        print(f"| {name} | {rej}/{tot} | {rej / tot if tot else 0:.3f} | {dict(reasons)} |")
    rej, tot = sum(v[0] for v in t.values()), sum(v[1] for v in t.values())
    print(f"| **all** | {rej}/{tot} | {rej / tot:.3f} | |")
    return t


def test_every_transform_is_exercised(table):
    assert all(total > 0 for _, total, _ in table.values())


def test_surface_transforms_are_never_rejected(table):
    for t in ("ending_plain", "ending_formal", "particle_swap", "merge", "number_word", "honorific_drop"):
        rejected, _total, reasons = table[t]
        assert rejected == 0, (t, dict(reasons))


def test_synonym_rejections_are_only_the_negation_rule(table):
    rejected, total, reasons = table["synonym"]
    assert set(reasons) <= {"negation_mismatch"}
    # `잠을 못 자요→수면 곤란`, `입맛이 없어요→식욕 저하`, `기운이 없어요→무기력감` absorb the negation
    # into a noun: rule 5 (fail-closed) rejects them. Reported, not tuned away.
    assert rejected <= total


def test_overall_false_rejection_rate_is_reported(table):
    rej, tot = sum(v[0] for v in table.values()), sum(v[1] for v in table.values())
    assert rej / tot <= 0.10, "residual causes must stay confined to the synonym transform"


def test_quotes_are_kept_verbatim_and_deterministic():
    rng = random.Random(5)
    base = build_draft(CONSULTATION)
    a, applied = paraphrase(base, random.Random(5))
    b, _ = paraphrase(base, rng)
    assert a == b and applied
    original = {e.quote for st in base.statements for e in st.evidence}
    assert {e.quote for st in a.statements for e in st.evidence} <= original
    assert all(st.text != base.statements[0].text for st in a.statements[:1])


def test_merge_yields_two_evidences_and_number_word_swaps_direction():
    segs = session(
        ("patient", "잠을 못 자요"), ("patient", "입맛이 없어요"), ("patient", "잠드는 데 2시간쯤 걸려요")
    )
    merged = next(
        st
        for seed in range(50)
        for st in paraphrase(build_draft(segs), random.Random(seed))[0].statements
        if len(st.evidence) == 2
    )
    assert merged.text == "잠을 못 잔다, 입맛이 없다"
    texts = {
        st.text
        for seed in range(50)
        for st in paraphrase(build_draft(segs), random.Random(seed))[0].statements
    }
    assert "잠드는 데 두 시간쯤 걸린다" in texts


async def test_provider_path_and_provenance():
    p = ParaphrasingMockProvider(seed=11)
    raw = await p.draft(context(CONSULTATION))
    assert raw.provider == "paraphrase(extractive)" and raw.model is None
    assert p.last_applied
    v = verify(parse_draft(raw.text), CONSULTATION)
    assert v.coverage >= 0.8
