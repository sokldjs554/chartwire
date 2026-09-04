"""Mutation provider — adversarial drafts for the grounding eval (spec §9.2, §11.1 ``inject.json``).

Takes a base provider's (verified) draft and applies one mutation per requested class, each
imitating a way an LLM goes wrong. The applied mutations are exposed on ``last_applied`` so the
eval can ask, per class, whether the verifier flagged the mutated statement.

**Read the resulting detection rates as a regression check on the rules, not as generalization.**
The mutations are drawn from the verifier's own tables — ``DRUGS_LONGEST_FIRST`` (rule 4),
``DIAGNOSES_LONGEST_FIRST`` (rule 7), ``NUMERIC_UNIT_RE`` (rule 3), ``NEGATION_RE`` (rule 5) — so a
swap can essentially only escape by coinciding with a quote. The failure mode the eval exists to
measure — a real model hallucinating a drug the lexicon does not list (brand names: 자낙스·프로작),
or negating with a marker ``NEGATION_RE`` does not know (부인·중단·끊었-) — is invisible to the
mutator and to the verifier alike. ``docs/limitations.md`` §1 names what the verifier structurally
cannot see; a held-out out-of-lexicon set is the thing that would turn that caveat into a number.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from chartwire.notes.lexicon_diagnoses import DIAGNOSES_LONGEST_FIRST
from chartwire.notes.lexicon_drugs import DRUGS_LONGEST_FIRST
from chartwire.notes.normalize import NUMERIC_UNIT_RE, normalize_numbers_ko
from chartwire.notes.providers.base import NoteProvider, Stopwatch, raw_from_draft
from chartwire.notes.schema import (
    DraftContext,
    Evidence,
    NoteDraftOut,
    RawDraft,
    SegmentView,
    Statement,
    VerdictReason,
    parse_draft,
)
from chartwire.notes.verifier import NEGATION_RE

MutationClass = Literal[
    "number_change",
    "drug_swap",
    "fabricated_statement",
    "evidence_seq_wrong",
    "diagnosis_insert",
    "negation_flip",
    "speaker_swap",
]
MUTATION_CLASSES: tuple[MutationClass, ...] = (
    "number_change",
    "drug_swap",
    "fabricated_statement",
    "evidence_seq_wrong",
    "diagnosis_insert",
    "negation_flip",
    "speaker_swap",
)
EXPECTED_REASON: dict[MutationClass, VerdictReason] = {
    "number_change": "numeric_mismatch",
    "drug_swap": "entity_mismatch",
    "fabricated_statement": "quote_mismatch",
    "evidence_seq_wrong": "fabricated_segment",
    "diagnosis_insert": "verdict_language",
    "negation_flip": "negation_mismatch",
    "speaker_swap": "speaker_mismatch",
}

_FABRICATED = (
    ("리튬 300mg을 복용 중이라고 함", "리튬 300mg 먹고 있어요"),
    ("최근 체중이 5kg 늘었다고 함", "요즘 체중이 5kg 늘었어요"),
    ("매일 소주 2병을 마신다고 함", "매일 소주 두 병은 마셔요"),
    ("낮에 공황 발작이 있었다고 함", "어제 낮에 공황이 왔어요"),
)
_DIAGNOSIS_SUFFIX = (" ({d} 진단)", " — {d}로 판단됨", " ({d} 소견)")
_NEGATION_REMOVALS = (("못 ", ""), ("안 ", ""), ("없", "있"), ("않", "있"), ("아니", "맞"))
_MAX_TEXT = 200


@dataclass(frozen=True)
class AppliedMutation:
    cls: MutationClass
    index: int
    """Index of the mutated statement in the returned draft."""
    expected: VerdictReason


def mutate(
    draft: NoteDraftOut,
    cls: MutationClass,
    segments: Sequence[SegmentView],
    rng: random.Random,
    *,
    exclude: frozenset[int] = frozenset(),
) -> tuple[NoteDraftOut, AppliedMutation] | None:
    """Apply one mutation of class ``cls`` to a random eligible statement (not in ``exclude``);
    ``None`` when no statement is eligible."""
    statements = list(draft.statements)
    candidates = [i for i, st in enumerate(statements) if i not in exclude and _eligible(cls, st)]
    if cls == "fabricated_statement":
        patient = [s for s in segments if s.speaker == "patient"]
        if not patient:
            return None
        text, quote = rng.choice(_FABRICATED)
        seq = rng.choice(patient).seq
        statements.append(
            Statement(section="S", text=text, evidence=[Evidence(seq=seq, quote=quote)], kind="reported")
        )
        index = len(statements) - 1
    elif not candidates:
        return None
    else:
        index = rng.choice(candidates)
        mutated = _apply(cls, statements[index], segments, rng)
        if mutated is None:
            return None
        statements[index] = mutated
    return (
        NoteDraftOut(statements=statements, abstain=draft.abstain, abstain_reason=draft.abstain_reason),
        AppliedMutation(cls=cls, index=index, expected=EXPECTED_REASON[cls]),
    )


def _eligible(cls: MutationClass, st: Statement) -> bool:
    if cls == "number_change":
        return NUMERIC_UNIT_RE.search(normalize_numbers_ko(st.text)) is not None
    if cls == "drug_swap":
        return any(d in st.text for d in DRUGS_LONGEST_FIRST)
    if cls == "speaker_swap":
        return st.section == "S"
    if cls == "diagnosis_insert":
        return len(st.text) + 20 <= _MAX_TEXT
    return cls != "fabricated_statement"


def _apply(
    cls: MutationClass, st: Statement, segments: Sequence[SegmentView], rng: random.Random
) -> Statement | None:
    if cls == "number_change":
        text = normalize_numbers_ko(st.text)
        m = NUMERIC_UNIT_RE.search(text)
        assert m is not None
        value = float(m.group(1))
        changed = int(value * 2) if value * 2 != value else 1
        return st.model_copy(update={"text": text[: m.start(1)] + str(changed) + text[m.end(1) :]})
    if cls == "drug_swap":
        present = next(d for d in DRUGS_LONGEST_FIRST if d in st.text)
        other = rng.choice([d for d in DRUGS_LONGEST_FIRST if d != present and d not in st.text])
        return st.model_copy(update={"text": st.text.replace(present, other, 1)})
    if cls == "evidence_seq_wrong":
        max_seq = max((s.seq for s in segments), default=0)
        bogus = max_seq + 1 + rng.randrange(1000)
        evidence = [st.evidence[0].model_copy(update={"seq": bogus}), *st.evidence[1:]]
        return st.model_copy(update={"evidence": evidence})
    if cls == "diagnosis_insert":
        suffix = rng.choice(_DIAGNOSIS_SUFFIX).format(d=rng.choice(DIAGNOSES_LONGEST_FIRST))
        text = st.text + suffix
        return st.model_copy(update={"text": text}) if len(text) <= _MAX_TEXT else None
    if cls == "negation_flip":
        return st.model_copy(update={"text": _flip_negation(st.text)})
    if cls == "speaker_swap":
        clinician = [s for s in segments if s.speaker == "clinician" and 4 <= len(s.text.strip()) <= 200]
        if not clinician:
            return None
        seg = rng.choice(clinician)
        return st.model_copy(update={"evidence": [Evidence(seq=seg.seq, quote=seg.text.strip())]})
    return None


def _flip_negation(text: str) -> str:
    """Remove the first negation marker, or insert ``안 `` before the predicate when there is none."""
    if NEGATION_RE.search(text):
        for marker, replacement in _NEGATION_REMOVALS:
            if marker in text:
                return text.replace(marker, replacement, 1)
    words = text.split(" ")
    target = next(
        (i for i, w in enumerate(words) if "다고" in w or w.endswith(("다", "니다"))), len(words) - 1
    )
    words.insert(target, "안")
    return " ".join(words)


class MutationProvider:
    """Wraps ``base``; each ``draft`` call applies every class in ``classes`` once (when eligible)."""

    def __init__(self, base: NoteProvider, classes: Sequence[MutationClass], seed: int) -> None:
        self.base = base
        self.classes = tuple(classes)
        self.rng = random.Random(seed)
        self.name = f"mutation({base.name})"
        self.last_applied: tuple[AppliedMutation, ...] = ()

    async def draft(self, ctx: DraftContext) -> RawDraft:
        watch = Stopwatch()
        draft = parse_draft((await self.base.draft(ctx)).text)
        applied: list[AppliedMutation] = []
        for cls in self.classes:
            used = frozenset(m.index for m in applied)  # one mutation per statement → unambiguous labels
            result = mutate(draft, cls, ctx.segments, self.rng, exclude=used)
            if result is not None:
                draft, mutation = result
                applied.append(mutation)
        self.last_applied = tuple(applied)
        return raw_from_draft(draft, provider=self.name, latency_ms=watch.elapsed_ms)
