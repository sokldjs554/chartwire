"""``alert_sla`` ticker (§7.2): every second, escalate open alerts whose SLA deadline passed and, every
few seconds, refresh ``risk_unacked_over_sla`` from PostgreSQL (the ZSET is a cache; the gauge is truth).

WP-G's worker attaches ``tick(ctx)`` at ``INTERVAL_S``; the escalation itself lives in
:mod:`chartwire.risk.alerts` so the REST layer and tests share one implementation.
"""

from __future__ import annotations

from typing import Any

from chartwire.risk import alerts

INTERVAL_S = 1.0
GAUGE_EVERY_S = 10.0
"""Cross-tenant count for the gauge is one query per tenant; sampled, not per tick."""

_last_gauge_at: float | None = None


async def tick(ctx: Any) -> list[int]:
    """One pass; returns the risk event ids escalated this tick."""
    global _last_gauge_at
    now = ctx.clock.now()
    escalated = await alerts.escalate_due(ctx, now)
    mono = ctx.clock.monotonic()
    if _last_gauge_at is None or mono - _last_gauge_at >= GAUGE_EVERY_S:
        _last_gauge_at = mono
        await alerts.over_sla_count(ctx, now)
    return escalated
