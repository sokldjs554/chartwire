"""Scenario definitions, chaos schedule, report shaping and ``results.md`` rendering (spec §11.2, §11.4).

Pure Python: no processes, no services. The runner itself is exercised by the smoke run
(``chartwire loadtest A --sessions 10 --duration 20``) and by CI ``load-smoke``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chartwire.loadtest import cli as loadtest_cli
from chartwire.loadtest import report as rpt
from chartwire.loadtest import scenarios as sc
from chartwire.loadtest.client import SessionStats, last_chunk_seq

runner = CliRunner()

# --------------------------------------------------------------------------- scenarios


def test_scenarios_match_spec_table() -> None:
    a, b, c, d = (sc.SCENARIOS[k] for k in "ABCD")
    assert sc.A_SESSION_COUNTS == (50, 100, 200)
    assert a.total_chunks() == 300 and a.viewers_per_session == 1 and not a.chaos
    assert (b.sessions, b.stt_provider, b.stt_slow_delay_ms) == (50, "slow", 400)
    assert (c.sessions, c.slow_viewer_fraction, c.slow_viewer_delay_s) == (100, 0.2, 0.2)
    assert c.slow_viewers(100) == 20 and len(sc.slow_viewer_indexes(c, 100)) == 20
    assert (d.kill_fraction, d.kill_every_s, d.flush_redis_at_s, d.stt_sigstop_at_s, d.stt_sigstop_for_s) == (
        0.1,
        10.0,
        30.0,
        40.0,
        15.0,
    )


def test_chaos_schedule_is_ordered_and_bounded() -> None:
    events = sc.chaos_schedule(sc.SCENARIOS["D"], 60)
    kinds = [(e.at_s, e.kind) for e in events]
    assert kinds == [
        (10.0, "kill_sockets"),
        (20.0, "kill_sockets"),
        (30.0, "flush_redis"),
        (30.0, "kill_sockets"),
        (40.0, "kill_sockets"),
        (40.0, "stt_sigstop"),
        (50.0, "kill_sockets"),
        (55.0, "stt_sigcont"),
    ]
    assert all(e.fraction == 0.1 for e in events if e.kind == "kill_sockets")
    assert sc.chaos_schedule(sc.SCENARIOS["A"], 60) == []
    # a short run keeps only the events that fit
    short = sc.chaos_schedule(sc.SCENARIOS["D"], 25)
    assert [(e.at_s, e.kind) for e in short] == [(10.0, "kill_sockets"), (20.0, "kill_sockets")]


def test_kill_targets_are_distinct_and_at_least_one() -> None:
    ids = [f"s{i}" for i in range(100)]
    picked = sc.pick_kill_targets(random.Random(1), ids, 0.1)
    assert len(picked) == 10 and len(set(picked)) == 10 and set(picked) <= set(ids)
    assert len(sc.pick_kill_targets(random.Random(1), ids[:3], 0.1)) == 1
    assert sc.pick_kill_targets(random.Random(1), ids, 0.0) == []
    assert sc.pick_kill_targets(random.Random(1), ids, 0.1) == sc.pick_kill_targets(
        random.Random(1), ids, 0.1
    )


def test_last_chunk_seq_maps_t_end_to_the_chunk_window() -> None:
    assert last_chunk_seq(0, 200) == 1
    assert last_chunk_seq(199, 200) == 1
    assert last_chunk_seq(200, 200) == 2
    assert last_chunk_seq(4588, 200) == 23
    assert (
        last_chunk_seq(None, 200) is None
        and last_chunk_seq("x", 200) is None
        and last_chunk_seq(5, 0) is None
    )


# --------------------------------------------------------------------------- aggregation


def _session(sid: str, *, sent: int = 300, acks: list[float] | None = None, **kw: object) -> SessionStats:
    s = SessionStats(sid)
    s.sent = sent
    s.ack_seq = sent
    s.final_seq = sent
    s.outcome = "ended"
    s.ack_rtt_ms = acks or [50.0, 60.0, 70.0]
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_aggregate_pools_samples_and_sums_counters() -> None:
    a = _session("a", acks=[10.0, 20.0, 30.0, 40.0], credit_min=50, final_e2e_ms=[100.0], alert_e2e_ms=[5.0])
    b = _session("b", acks=[1000.0], credit_min=0, credit_zero_at_s=12.5, pauses=2, final_dups=1)
    b.reconnects, b.resumes_ok = 2, 2
    b.close_codes[4409] += 1
    agg = rpt.aggregate([a, b], duration_s=60, ramp_s=2.0)
    assert agg["sessions"] == 2 and agg["chunks_sent"] == 600 and agg["chunks_per_s"] == 10.0
    assert (
        agg["ack_samples"] == 5 and agg["ack_p50_ms"] == 20.0 and agg["ack_p99_ms"] == 1000.0
    )  # nearest rank
    assert agg["final_e2e_p95_ms"] == 100.0 and agg["alert_e2e_p95_ms"] == 5.0 and agg["alert_sessions"] == 1
    assert agg["credit_min"] == 0 and agg["credit_zero_at_s"] == 12.5 and agg["credit_zero_sessions"] == 1
    assert agg["pause_count"] == 2 and agg["final_dups"] == 1 and agg["superseded_closes"] == 1
    assert agg["resume_success_pct"] == 100.0 and agg["outcomes"] == {"ended": 2}


def test_aggregate_without_samples_yields_nulls_not_zeros() -> None:
    s = SessionStats("empty")
    agg = rpt.aggregate([s], duration_s=10)
    assert agg["ack_p95_ms"] is None and agg["alert_e2e_p95_ms"] is None and agg["credit_min"] is None
    assert agg["resume_success_pct"] is None and agg["chunks_per_s"] == 0.0


def test_credit_zero_is_recorded_once_with_elapsed_time() -> None:
    s = SessionStats("x")
    s.observe_credit(50, elapsed_s=1.0)
    s.observe_credit(0, elapsed_s=7.5)
    s.observe_credit(0, elapsed_s=9.0)
    assert s.credit_min == 0 and s.credit_zero_at_s == 7.5
    assert s.summary()["credit_zero_at_s"] == 7.5


# --------------------------------------------------------------------------- resources


def test_rss_slope_is_least_squares_in_mb_per_min() -> None:
    samples = [rpt.ResourceSample(t, 10.0, 100.0 + t * 0.5) for t in range(0, 60, 5)]  # +0.5 MB/s
    assert rpt.rss_slope_mb_per_min(samples) == pytest.approx(30.0)
    assert rpt.rss_slope_mb_per_min(samples[:1]) is None
    flat = [rpt.ResourceSample(t, 1.0, 100.0) for t in range(5)]
    assert rpt.rss_slope_mb_per_min(flat) == 0.0
    summary = rpt.summarize_resources({"api": samples, "none": []})
    assert summary["api"]["rss_mb_max"] == pytest.approx(127.5) and summary["api"]["cpu_pct_max"] == 10.0
    assert summary["none"] == {"samples": 0}


# --------------------------------------------------------------------------- bodies / KEYS shapes


def _agg() -> dict[str, object]:
    a = _session("a", acks=[80.0, 90.0, 120.0], credit_min=50, final_e2e_ms=[300.0], alert_e2e_ms=[20.0])
    return rpt.aggregate([a], duration_s=60)


def test_a_run_has_the_readme_keys_and_db_loss_wins() -> None:
    db = rpt.DbCheck(sessions=1, chunk_rows=298, chunks_sent=300, sessions_ended=1)
    run = rpt.a_run(50, _agg(), {"api": {"rss_slope_mb_per_min": 0.1}}, db)
    for key in (
        "n",
        "ack_p50_ms",
        "ack_p95_ms",
        "ack_p99_ms",
        "final_e2e_p95_ms",
        "alert_e2e_p95_ms",
        "chunks_per_s",
        "credit_min",
        "loss",
        "dup",
    ):
        assert key in run
    assert run["n"] == 50 and run["loss"] == 2 and run["dup"] == 0 and run["caption"] == rpt.LOAD_CAPTION


def test_merge_a_runs_replaces_same_n_and_sorts() -> None:
    existing = {"runs": [{"n": 200, "ack_p95_ms": 1.0}, {"n": 50, "ack_p95_ms": 9.0}]}
    merged = rpt.merge_a_runs(existing, {"n": 50, "ack_p95_ms": 2.0})
    assert [r["n"] for r in merged] == [50, 200] and merged[0]["ack_p95_ms"] == 2.0
    assert rpt.merge_a_runs(None, {"n": 100}) == [{"n": 100}]


def test_b_c_d_bodies_expose_the_registered_keys() -> None:
    agg = _agg()
    b = rpt.b_body(
        agg, {"api": {"rss_slope_mb_per_min": 0.25}}, None, stream_len_max=1500, stream_maxlen=2000
    )
    assert set(b) >= {"credit_zero_at_s", "pause_count", "stream_len_max", "api_rss_slope_mb_per_min", "loss"}
    assert b["api_rss_slope_mb_per_min"] == 0.25 and b["stream_bounded"] is True
    c = rpt.c_body(agg, {}, None, reference_ack_p95_ms=100.0, dropped_partials=3, slow_viewers=20)
    assert c["ack_p95_ms"] == 120.0 and c["ack_p95_delta_pct"] == 20.0 and c["dropped_partials"] == 3
    assert (
        rpt.c_body(agg, {}, None, reference_ack_p95_ms=None, dropped_partials=None, slow_viewers=0)[
            "ack_p95_delta_pct"
        ]
        is None
    )
    db = rpt.DbCheck(
        sessions=4,
        chunk_rows=1200,
        chunks_sent=1200,
        stt_offsets_complete=4,
        segments_contiguous=3,
        sessions_ended=4,
    )
    d = rpt.d_body(agg, {}, db, rebuild_count=2, chaos_log=[{"kind": "flush_redis", "at_s": 30.0}])
    assert set(d) >= {"resume_success_pct", "superseded_closes", "rebuild_count", "loss", "dup"}
    assert d["invariants"] == {
        "stt_offsets_complete_pct": 100.0,
        "segments_contiguous_pct": 75.0,
        "sessions_ended_pct": 100.0,
    }
    assert d["chaos"][0]["kind"] == "flush_redis"


def test_alert_latency_body_needs_samples() -> None:
    body = rpt.alert_latency_body(_agg(), n_run=50)
    assert body is not None and body["p95_ms"] == 20.0 and body["n_sessions"] == 1 and body["n_run"] == 50
    assert rpt.alert_latency_body(rpt.aggregate([SessionStats("x")], duration_s=1), n_run=50) is None


def test_reports_resolve_through_readme_numbers_registry(tmp_path: Path) -> None:
    """The shapes above must satisfy WP-F's ``KEYS`` — resolve every ``load.*`` key from written files."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("readme_numbers", Path("scripts/readme_numbers.py"))
    assert spec is not None and spec.loader is not None
    rn = importlib.util.module_from_spec(spec)
    sys.modules["readme_numbers"] = rn  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(rn)
    out = tmp_path / "docs" / "loadtest"
    agg = _agg()
    a_body = {"runs": [rpt.a_run(n, agg, {}, rpt.DbCheck(1, 300, 300)) for n in (50, 100, 200)]}
    rpt.write_json(out / "A.json", rpt.finish(42, a_body, pg_version=None))
    rpt.write_json(
        out / "B.json",
        rpt.finish(
            42,
            rpt.b_body(
                agg, {"api": {"rss_slope_mb_per_min": 0.0}}, None, stream_len_max=1, stream_maxlen=2000
            ),
            pg_version=None,
        ),
    )
    rpt.write_json(
        out / "C.json",
        rpt.finish(
            42,
            rpt.c_body(agg, {}, None, reference_ack_p95_ms=100.0, dropped_partials=0, slow_viewers=20),
            pg_version=None,
        ),
    )
    rpt.write_json(
        out / "D.json",
        rpt.finish(
            42, rpt.d_body(agg, {}, rpt.DbCheck(1, 300, 300), rebuild_count=0, chaos_log=[]), pg_version=None
        ),
    )
    rpt.write_json(
        tmp_path / "docs" / "eval" / "alert_latency.json",
        rpt.finish(42, rpt.alert_latency_body(agg, n_run=50) or {}, pg_version=None),
    )
    values = rn.load_values(tmp_path)
    load_keys = [
        k for k in rn.KEYS if k.startswith(("load.A", "load.B", "load.C", "load.D", "eval.alert_latency"))
    ]
    unresolved = [k for k in load_keys if values[k] is None]
    # legitimately null in this fixture: credit never hit 0 (B) and nothing reconnected (D) — the README
    # rows for those two keys are deleted instead of guessed; every other key resolves
    assert unresolved == ["load.B.credit_zero_at_s", "load.D.resume_success_pct"], unresolved
    assert values["load.A.n200.ack_p95_ms"] == "120" and values["load.D.loss"] == "0"


# --------------------------------------------------------------------------- results.md + cli


def test_results_md_renders_from_json_only(tmp_path: Path) -> None:
    md = rpt.render_results_md({})
    assert "미측정" in md and rpt.LOAD_CAPTION in md
    a = rpt.finish(
        42,
        {
            "runs": [
                rpt.a_run(
                    50,
                    _agg(),
                    {
                        "api": {
                            "cpu_pct_mean": 1.0,
                            "cpu_pct_max": 2.0,
                            "rss_mb_max": 3.0,
                            "rss_slope_mb_per_min": 0.0,
                        }
                    },
                    None,
                )
            ]
        },
        pg_version="16.13",
    )
    h = rpt.finish(
        42,
        {
            "events": 2000,
            "workers": 2,
            "tenants": 5,
            "events_per_s": 185.1,
            "dlq_count": 0,
            "reclaimed": 200,
            "worker_killed": True,
            "claim_ms_p50": 4.5,
            "claim_ms_p95": 7.2,
        },
        pg_version="16.13",
    )
    md = rpt.render_results_md({"A": a, "H": h})
    assert "| 50 | 5 | 90 | 120 | 120 | 300 | 20 | 50 | 0 | 0 |" in md
    assert "| events/s | 185.1 |" in md and "PG 16.13" in md
    rpt.write_json(tmp_path / "A.json", a)
    assert set(rpt.load_reports(tmp_path)) == {"A"}


def test_cli_rejects_unknown_scenario_and_lists_choices() -> None:
    result = runner.invoke(loadtest_cli.app, ["Z"])
    assert result.exit_code == 2 and "A, B, C, D, H, results" in result.output


def test_cli_results_renders_without_services(tmp_path: Path) -> None:
    result = runner.invoke(loadtest_cli.app, ["results", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "results.md").read_text(encoding="utf-8").startswith("# 부하 테스트 결과")


def test_cli_summary_picks_headline_fields() -> None:
    summary = loadtest_cli.summary_of("B", {"credit_zero_at_s": 3.2, "pause_count": 4, "loss": 0, "extra": 1})
    assert summary == {
        "scenario": "B",
        "credit_zero_at_s": 3.2,
        "pause_count": 4,
        "stream_len_max": None,
        "api_rss_slope_mb_per_min": None,
        "loss": 0,
    }
    assert (
        json.loads(json.dumps(loadtest_cli.summary_of("A", {"runs": [{"n": 50, "loss": 0}]})))["runs"][0]["n"]
        == 50
    )


def test_root_cli_mounts_loadtest() -> None:
    from chartwire.cli import MOUNTED

    assert "loadtest" in MOUNTED and "simulate" in MOUNTED
