"""Extractive SOAP drafting — the default, key-less, deterministic provider (spec §9.2).

Patient utterances that mention a symptom cue become ``S`` statements in reported form; clinician
utterances with an observation cue become ``O``; clinician utterances with a plan cue become
``P``. Every statement cites the *whole* utterance as its single evidence quote, so rule 2 of the
verifier matches exactly and rules 3–7 compare a text with its own source. Coverage is therefore
1.0 by construction — the only thing this provider can be wrong about is *relevance*, which the
clinician reviews.

The one exception to "whole utterance" is an identifier: when a patient says ``입맛이 없어요 집은
가온시 라온구 새벽로 13번길 17예요`` the note charts the symptom clause and cites only that clause,
and an utterance whose every chartable clause carries a phone number, road address or a named
person is not charted at all. A note is a document people read verbatim; the search index has its
own redaction, the note must not depend on it.
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
# ``뵙겠`` alone is a farewell (``그럼 다음에 뵙겠습니다``), a plan item only with a time: ``2주 뒤에 뵙겠습니다``.
FOLLOWUP_TIME_RE = re.compile(r"\d+\s*(?:주|일|달|개월|시간)|다음\s*주|한\s*달|내일|모레")

# Identifiers. The synthetic corpus appends one to 5 % of utterances (synth/scripts.py ``_inject_pii``:
# ``제 번호는 010-…예요`` / ``집은 …시 …구 …로 N번길 N예요`` / ``… 선생님이 소개해 주셨어요``), and real
# patients volunteer the same things. These are deterministic safety nets, not a name recogniser:
# a false positive only costs one clause of one statement, a false negative puts an address in a note.
PHONE_RE = re.compile(r"0\d{1,2}-\d{3,4}-\d{4}")
ADDRESS_RE = re.compile(
    r"[가-힣]+(?:시|도)\s+[가-힣]+(?:구|군)\s+[가-힣]+(?:로|길)\s*\d+(?:번길)?(?:\s*\d+)?"
)
# ``백예봄님도`` / ``박온솔 선생님`` — a 2–4 syllable token before 님 (any particle may follow: ``님도``,
# ``님께서``), or a full 3-syllable name before 선생님. Honorific nouns that end in 님 are not names.
NAME_RE = re.compile(r"(?<![가-힣])([가-힣]{2,4})(?=님)|(?<![가-힣])([가-힣]{3})(?=\s?선생님)")
_NOT_NAMES = frozenset(
    {"선생", "사모", "부모", "어머", "아버", "고객", "환자", "아드", "며느", "스승", "장모", "장인", "형수", "도련",
     "임금", "하느", "정신과", "소아과", "내과", "외과", "주치의", "담당의"}
)  # fmt: skip
_CLAUSE_SPLIT = re.compile(r"(?<=[요죠다까])\s+|(?<=[.!?])\s*")


def has_identifier(text: str) -> bool:
    """True when ``text`` carries a phone number, a road address or a named person."""
    if PHONE_RE.search(text) or ADDRESS_RE.search(text):
        return True
    return any((m.group(1) or m.group(2)) not in _NOT_NAMES for m in NAME_RE.finditer(text))


def chartable_text(seg: SegmentView) -> str | None:
    """The part of an utterance the note may quote.

    Without an identifier that is the whole utterance. With one, it is the first clause that carries
    this speaker's cue and no identifier; ``None`` when every cue clause also carries an identifier
    (``제 번호는 …`` alone, or ``… 선생님이 소개해 주셨어요`` with the symptom in the same clause).
    """
    text = seg.text.strip()
    if not has_identifier(text):
        return text
    cue = (
        SYMPTOM_CUES
        if seg.speaker == "patient"
        else re.compile(f"{OBSERVATION_CUES.pattern}|{PLAN_CUES.pattern}")
    )
    for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
        if len(clause) >= _MIN_QUOTE_CHARS and cue.search(clause) and not has_identifier(clause):
            return clause
    return None


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
    for raw in sorted(segments, key=lambda s: s.seq):
        text = chartable_text(raw)
        if text is None:
            continue
        # The quote and every rule below see the chartable clause; the verifier still matches it as a
        # substring of the stored segment (rule 2), so evidence offsets point into the real utterance.
        seg = raw if text == raw.text.strip() else raw.model_copy(update={"text": text})
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
        cues = set(PLAN_CUES.findall(text))
        if cues == {"뵙겠"} and not FOLLOWUP_TIME_RE.search(text):
            return None  # farewell, not a follow-up plan
        if cues:
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
