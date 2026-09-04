"""Risk-detector evaluation (spec §11.1 ``risk_heldout.json`` / ``risk_ingrammar.json``).

Alert-worthy = ``detector.scan(text, speaker)`` returns a hit with ``alerts`` (severity ≥ 1 and no
suppressing scope flag). Precision / recall / F1 are computed on that binary decision; the
per-kind table shows where the false positives come from (``idiom``, ``past``, ...).

The held-out set is the headline: hand-written before the lexicon existed, hash-frozen, never
tuned against. The in-grammar set shares its vocabulary with the rules and is a regression check
only. Both report the same fields so the README can show them as two rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from chartwire.risk.detector import DETECTOR_VERSION, scan
from chartwire.synth.gold import alert_expected
from chartwire.synth.scripts import Script

PAST_KIND = "past"


@dataclass
class Tally:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    reasons_fp: Counter[str] = field(default_factory=Counter)
    """Suppression state of the false positives: ``category:severity`` of the alerting hit."""

    def add(self, expected: bool, alerted: bool, label: str | None = None) -> None:
        if expected and alerted:
            self.tp += 1
        elif expected:
            self.fn += 1
        elif alerted:
            self.fp += 1
            if label:
                self.reasons_fp[label] += 1
        else:
            self.tn += 1

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def _alerting_hit(text: str, speaker: str) -> tuple[bool, str | None]:
    hits = scan(text, speaker)
    if not hits:
        return False, None
    hit = hits[0]
    return hit.alerts, f"{hit.category}:{hit.severity}"


def per_kind_table(rows: list[tuple[str, bool, bool]]) -> dict[str, dict[str, Any]]:
    """``kind → {n, expected, alerted, fp, fn, fp_rate}`` from ``(kind, expected, alerted)`` triples."""
    kinds: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "expected": 0, "alerted": 0, "fp": 0, "fn": 0}
    )
    for kind, expected, alerted in rows:
        row = kinds[kind]
        row["n"] += 1
        row["expected"] += expected
        row["alerted"] += alerted
        row["fp"] += (not expected) and alerted
        row["fn"] += expected and not alerted
    out: dict[str, dict[str, Any]] = {}
    for kind in sorted(kinds):
        row = kinds[kind]
        negatives = row["n"] - row["expected"]
        out[kind] = {**row, "fp_rate": round(row["fp"] / negatives, 4) if negatives else None}
    return out


def evaluate_heldout(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score the frozen held-out sentences; the label is the row's ``alert`` field."""
    tally = Tally()
    by_kind: list[tuple[str, bool, bool]] = []
    by_category: dict[str, Tally] = defaultdict(Tally)
    for row in rows:
        expected = bool(row["alert"])
        alerted, label = _alerting_hit(row["text"], row["speaker"])
        tally.add(expected, alerted, label)
        by_kind.append((row["kind"], expected, alerted))
        by_category[row.get("category") or "none"].add(expected, alerted)
    return {
        "set": "heldout",
        "detector_version": DETECTOR_VERSION,
        **tally.as_dict(),
        "per_kind": per_kind_table(by_kind),
        "per_category": {c: t.as_dict() for c, t in sorted(by_category.items())},
        "fp_by_hit": dict(tally.reasons_fp.most_common()),
    }


def evaluate_ingrammar(scripts: list[Script]) -> dict[str, Any]:
    """Score every utterance of the eval scripts against the generator gold.

    ``past``-kind utterances are excluded from P/R/F1 and reported separately: spec §9.4 defines
    them as *severity − 1, alert if still ≥ 1*, whereas the generator gold marks every suppressed
    kind as no-alert; both readings are shown instead of picking one silently.
    """
    tally = Tally()
    by_kind: list[tuple[str, bool, bool]] = []
    past = {"n": 0, "alerted": 0, "severity_1": 0}
    sessions_expected = sessions_alerted = 0
    for script in scripts:
        expected_any = alerted_any = False
        for utt in script.utterances:
            kind = utt.gold.risk.kind if utt.gold.risk is not None else "none"
            alerted, label = _alerting_hit(utt.text, utt.speaker)
            if kind == PAST_KIND:
                past["n"] += 1
                past["alerted"] += alerted
                past["severity_1"] += label is not None and label.endswith(":1") and alerted
                continue
            expected = alert_expected(utt)
            tally.add(expected, alerted, label)
            by_kind.append((kind, expected, alerted))
            expected_any |= expected
            alerted_any |= alerted
        sessions_expected += expected_any
        sessions_alerted += expected_any and alerted_any
    return {
        "set": "ingrammar",
        "detector_version": DETECTOR_VERSION,
        "n_scripts": len(scripts),
        **tally.as_dict(),
        "per_kind": per_kind_table(by_kind),
        "past_kind": {
            **past,
            "note": "§9.4: severity−1 이면서 ≥1 이면 경보 — 생성기 골드(alert=false)와 다른 해석; P/R 에서 제외",
        },
        "sessions_with_expected_alert": sessions_expected,
        "sessions_alerted": sessions_alerted,
        "fp_by_hit": dict(tally.reasons_fp.most_common()),
    }
