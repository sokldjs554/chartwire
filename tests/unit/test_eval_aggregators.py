"""Aggregator reports (purge / rls / protocol) and the perf-study pure helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartwire.eval import protocol_eval, purge_eval, rls_eval
from chartwire.perf import queries, study
from chartwire.purge import receipt


def test_purge_and_rls_collect_validate_their_inputs(tmp_path: Path) -> None:
    with pytest.raises(purge_eval.MissingMeasurement):
        purge_eval.collect(tmp_path / "purge.json")
    src = tmp_path / "purge.json"
    src.write_text(json.dumps({"residual_rows": 0}), encoding="utf-8")
    with pytest.raises(ValueError, match="필수 필드"):
        purge_eval.collect(src)
    full = {k: 0 for k in purge_eval.REQUIRED} | {"sessions_purged": 50}
    src.write_text(json.dumps(full), encoding="utf-8")
    assert purge_eval.collect(src) == {"source": str(src), **full}
    rls = tmp_path / "rls.json"
    rls.write_text(json.dumps({"attempts": 370, "leaks": 0}), encoding="utf-8")
    assert rls_eval.collect(rls)["attempts"] == 370


def test_protocol_statistics_parsing_and_chaos_report(tmp_path: Path) -> None:
    output = """
tests/ws/test_ingest_core_props.py::test_no_loss:

  - during generate phase (1.02 seconds):
    - Typical runtimes: ~ 1-2 ms, of which ~ 1 ms in data generation
    - 1000 passing examples, 0 failing examples, 12 invalid examples

tests/ws/test_watch_core_props.py::test_gap_fill:

  - during reuse phase (0.01 seconds):
    - 2 passing examples, 0 failing examples, 0 invalid examples
  - during generate phase (0.40 seconds):
    - 498 passing examples, 0 failing examples, 0 invalid examples
"""
    counts = protocol_eval.parse_statistics(output)
    assert counts == {
        "tests/ws/test_ingest_core_props.py::test_no_loss": 1000,
        "tests/ws/test_watch_core_props.py::test_gap_fill": 500,
    }
    assert protocol_eval.chaos_counts(tmp_path / "D.json") is None
    d = tmp_path / "D.json"
    d.write_text(json.dumps({"loss": 0, "dup": 0}), encoding="utf-8")
    assert protocol_eval.chaos_counts(d) == {"chaos_loss": 0, "chaos_dup": 0, "chaos_source": str(d)}
    assert protocol_eval.collect(run_tests=False, chaos_report=tmp_path / "none.json") is None
    assert protocol_eval.collect(run_tests=False, chaos_report=d)["chaos_loss"] == 0


def test_perf_query_selection_and_plan_summary() -> None:
    assert [q.id for q in queries.select("q2")] == ["Q2a", "Q2b", "Q2c", "Q2d_text", "Q2d_term"]
    assert [q.id for q in queries.select("Q1,q6")] == ["Q1", "Q6"]
    assert [q.id for q in queries.select("q1a_with")] == ["Q1a_with"]
    assert len(queries.select(None)) == len(queries.QUERIES) and len({q.id for q in queries.QUERIES}) == len(
        queries.QUERIES
    )
    assert {q.role for q in queries.QUERIES} == {"app", "owner", "superuser"}
    plan = {
        "Planning Time": 0.2,
        "Execution Time": 1.5,
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": 50,
            "Shared Hit Blocks": 10,
            "Plans": [
                {
                    "Node Type": "Append",
                    "Subplans Removed": 23,
                    "Plans": [
                        {"Node Type": "Index Scan", "Index Name": "ix_a", "Relation Name": "p1"},
                        {"Node Type": "Index Scan", "Index Name": "ix_a", "Relation Name": "p2"},
                    ],
                }
            ],
        },
    }
    summary = study.plan_summary(plan)
    assert summary["top_node"] == "Limit" and summary["node_types"] == ["Limit", "Append", "Index Scan"]
    assert summary["indexes"] == ["ix_a"] and summary["relations_scanned"] == 2
    assert (
        summary["subplans_removed"] == 23 and summary["execution_ms"] == 1.5 and summary["actual_rows"] == 50
    )
    assert study.detect_state("0006_rls") == "before" and study.detect_state("0007_perf") == "after"
    assert study.detect_state("0003_segments") is None and study.detect_state(None) is None
    # 0007 뒤에 붙는 마이그레이션은 인덱스와 무관해도 여전히 "after" 다 — 여기가 슬러그 완전일치로
    # 굳어 있으면 head 가 올라갈 때마다 perf study 가 상태를 못 읽고 조용히 멈춘다.
    assert study.detect_state("0008_consultations") == "after"
    assert study.detect_state("0042_anything") == "after"
    assert study.detect_state("head") is None


# ------------------------------------------------ quality pass 2: the executing purge / rls runs


def test_rls_path_substitution_pins_real_ids_per_collection():
    """``/v1/patients/{id}/consents`` takes the *patient* id; a non-id parameter is left alone."""

    class _Route:
        def __init__(self, path: str, params: tuple[str, ...]) -> None:
            self.path = path
            self.param_convertors = dict.fromkeys(params)
            self.endpoint = lambda: None
            self.methods = {"GET"}

    subjects = {"patient": "P", "session": "S", "note": "N", "alert": "7"}
    for template, params, expected in [
        ("/v1/patients/{id}/consents", ("id",), "/v1/patients/P/consents"),
        ("/v1/sessions/{id}/segments", ("id",), "/v1/sessions/S/segments"),
        ("/v1/notes/{id}", ("id",), "/v1/notes/N"),
        ("/v1/alerts/{id}/ack", ("id",), "/v1/alerts/7/ack"),
    ]:
        path, pinned = rls_eval._concrete(_Route(template, params), subjects)
        assert (path, pinned) == (expected, 1), template
    # a collection with no seeded row is not pinned → no cross-tenant attempt is made
    _, pinned = rls_eval._concrete(_Route("/v1/purge-jobs/{id}", ("id",)), subjects)
    assert pinned == 0


def test_rls_tally_caps_the_violation_list():
    tally = rls_eval.Tally()
    for i in range(60):
        tally.leak(f"leak-{i}")
    assert tally.leaks == 60 and len(tally.violations) == 50


def test_purge_receipt_validity_requires_verified_and_matching_hash():
    class _Job:
        def __init__(self, state, receipt_hash):
            self.state = state
            self.receipt_hash = receipt_hash
            self.steps = [{"step": "capture", "counts": {}}]
            self.counts = {"objects": 1}
            self.dek_fingerprints = ["a" * 64]

    good = receipt.receipt_hash([{"step": "capture", "counts": {}}], {"objects": 1}, ["a" * 64])
    assert purge_eval._receipt_valid(_Job("verified", good))
    assert not purge_eval._receipt_valid(_Job("completed", good))
    assert not purge_eval._receipt_valid(_Job("verified", None))
    assert not purge_eval._receipt_valid(_Job("verified", b"\x00" * 32))


def test_purge_percentage_helper_is_zero_for_an_empty_run():
    assert purge_eval._pct(3, 4) == 75.0
    assert purge_eval._pct(0, 0) == 0.0


def test_protocol_parses_both_hypothesis_statistics_wordings():
    """Hypothesis ≥ 6.9x prints "N passing, M failing, and K invalid test cases"."""
    output = (
        "tests/ws/test_ingest_core_props.py::test_no_loss:\n"
        "  - during generate phase (1.02 seconds):\n"
        "    - 250 passing examples, 0 failing examples, 3 invalid examples\n"
        "tests/ws/test_watch_core_props.py::TestWatchCoreStateMachine::runTest:\n"
        "  - during generate phase (13.17 seconds):\n"
        "    - 1000 passing, 0 failing, and 207 invalid test cases\n"
    )
    counts = protocol_eval.parse_statistics(output)
    assert counts == {
        "tests/ws/test_ingest_core_props.py::test_no_loss": 250,
        "tests/ws/test_watch_core_props.py::TestWatchCoreStateMachine::runTest": 1000,
    }
