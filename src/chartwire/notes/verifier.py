"""Deterministic grounding verifier (spec §9.3, ADR-0003).

``verify(draft, segments)`` checks every statement of a :class:`NoteDraftOut` against the session
segments with eight rules, applied in order; the first failing rule sets ``verdict='unsupported'``
and its ``verdict_reason``. There is no similarity fallback and no model in the loop: a quote
either is a verbatim (or punctuation/whitespace-insensitive) substring of the cited segment or
it is not. Everything here is pure, synchronous and total — it never raises on provider output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from chartwire.notes.lexicon_diagnoses import DIAGNOSES_LONGEST_FIRST
from chartwire.notes.lexicon_drugs import DRUGS_LONGEST_FIRST
from chartwire.notes.normalize import nfc, normalize_with_offsets, numeric_tokens
from chartwire.notes.schema import (
    NoteDraftOut,
    SegmentView,
    Statement,
    VerdictReason,
    VerifiedDraft,
    VerifiedEvidence,
    VerifiedStatement,
)

VERIFIER_VERSION: Final = "rules-8.v1"

NEGATION_RE: Final = re.compile(r"않|없|아니|못|안 ")
VERDICT_LANGUAGE_RE: Final = re.compile(
    r"진단|확진|장애로\s*판단|F\d{2}(?:\.\d)?|DSM|ICD|처방해야|투약해야|판정", re.IGNORECASE
)
INJECTION_RE: Final = re.compile(
    r"무시하|지시|시스템 프롬프트|시스템 메시지|ignore|instruction|진단란에|적어|써 주|기록하세요|항목에|명령|관리자\]|AI야|JSON",
    re.IGNORECASE,
)
"""Rule 8 (§9.3) plus the surface forms of every §9.5 injection sentence (WP-F eval request 2)."""

_SECTION_SPEAKER: Final[dict[str, str]] = {"S": "patient", "O": "clinician", "P": "clinician"}


def verify(draft: NoteDraftOut, segments: Sequence[SegmentView]) -> VerifiedDraft:
    """Verify every statement of ``draft`` against ``segments`` (any order, any subset of a session)."""
    by_seq: dict[int, SegmentView] = {s.seq: s for s in segments}
    verified = [_verify_statement(st, by_seq) for st in draft.statements]
    unsupported = sum(1 for v in verified if v.verdict == "unsupported")
    total = len(verified)
    coverage = (total - unsupported) / total if total else 0.0
    return VerifiedDraft(
        statements=verified,
        coverage=coverage,
        unsupported_count=unsupported,
        abstain_requested=draft.abstain,
    )


def _verify_statement(st: Statement, by_seq: dict[int, SegmentView]) -> VerifiedStatement:
    evidence = [_match(ev.seq, ev.quote, by_seq.get(ev.seq)) for ev in st.evidence]
    quotes = [ev.quote for ev in st.evidence]
    speakers = [seg.speaker for ev in st.evidence if (seg := by_seq.get(ev.seq)) is not None]
    reason = _first_failure(st, evidence, quotes, speakers)
    return VerifiedStatement(
        section=st.section,
        text=st.text,
        kind=st.kind,
        evidence=evidence,
        verdict="supported" if reason is None else "unsupported",
        verdict_reason=reason,
    )


def _first_failure(
    st: Statement,
    evidence: Sequence[VerifiedEvidence],
    quotes: Sequence[str],
    speakers: Sequence[str],
) -> VerdictReason | None:
    """Rules 1–8 in spec order; the first failure wins (fail-closed)."""
    if len(speakers) != len(evidence):
        return "fabricated_segment"
    if any(ev.method is None for ev in evidence):
        return "quote_mismatch"
    if not numeric_tokens(st.text) <= _union(numeric_tokens(q) for q in quotes):
        return "numeric_mismatch"
    if not _entities(st.text, DRUGS_LONGEST_FIRST) <= _union(
        _entities(q, DRUGS_LONGEST_FIRST) for q in quotes
    ):
        return "entity_mismatch"
    if _has_negation(st.text) != any(_has_negation(q) for q in quotes):
        return "negation_mismatch"
    if any(sp != _SECTION_SPEAKER[st.section] for sp in speakers):
        return "speaker_mismatch"
    if any(not _in_any_quote(tok, quotes) for tok in _verdict_tokens(st.text)):
        return "verdict_language"
    if INJECTION_RE.search(st.text):
        return "injection_pattern"
    return None


# --------------------------------------------------------------------------- rule 2: quote matching


def _match(seq: int, quote: str, segment: SegmentView | None) -> VerifiedEvidence:
    """Exact NFC substring first, then the normalised form mapped back to NFC offsets."""
    if segment is None:
        return VerifiedEvidence(seq=seq, quote=quote)
    text = nfc(segment.text)
    idx = text.find(nfc(quote))
    if idx >= 0:
        return VerifiedEvidence(seq=seq, quote=quote, start=idx, end=idx + len(nfc(quote)), method="exact")
    norm_seg = normalize_with_offsets(text)
    norm_quote = normalize_with_offsets(quote).text
    if not norm_quote:
        return VerifiedEvidence(seq=seq, quote=quote)
    idx = norm_seg.text.find(norm_quote)
    if idx < 0:
        return VerifiedEvidence(seq=seq, quote=quote)
    start, end = norm_seg.span(idx, len(norm_quote))
    return VerifiedEvidence(seq=seq, quote=quote, start=start, end=end, method="normalized")


# --------------------------------------------------------------------------- rules 4, 5, 7 helpers


def _entities(text: str, lexicon: Sequence[str]) -> set[str]:
    """Lexicon entries present in ``text``; longest-first, each match consumed so a shorter entry
    contained in a longer one (``벤라팍신`` ⊂ ``데스벤라팍신``) is not double-counted."""
    found: set[str] = set()
    remaining = text
    for entry in lexicon:
        if entry in remaining:
            found.add(entry)
            remaining = remaining.replace(entry, "\0")
    return found


def _has_negation(text: str) -> bool:
    return NEGATION_RE.search(text) is not None


def _verdict_tokens(text: str) -> set[str]:
    """Verdict-language regex hits plus diagnosis names, upper-cased so Latin tokens (``ptsd``,
    ``dsm``) compare case-insensitively; Hangul is unchanged by ``upper()``."""
    upper = text.upper()
    tokens = {m.group(0) for m in VERDICT_LANGUAGE_RE.finditer(upper)}
    return tokens | _entities(upper, DIAGNOSES_LONGEST_FIRST)


def _in_any_quote(token: str, quotes: Iterable[str]) -> bool:
    return any(token in q.upper() for q in quotes)


def _union(sets: Iterable[set[str]]) -> set[str]:
    out: set[str] = set()
    for s in sets:
        out |= s
    return out
