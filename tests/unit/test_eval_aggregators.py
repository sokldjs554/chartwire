"""Aggregator reports (purge / rls / protocol) and the perf-study pure helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartwire.eval import protocol_eval, purge_eval, rls_eval
from chartwire.perf import queries, study


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
