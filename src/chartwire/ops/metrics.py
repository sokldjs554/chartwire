"""Every Prometheus metric name used in the spec, on one private registry (§6.6, §7.3, §9.6, §8.4).

Other packages import the collector objects from here and never create their own, so the name set
below is the complete `/metrics` vocabulary — ``ALL_NAMES`` is asserted by a unit test and by the
failure-modes table in ``docs/ops/failure-modes.md``.

Conventions: durations are seconds, counters end in ``_total``, gauges describe *this process*.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.exposition import CONTENT_TYPE_LATEST

REGISTRY: Final = CollectorRegistry(auto_describe=True)
"""Private registry: nothing from the default process registry (python_gc_*, …) is exposed."""

disable_created_metrics()  # type: ignore[no-untyped-call]
"""No ``*_created`` companion series: they double the counter/histogram cardinality for no query value."""

LATENCY_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
E2E_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
JOB_BUCKETS: Final = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
ROW_BUCKETS: Final = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)
CREDIT_BUCKETS: Final = (0.0, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0)
RATIO_BUCKETS: Final = tuple(i / 10 for i in range(11))

# --- ws/ledger.py (§6.6) ---------------------------------------------------------------------------
LEDGER_FLUSH_SECONDS = Histogram(
    "ledger_flush_seconds",
    "One LedgerBatcher flush (all tenants) wall time",
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
LEDGER_FLUSH_ROWS = Histogram(
    "ledger_flush_rows", "Rows per LedgerBatcher flush", buckets=ROW_BUCKETS, registry=REGISTRY
)
LEDGER_PENDING_ROWS = Gauge("ledger_pending_rows", "Rows submitted but not yet committed", registry=REGISTRY)

# --- ws/ ingest + watch (§6.1, §6.4, §6.7) ---------------------------------------------------------
WS_CONNECTIONS = Gauge("ws_connections", "Open WebSocket connections", ["kind"], registry=REGISTRY)
WS_CHUNKS_TOTAL = Counter(
    "ws_chunks_total",
    "Audio chunks by outcome (stored|duplicate|reordered|stale|rejected)",
    ["result"],
    registry=REGISTRY,
)
WS_ACK_LATENCY_SECONDS = Histogram(
    "ws_ack_latency_seconds",
    "Chunk receipt to cumulative ack (durable in PostgreSQL)",
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
WS_CREDIT = Histogram(
    "ws_credit", "Credit value sent with each ack", buckets=CREDIT_BUCKETS, registry=REGISTRY
)
WS_RESUME_TOTAL = Counter(
    "ws_resume_total", "hello{resume} outcomes (ok|gap_unrecoverable)", ["result"], registry=REGISTRY
)
WS_DROPPED_PARTIALS_TOTAL = Counter(
    "ws_dropped_partials_total", "transcript.partial messages dropped for slow viewers", registry=REGISTRY
)

# --- stt-worker / risk (§7.4, §9.6) ------------------------------------------------------------------
STT_LAG_CHUNKS = Gauge(
    "stt_lag_chunks", "Chunks waiting for STT across sessions owned by this worker", registry=REGISTRY
)
SEGMENT_E2E_SECONDS = Histogram(
    "segment_e2e_seconds", "Chunk ack to transcript.final commit", buckets=E2E_BUCKETS, registry=REGISTRY
)
RISK_ALERT_LATENCY_SECONDS = Histogram(
    "risk_alert_latency_seconds",
    "Final commit to risk.alert publish",
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
RISK_ALERT_PUBLISH_SECONDS = Histogram(
    "risk_alert_publish_seconds",
    "PUBLISH round-trip for risk.alert",
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
RISK_HITS_TOTAL = Counter(
    "risk_hits_total",
    "Lexicon hits by category/severity/suppressed",
    ["category", "severity", "suppressed"],
    registry=REGISTRY,
)
RISK_UNACKED_OVER_SLA = Gauge(
    "risk_unacked_over_sla", "Open risk events past their SLA deadline", registry=REGISTRY
)

# --- outbox / worker (§7.1, §7.3) ------------------------------------------------------------------
OUTBOX_PENDING = Gauge("outbox_pending", "Rows with status=pending (sum over tenants)", registry=REGISTRY)
OUTBOX_LAG_SECONDS = Gauge("outbox_lag_seconds", "Age of the oldest pending row", registry=REGISTRY)
OUTBOX_DEAD_TOTAL = Counter("outbox_dead_total", "Rows moved to dead_letters", registry=REGISTRY)
HANDLER_DURATION_SECONDS = Histogram(
    "handler_duration_seconds",
    "Handler execution time",
    ["event_type"],
    buckets=JOB_BUCKETS,
    registry=REGISTRY,
)
HANDLER_FAILURES_TOTAL = Counter(
    "handler_failures_total", "Handler raised", ["event_type"], registry=REGISTRY
)
SEGMENTS_DEFAULT_PARTITION_ROWS = Gauge(
    "segments_default_partition_rows",
    "Rows in transcript_segments_default (partition_ensure ticker; must stay 0)",
    registry=REGISTRY,
)

# --- notes (§9.6) -----------------------------------------------------------------------------------
NOTE_STATUS_TOTAL = Counter("note_status_total", "Draft outcomes by status", ["status"], registry=REGISTRY)
NOTE_COVERAGE = Histogram(
    "note_coverage", "Verifier coverage per draft", buckets=RATIO_BUCKETS, registry=REGISTRY
)
NOTE_VERIFY_REASON_TOTAL = Counter(
    "note_verify_reason_total", "Verifier verdict reasons", ["reason"], registry=REGISTRY
)
NOTE_DRAFT_SECONDS = Histogram(
    "note_draft_seconds", "Provider draft call time", ["provider"], buckets=JOB_BUCKETS, registry=REGISTRY
)

# --- purge / db (§8.4, §4) --------------------------------------------------------------------------
PURGE_DURATION_SECONDS = Histogram(
    "purge_duration_seconds", "purge_run wall time", buckets=JOB_BUCKETS, registry=REGISTRY
)
DB_POOL_IN_USE = Gauge("db_pool_in_use", "Connections checked out of the asyncpg pool", registry=REGISTRY)
STT_REBUILDS_TOTAL = Counter(
    "stt_rebuilds_total", "Ledger rebuilds after a stream gap / lost consumer group", registry=REGISTRY
)

ALL_NAMES: Final[tuple[str, ...]] = (
    "ledger_flush_seconds",
    "ledger_flush_rows",
    "ledger_pending_rows",
    "ws_connections",
    "ws_chunks_total",
    "ws_ack_latency_seconds",
    "ws_credit",
    "ws_resume_total",
    "ws_dropped_partials_total",
    "stt_lag_chunks",
    "segment_e2e_seconds",
    "risk_alert_latency_seconds",
    "risk_alert_publish_seconds",
    "risk_hits_total",
    "risk_unacked_over_sla",
    "outbox_pending",
    "outbox_lag_seconds",
    "outbox_dead_total",
    "handler_duration_seconds",
    "handler_failures_total",
    "segments_default_partition_rows",
    "note_status_total",
    "note_coverage",
    "note_verify_reason_total",
    "note_draft_seconds",
    "purge_duration_seconds",
    "db_pool_in_use",
    "stt_rebuilds_total",
)
"""Every metric name in the spec plus ``segments_default_partition_rows`` (§7.2 names none) and
``stt_rebuilds_total`` (scenario D ``rebuild_count``, §11.2)."""

KNOWN_LABEL_VALUES: Final[tuple[tuple[Counter | Gauge | Histogram, tuple[str, ...]], ...]] = (
    (WS_CONNECTIONS, ("ingest", "watch")),
    (WS_CHUNKS_TOTAL, ("stored", "duplicate", "reordered", "stale", "rejected")),
    (WS_RESUME_TOTAL, ("ok", "gap_unrecoverable")),
    (
        HANDLER_DURATION_SECONDS,
        ("session.transcribed", "consent.revoked", "purge.requested", "purge.completed"),
    ),
    (
        HANDLER_FAILURES_TOTAL,
        ("session.transcribed", "consent.revoked", "purge.requested", "purge.completed"),
    ),
    (NOTE_STATUS_TOTAL, ("verified", "needs_review", "abstained")),
    (
        NOTE_VERIFY_REASON_TOTAL,
        (
            "fabricated_segment",
            "quote_mismatch",
            "numeric_mismatch",
            "entity_mismatch",
            "negation_mismatch",
            "speaker_mismatch",
            "verdict_language",
            "injection_pattern",
        ),
    ),
    (NOTE_DRAFT_SECONDS, ("extractive", "anthropic", "recorded")),
)
"""Closed label sets fixed by the spec (§6.4, §7.2, §9.2, §9.3). Pre-creating them makes every series
exist at 0 from the first scrape, so ``rate()`` / ``increase()`` never see a missing series after a
deploy. ``risk_hits_total`` is left to first observation: ``category × severity × suppressed`` is
data-driven and a zero-filled cube would only add noise."""


def init_known_labels() -> None:
    """Create the zero-valued child series for every closed label set (idempotent; runs at import)."""
    for collector, values in KNOWN_LABEL_VALUES:
        for value in values:
            collector.labels(value)


init_known_labels()


def bind_db_pool(checked_out: Callable[[], int]) -> None:
    """Sample ``db_pool_in_use`` lazily on each scrape (``engine.pool.checkedout`` in Phase 1)."""
    DB_POOL_IN_USE.set_function(checked_out)


def render() -> tuple[bytes, str]:
    """Body and ``Content-Type`` for ``GET /metrics``."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def registered_names() -> frozenset[str]:
    """Metric family names currently on the registry (``_total`` suffix restored for counters)."""
    names: set[str] = set()
    for family in REGISTRY.collect():
        names.add(f"{family.name}_total" if family.type == "counter" else family.name)
    return frozenset(names)
