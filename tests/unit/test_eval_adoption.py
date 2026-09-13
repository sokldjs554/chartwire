"""Adoption harness: the rule engine, the committed ``adoption.json``, and the shipped-code invariant.

The invariant is the reason the harness exists — a row may not adopt what the code does not run, nor
reject what it does — so the committed report is re-decided here from its own stored numbers.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from chartwire.eval import adoption_eval, corpus
from chartwire.notes.extractive import DEFAULT_SELECTION
from chartwire.risk.detector import DEFAULT_POLICY

REPORT = Path(__file__).resolve().parents[2] / "docs" / "eval" / "adoption.json"


def test_risk_rule_needs_the_delta_and_every_guard() -> None:
    rule = adoption_eval.RISK_RULE
    base = {"precision": 0.70, "recall": 0.60, "category_recall": {"a": 0.5, "b": 0.6}}
    better = {"precision": 0.73, "recall": 0.60, "category_recall": {"a": 0.5, "b": 0.6}}
    assert rule.decide(base, better)[0] == "adopt"
    # +0.01 is inside the noise band on 300 sentences
    assert rule.decide(base, {**better, "precision": 0.71})[0] == "reject"
    # total recall held, one category lost: thinner net, whatever the headline says
    decision, checks = rule.decide(base, {**better, "category_recall": {"a": 0.5, "b": 0.55}})
    assert decision == "reject"
    assert [c["name"] for c in checks if not c["ok"]] == ["category_recall each_not_lower"]
    assert "b 0.6000→0.5500" in checks[-1]["detail"]


def test_notes_rule_decides_on_macro_recall_not_the_micro_total() -> None:
    rule = adoption_eval.NOTES_RULE
    base = {
        "fact_recall_macro": 0.70,
        "fact_recall": 0.60,
        "coverage": 1.0,
        "abstain_rate": 0.0,
        "fact_types_at_zero": 4,
    }
    cand = {
        "fact_recall_macro": 0.80,
        "fact_recall": 0.58,
        "coverage": 1.0,
        "abstain_rate": 0.0,
        "fact_types_at_zero": 0,
    }
    # the micro total fell and the macro rose — exactly the duration-dominated case the rule is written for
    assert rule.decide(base, cand)[0] == "adopt"
    assert rule.decide(base, {**cand, "fact_types_at_zero": 5})[0] == "reject"
    assert rule.decide(base, {**cand, "abstain_rate": 0.01})[0] == "reject"


def test_candidates_are_the_declared_fixed_list() -> None:
    assert [c.id for c in adoption_eval.CANDIDATES] == [
        "risk.past_split",
        "risk.past_suppress_all",
        "risk.severity_floor_2",
        "notes.selection",
        "notes.family_first_alone",
        "notes.anthropic_provider",
    ]
    for cand in adoption_eval.CANDIDATES:
        if cand.measured:
            assert cand.baseline != cand.candidate, cand.id
        else:
            assert cand.not_measured_because and cand.shipped() == "baseline"


def test_committed_report_follows_from_its_own_numbers_and_agrees_with_the_code() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["shipped"]["risk"] == {"detector_version": "lex-1", **vars(DEFAULT_POLICY)}
    assert report["shipped"]["notes"] == vars(DEFAULT_SELECTION)
    assert {r["id"] for r in report["candidates"]} == {c.id for c in adoption_eval.CANDIDATES}
    for row in report["candidates"]:
        assert adoption_eval.redecide(row) == row["decision"], row["id"]
        assert adoption_eval.shipped_agrees(row), row["id"]
    assert dict(Counter(r["decision"] for r in report["candidates"])) == report["counts"]
    assert report["decision_sets"]["risk"]["frozen_sha256"] == corpus.sha256_file(corpus.HELDOUT_FILE)
    assert report["rules"]["risk"]["primary"] == "precision"
    assert report["rules"]["notes"]["primary"] == "fact_recall_macro"


def test_evaluate_has_the_report_shape_on_a_small_corpus() -> None:
    body = adoption_eval.evaluate(corpus.load_heldout(), corpus.eval_scripts(7, 10), seed=7)
    assert set(body) == {"rules", "decision_sets", "shipped", "candidates", "counts"}
    assert sum(body["counts"].values()) == len(adoption_eval.CANDIDATES)
    for row in body["candidates"]:
        assert row["decision"] in ("adopt", "reject", "not_measured")
        # the held-out set does not shrink with --n, so the risk rows decide the same as the full report
        if row["family"] == "risk":
            assert adoption_eval.shipped_agrees(row), row["id"]
            assert row["baseline"]["category_recall"].keys() == row["candidate"]["category_recall"].keys()
