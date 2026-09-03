"""Metric vocabulary: every spec name exists once, renders, and is isolated from the default registry."""

from __future__ import annotations

import re

import prometheus_client

from chartwire.ops import metrics

SPEC_NAMES = {
    # §6.6
    "ledger_flush_seconds",
    "ledger_flush_rows",
    "ledger_pending_rows",
    # §6.1 / §6.4 / §6.7
    "ws_connections",
    "ws_chunks_total",
    "ws_ack_latency_seconds",
    "ws_credit",
    "ws_resume_total",
    "ws_dropped_partials_total",
    "stt_lag_chunks",
    "segment_e2e_seconds",
    "risk_alert_latency_seconds",
    "risk_unacked_over_sla",
    # §7.3
    "outbox_pending",
    "outbox_lag_seconds",
    "outbox_dead_total",
    "handler_duration_seconds",
    "handler_failures_total",
    # §9.6
    "note_status_total",
    "note_coverage",
    "note_verify_reason_total",
    "note_draft_seconds",
    "risk_hits_total",
    "risk_alert_publish_seconds",
    # §8.4 / §4
    "purge_duration_seconds",
    "db_pool_in_use",
}


def test_all_names_cover_the_spec_exactly_plus_partition_gauge() -> None:
    assert set(metrics.ALL_NAMES) - SPEC_NAMES == {"segments_default_partition_rows"}
    assert SPEC_NAMES <= set(metrics.ALL_NAMES)
    assert len(metrics.ALL_NAMES) == len(set(metrics.ALL_NAMES))


def test_every_name_is_registered_and_rendered() -> None:
    assert metrics.registered_names() == set(metrics.ALL_NAMES)
    body, content_type = metrics.render()
    text = body.decode()
    assert content_type.startswith("text/plain")
    for name in metrics.ALL_NAMES:
        family = name.removesuffix("_total")
        assert re.search(rf"^# TYPE {family} (counter|gauge|histogram)$", text, re.M), name


def test_label_sets_match_spec() -> None:
    assert metrics.WS_CONNECTIONS._labelnames == ("kind",)
    assert metrics.WS_CHUNKS_TOTAL._labelnames == ("result",)
    assert metrics.WS_RESUME_TOTAL._labelnames == ("result",)
    assert metrics.HANDLER_DURATION_SECONDS._labelnames == ("event_type",)
    assert metrics.HANDLER_FAILURES_TOTAL._labelnames == ("event_type",)
    assert metrics.NOTE_STATUS_TOTAL._labelnames == ("status",)
    assert metrics.NOTE_VERIFY_REASON_TOTAL._labelnames == ("reason",)
    assert metrics.NOTE_DRAFT_SECONDS._labelnames == ("provider",)
    assert metrics.RISK_HITS_TOTAL._labelnames == ("category", "severity", "suppressed")


def test_counters_and_histograms_observe_into_render() -> None:
    metrics.WS_CHUNKS_TOTAL.labels(result="stale").inc()
    metrics.OUTBOX_DEAD_TOTAL.inc(2)
    metrics.NOTE_COVERAGE.observe(0.85)
    metrics.WS_CONNECTIONS.labels(kind="watch").set(3)
    text = metrics.render()[0].decode()
    assert 'ws_chunks_total{result="stale"} 1.0' in text
    assert "outbox_dead_total 2.0" in text
    assert 'note_coverage_bucket{le="0.9"} 1.0' in text
    assert 'ws_connections{kind="watch"} 3.0' in text


def test_bind_db_pool_samples_lazily() -> None:
    value = {"n": 4}
    metrics.bind_db_pool(lambda: value["n"])
    assert "db_pool_in_use 4.0" in metrics.render()[0].decode()
    value["n"] = 9
    assert "db_pool_in_use 9.0" in metrics.render()[0].decode()


def test_private_registry_excludes_process_collectors() -> None:
    assert metrics.REGISTRY is not prometheus_client.REGISTRY
    text = metrics.render()[0].decode()
    assert "python_gc" not in text and "process_cpu" not in text
