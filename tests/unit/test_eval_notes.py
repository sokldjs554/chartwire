"""Grounding, mutation-detection, paraphrase and injection evaluations on a 20-script corpus."""

from __future__ import annotations

import pytest

from chartwire.eval import corpus, grounding_eval, inject_eval, paraphrase_eval
from chartwire.notes.providers.mutation import MUTATION_CLASSES


@pytest.fixture(scope="module")
def scripts():
    return corpus.eval_scripts(42, 20)


def test_grounding_report(scripts) -> None:
    report = grounding_eval.evaluate(scripts)
    assert report["n_sessions"] == 20 and report["coverage"] == 1.0  # extractive: by construction
    assert 0.0 <= report["fact_recall"] <= 1.0 and report["facts_recalled"] <= report["facts_total"]
    assert sum(v["total"] for v in report["fact_recall_by_type"].values()) == report["facts_total"]
    assert set(report["status_counts"]) <= {"verified", "needs_review", "abstained"}
    assert report["statements_total"] == sum(report["section_counts"].values())
    assert report["verifier_version"] == "rules-8.v1"


def test_mutation_report_covers_every_class_with_short_names(scripts) -> None:
    report = inject_eval.evaluate_mutations(scripts, per_class=10, seed=1)
    assert [c["mutation"] for c in report["classes"]] == list(MUTATION_CLASSES)
    assert {c["class"] for c in report["classes"]} == set(inject_eval.SHORT_NAME.values())
    for c in report["classes"]:
        assert c["n"] == 10 and c["detected"] <= 10 and sum(c["reasons"].values()) == 10
        assert c["detection_rate"] == c["detected"] / 10
    assert 0.0 <= report["false_flag_rate"] <= 1.0 and report["clean_statements"] > 0
    again = inject_eval.evaluate_mutations(scripts, per_class=10, seed=1)
    assert again["classes"] == report["classes"]  # seed-deterministic


def test_paraphrase_report_by_transform(scripts) -> None:
    report = paraphrase_eval.evaluate(scripts, passes=1, seed=3)
    assert report["n_statements"] == sum(r["total"] for r in report["by_transform"])
    assert report["rejected"] == sum(r["rejected"] for r in report["by_transform"])
    assert report["false_rejection_rate"] == round(report["rejected"] / report["n_statements"], 4)
    assert sum(c["count"] for c in report["residual_causes"]) == report["rejected"]
    assert all(":" in c["cause"] for c in report["residual_causes"])


def test_injection_report_counts_leaks_against_the_injected_utterances(scripts) -> None:
    targets = inject_eval.injection_scripts(scripts)
    assert [s.script_ref for s in targets] == ["e0010", "e0020"]
    report = inject_eval.evaluate_injection(scripts)
    assert report["n_sessions"] == 2 and report["script_refs"] == ["e0010", "e0020"]
    assert report["n_injection_utterances"] == sum(len(s.meta.injection_utterances) for s in targets)
    assert report["injection_leaks"] == sum(len(d["utterances"]) for d in report["leak_details"])
    assert report["excluded_by_provider"] + report["injection_leaks"] >= report[
        "n_injection_utterances"
    ] - len(report["leak_details"])
    assert report["forced_citations_flagged"] <= report["forced_citations_total"]
