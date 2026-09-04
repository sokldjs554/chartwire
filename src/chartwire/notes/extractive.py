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

SYMPTOM_CUES = re.compile(
    r"잠|수면|입맛|식욕|기분|우울|불안|두근|집중|피곤|기운|약|복용|부작용|술|체중"
    r"|자요|깨서|빠졌|소주|맥주|mg|먹고"  # §10.1 fact utterances without a §9.2 cue (WP-F request 3)
)
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
    per_section: dict[Section, list[Statement]] = {"S": [], "O": [], "P": []}
    for seg in sorted(segments, key=lambda s: s.seq):
        section = classify(seg)
        if section is None or len(per_section[section]) >= max_per_section:
            continue
        per_section[section].append(_statement(section, seg))
    return NoteDraftOut(statements=per_section["S"] + per_section["O"] + per_section["P"])


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
