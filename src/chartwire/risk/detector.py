"""Deterministic risk detector (spec §3.1, §9.4; ADR-0003).

``scan(text, speaker)`` returns at most one :class:`RiskHit` per segment — the highest-severity
alertable hit if any, otherwise the highest-severity suppressed hit (so callers can still count
``risk_hits_total{suppressed="true"}``). Callers create ``risk_events`` only for hits where
``hit.alerts`` is true (§7.4).

Candidate selection: every lexicon phrase occurrence is a candidate (matching ignores
whitespace, so ``죽고싶어요`` and ``죽고  싶어요`` both hit the stem ``죽고 싶``); a candidate whose span lies
strictly inside another candidate's span is the same mention seen through a shorter stem
(``손목`` inside ``손목을 긋``) and is dropped, so scope rules are evaluated on the most specific
phrase. Ranking: alertable first, then severity, then phrase length, then earliest position.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from chartwire.risk.lexicon_ko import PHRASES, Category
from chartwire.risk.scope import ScopeFlags, classify, find_spans

DETECTOR_VERSION = "lex-1"

PastRule = Literal["split", "spec_literal", "suppress_all"]


@dataclass(frozen=True)
class Policy:
    """The two knobs of the shipped detector that a rival reading of §9.4 would turn.

    ``DEFAULT`` *is* ``lex-1``; the other values exist so the adoption harness
    (``chartwire eval adopt``) can measure the road not taken on the same frozen set instead of
    arguing about it. Nothing in the serving path passes a policy.

    * ``past`` — what a past marker does to a hit. ``split`` (lex-1): suppressed only with an
      explicit present denial, otherwise severity − 1; ``spec_literal`` (§9.4 as written): always
      severity − 1, a present denial changes nothing; ``suppress_all``: any past marker suppresses.
    * ``min_severity`` — the alert floor. lex-1 alerts from severity 1.
    """

    past: PastRule = "split"
    min_severity: int = 1


DEFAULT_POLICY = Policy()


@dataclass(frozen=True)
class RiskHit:
    category: Category
    severity: int
    phrase: str
    start: int
    end: int
    scope: ScopeFlags
    alert_floor: int = 1
    """Lowest severity that still alerts — ``Policy.min_severity`` of the scan that produced the hit."""

    @property
    def suppressed(self) -> bool:
        return self.scope.suppressed

    @property
    def alerts(self) -> bool:
        """§9.4: alert when severity ≥ the floor (1 for lex-1) and no suppressing scope flag."""
        return self.severity >= self.alert_floor and not self.scope.suppressed


def scan(text: str, speaker: str, *, policy: Policy = DEFAULT_POLICY) -> list[RiskHit]:
    if not text:
        return []
    spans = _candidate_spans(text)
    best: RiskHit | None = None
    best_key: tuple[bool, int, int, int] | None = None
    for start, end, category, base_severity, phrase in spans:
        if any(o_s <= start and end <= o_e and (o_e - o_s) > (end - start) for o_s, o_e, *_ in spans):
            continue
        flags = _apply_past_rule(classify(text, start, end, speaker), policy.past)
        # §9.4 / docs/risk-detection.md §9: past *without* a present denial is a lowered alert;
        # past *with* one (``지금은 아니에요``) is suppressed by ``ScopeFlags.suppressed``.
        demoted = flags.past and not flags.present_denial
        severity = max(base_severity - 1, 0) if demoted else base_severity
        hit = RiskHit(category, severity, phrase, start, end, flags, policy.min_severity)
        key = (hit.alerts, severity, end - start, -start)
        if best_key is None or key > best_key:
            best, best_key = hit, key
    return [best] if best is not None else []


def _apply_past_rule(flags: ScopeFlags, rule: PastRule) -> ScopeFlags:
    """Re-read the past flags under a rival rule. ``split`` returns them untouched.

    The rivals are expressed through ``present_denial`` because that is the one flag
    ``ScopeFlags.suppressed`` and the demotion above both key on: forcing it off makes every past
    hit a demoted alert (§9.4 as written), forcing it on makes every past hit suppressed.
    """
    if rule == "split" or not flags.past:
        return flags
    return replace(flags, present_denial=rule == "suppress_all")


def _candidate_spans(text: str) -> list[tuple[int, int, Category, int, str]]:
    """Every lexicon occurrence, matched whitespace-insensitively (``죽고싶어요`` hits ``죽고 싶``)."""
    out: list[tuple[int, int, Category, int, str]] = []
    for phrase in PHRASES:
        for start, end in find_spans(text, phrase.text):
            out.append((start, end, phrase.category, phrase.severity, phrase.text))
    return out
