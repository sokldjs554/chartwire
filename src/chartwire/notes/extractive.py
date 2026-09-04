"""Extractive SOAP drafting — the default, key-less, deterministic provider (spec §9.2).

Patient utterances that mention a symptom cue become ``S`` statements in reported form; clinician
utterances with an observation cue become ``O``; clinician utterances with a plan cue become
``P``. Every statement cites the *whole* utterance as its single evidence quote, so rule 2 of the
verifier matches exactly and rules 3–7 compare a text with its own source. Coverage is therefore
1.0 by construction — the only thing this provider can be wrong about is *relevance*, which the
clinician reviews.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from chartwire.notes.korean import report_form
from chartwire.notes.providers.base import Stopwatch, raw_from_draft
from chartwire.notes.schema import (
    DraftContext,
    Evidence,
    NoteDraftOut,
    RawDraft,
    Section,
    SegmentView,
    Statement,
    StatementKind,
)
from chartwire.notes.verifier import INJECTION_RE

CUE_FAMILIES: tuple[tuple[str, str], ...] = (
    # §9.2 cues split into the fact families of §10.1, most-scarce first. The split exists because
    # a section holds ≤12 statements while a session repeats the early phases far more often than
    # the late ones (약물·음주 come last in the §10.2 phase order): picking one utterance per family
    # before filling by seq is what keeps medication and alcohol facts in the draft at all.
    ("medication", r"약|복용|부작용|mg|먹고"),
    ("alcohol", r"술|소주|맥주"),
    ("appetite", r"입맛|식욕|빠졌|체중"),
    ("anxiety", r"불안|두근"),
    ("concentration", r"집중"),
    ("mood", r"기분|우울|피곤|기운"),
    ("sleep", r"잠|수면|자요|깨서"),
    # §10.1 fact utterances without a §9.2 cue (WP-F request 3 + 품질 패스 2): 새벽에 깨서 / 하루 N시간 /
    # N kg 빠졌 / 소주 N병 / {drug} {dose}mg 먹고 / N주 정도 됐어요 · N주 전부터요 (duration)
    ("duration", r"됐어요|됐고|된 것 같|정도 됐|주 전부터"),
)
_FAMILY_RE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in CUE_FAMILIES
)
SYMPTOM_CUES = re.compile("|".join(pattern for _, pattern in CUE_FAMILIES))


def cue_family(text: str) -> str | None:
    """The §10.1 fact family a patient utterance belongs to (first match in ``CUE_FAMILIES``)."""
    for name, pattern in _FAMILY_RE:
        if pattern.search(text):
            return name
    return None


OBSERVATION_CUES = re.compile(r"보이시|보입니다|표정|말속도|목소리|시선|위생|안절부절|눈물")
PLAN_CUES = re.compile(r"올려|줄여|유지|처방|드리겠|뵙겠|의뢰|검사|일지|연습|주 뒤|다음 주|2주|한 달")
# A clinician *question* (``지난 2주 동안 어떻게 지내셨어요?``) is neither an observation nor a plan even
# when it contains a cue word; ``…세요`` is not excluded because imperatives are plans (``써 오세요``).
QUESTION_RE = re.compile(r"(\?|나요|십니까|까요)\s*$")

MAX_QUOTE_CHARS = 200
"""``Evidence.quote`` limit (§9.1). Longer utterances are cited by their first 190 characters in the
direct-quotation form so both quote and statement stay within the schema limits."""
_QUOTE_PREFIX_CHARS = 190
_MIN_QUOTE_CHARS = 4
_KIND: dict[Section, StatementKind] = {"S": "reported", "O": "observed", "P": "plan_item"}


def build_draft(segments: Iterable[SegmentView], *, max_per_section: int = 12) -> NoteDraftOut:
    """Pure core of the provider — also used by the mutation and paraphrase mocks."""
    buckets: dict[Section, dict[str, list[SegmentView]]] = {"S": {}, "O": {}, "P": {}}
    seen: dict[Section, set[str]] = {"S": set(), "O": set(), "P": set()}
    for seg in sorted(segments, key=lambda s: s.seq):
        section = classify(seg)
        if section is None:
            continue
        # A repeated utterance ("한 3주 됐어요" recurs through a session) is one fact, not two:
        # charting it twice spends a scarce ≤12 slot the later phases need.
        key = "".join(seg.text.split())
        if key in seen[section]:
            continue
        seen[section].add(key)
        family = (cue_family(seg.text) or section) if section == "S" else section
        buckets[section].setdefault(family, []).append(seg)
    statements: list[Statement] = []
    for target in ("S", "O", "P"):
        picked = _select(buckets[target], max_per_section)
        statements.extend(_statement(target, seg) for seg in sorted(picked, key=lambda s: s.seq))
    return NoteDraftOut(statements=statements)


def _select(buckets: dict[str, list[SegmentView]], limit: int) -> list[SegmentView]:
    """≤``limit`` segments: one per cue family (scarcest first) per round, then the next round."""
    order = [name for name, _ in CUE_FAMILIES if name in buckets]
    order += [name for name in buckets if name not in order]
    picked: list[SegmentView] = []
    depth = 0
    while len(picked) < limit:
        progressed = False
        for name in order:
            items = buckets[name]
            if depth < len(items):
                picked.append(items[depth])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        depth += 1
    return picked


def classify(seg: SegmentView) -> Section | None:
    """Section for one segment, or ``None`` when it is not chartable by this provider."""
    text = seg.text.strip()
    if len(text) < _MIN_QUOTE_CHARS or INJECTION_RE.search(text):
        return None
    if seg.speaker == "patient":
        return "S" if SYMPTOM_CUES.search(text) else None
    if seg.speaker == "clinician" and not QUESTION_RE.search(text):
        if OBSERVATION_CUES.search(text):
            return "O"
        if PLAN_CUES.search(text):
            return "P"
    return None


def _statement(section: Section, seg: SegmentView) -> Statement:
    quote = seg.text.strip()
    if len(quote) > MAX_QUOTE_CHARS:
        quote = quote[:_QUOTE_PREFIX_CHARS].rstrip()
        text = f"“{quote}”라고 함" if section == "S" else quote
    else:
        text = report_form(quote) if section == "S" else quote
    return Statement(
        section=section, text=text, evidence=[Evidence(seq=seg.seq, quote=quote)], kind=_KIND[section]
    )


class ExtractiveProvider:
    name = "extractive"

    async def draft(self, ctx: DraftContext) -> RawDraft:
        watch = Stopwatch()
        draft = build_draft(ctx.segments, max_per_section=ctx.max_per_section)
        return raw_from_draft(draft, provider=self.name, latency_ms=watch.elapsed_ms)
