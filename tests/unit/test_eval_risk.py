"""Risk evaluation arithmetic and report layout (held-out + in-grammar)."""

from __future__ import annotations

from chartwire.eval import corpus, risk_eval


def test_tally_precision_recall_f1() -> None:
    t = risk_eval.Tally()
    for expected, alerted in [(True, True), (True, True), (True, False), (False, True), (False, False)]:
        t.add(expected, alerted, "suicidal_ideation:2")
    assert (t.tp, t.fp, t.fn, t.tn, t.n) == (2, 1, 1, 1, 5)
    assert t.precision == 2 / 3 and t.recall == 2 / 3 and abs(t.f1 - 2 / 3) < 1e-9
    assert t.reasons_fp == {"suicidal_ideation:2": 1}
    assert risk_eval.Tally().as_dict()["f1"] == 0.0


def test_per_kind_table_counts_fp_against_negatives_only() -> None:
    table = risk_eval.per_kind_table(
        [("idiom", False, True), ("idiom", False, False), ("positive", True, True), ("positive", True, False)]
    )
    assert table["idiom"] == {"n": 2, "expected": 0, "alerted": 1, "fp": 1, "fn": 0, "fp_rate": 0.5}
    assert table["positive"]["fn"] == 1 and table["positive"]["fp_rate"] is None


def test_heldout_report_shape_on_a_hand_made_subset() -> None:
    rows = [
        {
            "text": "약을 모아두고 있어요",
            "speaker": "patient",
            "category": "suicidal_ideation",
            "alert": True,
            "kind": "positive",
        },
        {
            "text": "죽고 싶다는 생각까지는 없어요",
            "speaker": "patient",
            "category": "suicidal_ideation",
            "alert": False,
            "kind": "negated",
        },
        {
            "text": "오늘 날씨가 좋네요",
            "speaker": "patient",
            "category": None,
            "alert": False,
            "kind": "unrelated",
        },
    ]
    report = risk_eval.evaluate_heldout(rows)
    assert report["set"] == "heldout" and report["detector_version"] == "lex-1"
    assert report["n"] == 3 and report["tp"] == 1 and report["fn"] == 0
    assert set(report["per_kind"]) == {"positive", "negated", "unrelated"}
    assert "none" in report["per_category"] and "suicidal_ideation" in report["per_category"]


def test_ingrammar_excludes_past_kind_and_reports_it_separately() -> None:
    scripts = corpus.eval_scripts(42, 20)
    report = risk_eval.evaluate_ingrammar(scripts)
    assert report["n_scripts"] == 20 and report["n"] + report["past_kind"]["n"] == sum(
        len(s.utterances) for s in scripts
    )
    assert "past" not in report["per_kind"] and "positive" in report["per_kind"]
    assert report["sessions_alerted"] <= report["sessions_with_expected_alert"] <= 20
    assert 0.0 <= report["recall"] <= 1.0 and report["past_kind"]["alerted"] <= report["past_kind"]["n"]
    assert "§9.4" in report["past_kind"]["note"]
