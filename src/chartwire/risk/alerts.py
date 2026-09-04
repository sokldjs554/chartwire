"""Risk alerts: the ``risk_events`` lifecycle around the detector (spec §7.2, §7.4, §8.5).

Three moments, three functions:

* **inside the final-segment transaction** — :func:`create_event` inserts the ``risk_events`` row and
  the ``alert.created`` audit row next to the segment, so an alert can never exist without its segment
  (or vice versa);
* **after that transaction commits** — :func:`after_commit` performs the effects that are idempotent
  by construction and must not run for a rolled-back row: ``ZADD alerts:sla`` (severity 3: +60 s,
  severity 2: +300 s, severity 1: none) and ``PUBLISH risk.alert`` carrying ``committed_at``, the
  timestamp the alert-latency measurement starts from (§11.2);
* **later** — :func:`ack` (clinician acknowledged; REST and the viewer socket share it) and
  :func:`escalate_due` (the ``alert_sla`` ticker: one escalation step, then the member is dropped).

Nothing here logs transcript text; alerts carry ``phrase``/``span`` only inside the database row.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.audit import service as audit
from chartwire.db.models import RiskEvent
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops import metrics
from chartwire.outbox.runtime import active_tenant_ids
from chartwire.redis import keys
from chartwire.risk.detector import DETECTOR_VERSION, RiskHit

log = logging.getLogger(__name__)

SLA_SECONDS: Final[Mapping[int, int]] = {3: 60, 2: 300}
"""Acknowledgement deadline per severity (§7.4); severity 1 has no SLA timer."""

OPEN_SCAN_LIMIT: Final = 1_000
"""Upper bound of open alerts inspected per tenant when computing ``risk_unacked_over_sla``."""


def sla_deadline(severity: int, committed_at: datetime) -> datetime | None:
    seconds = SLA_SECONDS.get(severity)
    return None if seconds is None else committed_at + timedelta(seconds=seconds)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def alert_message(event: RiskEvent, *, committed_at: datetime | None = None) -> dict[str, Any]:
    """The ``risk.alert`` payload of §6.3 / ``docs/protocol.md`` §3.4 (``committed_at`` optional)."""
    msg: dict[str, Any] = {
        "t": "risk.alert",
        "risk_event_id": int(event.id),
        "category": event.category,
        "severity": int(event.severity),
        "segment_seq": int(event.segment_seq),
        "span": [int(event.span_start), int(event.span_end)],
    }
    if event.sla_deadline_at is not None:
        msg["sla_deadline_at"] = _iso(event.sla_deadline_at)
    if committed_at is not None:
        msg["committed_at"] = _iso(committed_at)
    return msg


# --- inside the segment transaction ------------------------------------------------------------------


async def create_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    patient_id: UUID,
    segment_id: int,
    segment_created_at: datetime,
    segment_seq: int,
    hit: RiskHit,
    now: datetime,
) -> RiskEvent:
    """Insert the ``risk_events`` row (+ audit ``alert.created``) for an alert-worthy hit.

    Runs in the caller's transaction — the stt-worker's final-segment transaction (§7.4) — so the
    alert commits atomically with its segment. The SLA deadline is fixed here from ``now`` (the
    transaction's clock reading); :func:`after_commit` pushes the same deadline into Redis.
    """
    if not hit.alerts:
        raise ValueError("create_event called for a suppressed / severity-0 hit")
    event = await risk_repo.insert_event(
        session,
        tenant_id=tenant_id,
        session_id=session_id,
        patient_id=patient_id,
        segment_id=segment_id,
        segment_created_at=segment_created_at,
        segment_seq=segment_seq,
        category=hit.category,
        severity=hit.severity,
        phrase=hit.phrase,
        span_start=hit.start,
        span_end=hit.end,
        scope=asdict(hit.scope),
        detector_version=DETECTOR_VERSION,
        detected_at=now,
        sla_deadline_at=sla_deadline(hit.severity, now),
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor_id=None,
        actor_role="service",
        action="alert.created",
        resource_type="risk_event",
        resource_id=str(event.id),
        detail={
            "session_id": str(session_id),
            "segment_seq": segment_seq,
            "category": hit.category,
            "severity": hit.severity,
            "detector_version": DETECTOR_VERSION,
        },
    )
    return event


# --- after commit -------------------------------------------------------------------------------------


async def after_commit(redis: Any, event: RiskEvent, *, committed_at: datetime) -> dict[str, Any]:
    """``ZADD alerts:sla`` (when the severity has an SLA) and ``PUBLISH risk.alert`` on the session
    channel. Both are idempotent: a re-delivery re-ZADDs the same member/score and re-publishes an
    alert the viewer core de-duplicates by ``risk_event_id``. Returns the published message."""
    msg = alert_message(event, committed_at=committed_at)
    started = time.monotonic()
    if event.sla_deadline_at is not None:
        member = keys.alerts_sla_member(event.tenant_id, int(event.id))
        await redis.zadd(keys.ALERTS_SLA, {member: event.sla_deadline_at.timestamp() * 1000.0})
    await redis.publish(keys.sess_events(event.session_id), orjson.dumps(msg).decode())
    now = time.monotonic()
    metrics.RISK_ALERT_PUBLISH_SECONDS.observe(now - started)
    metrics.RISK_ALERT_LATENCY_SECONDS.observe(max(0.0, time.time() - committed_at.timestamp()))
    return msg


# --- acknowledgement ---------------------------------------------------------------------------------


async def acknowledge_in_tx(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    risk_event_id: int,
    by: UUID | None,
    actor_role: str,
    now: datetime | None = None,
    via: str = "rest",
    require_own_session: bool = False,
) -> RiskEvent | None:
    """DB half of an ack in the caller's transaction: idempotent update + audit ``alert.acked``.

    Returns ``None`` when the event is not visible under the caller's tenant context (RLS) — the
    caller answers 404. An already-acknowledged event is returned unchanged and audited again only
    when this call actually acknowledged it.

    ``require_own_session`` applies the "clinician (own)" row rule of ``sessions.clinician_id`` that
    ``GET /v1/alerts`` already applies, inside this transaction so there is no TOCTOU. RLS scopes
    ``risk_events`` to the tenant and nothing further, and ``risk_events.id`` is a guessable bigint
    identity — so without it any clinician could walk the id space and irreversibly acknowledge (and
    read the ``patient_id``/``category`` of) another clinician's patients' suicide-risk alerts, while
    ``ZREM alerts:sla`` silently disarmed the escalation for them.
    """
    before = await risk_repo.get_event(session, risk_event_id)
    if before is None:
        return None
    if require_own_session:
        owner = await sessions_repo.get_session(session, before.session_id)
        if owner is None or by is None or owner.clinician_id != by:
            return None
    if before.acknowledged_at is not None:
        return before
    event = await risk_repo.acknowledge(session, risk_event_id, by=by, now=now or datetime.now(tz=UTC))
    if event is None:
        return None
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor_id=by,
        actor_role=actor_role,
        action="alert.acked",
        resource_type="risk_event",
        resource_id=str(risk_event_id),
        detail={"session_id": str(event.session_id), "via": via},
    )
    return event


async def ack(
    ctx: Any,
    *,
    tenant_id: UUID,
    risk_event_id: int,
    by: UUID | None,
    actor_role: str = "clinician",
    via: str = "rest",
    require_own_session: bool = False,
) -> RiskEvent | None:
    """Acknowledge an alert end to end: tenant transaction (update + audit) under the actor's own
    role, then ``ZREM alerts:sla`` and ``PUBLISH risk.ack{risk_event_id, by}``.

    ``ctx`` needs ``engine``, ``redis`` and ``clock`` (``HandlerContext`` or the api's ``AppDeps``).
    Returns the event, or ``None`` when it is not visible to this tenant/role — including, with
    ``require_own_session``, an alert on another clinician's session.
    """
    async with tenant_tx(ctx.engine, TenantCtx(tenant_id=tenant_id, user_id=by, role=actor_role)) as s:
        event = await acknowledge_in_tx(
            s,
            tenant_id=tenant_id,
            risk_event_id=risk_event_id,
            by=by,
            actor_role=actor_role,
            now=ctx.clock.now(),
            via=via,
            require_own_session=require_own_session,
        )
    if event is None:
        return None
    await after_ack(ctx.redis, event, by=by)
    return event


async def after_ack(redis: Any, event: RiskEvent, *, by: UUID | None) -> None:
    """Redis effects of an acknowledgement (idempotent): drop the SLA member, tell the viewers."""
    await redis.zrem(keys.ALERTS_SLA, keys.alerts_sla_member(event.tenant_id, int(event.id)))
    msg = {"t": "risk.ack", "risk_event_id": int(event.id), "by": str(by or NOBODY)}
    await redis.publish(keys.sess_events(event.session_id), orjson.dumps(msg).decode())


NOBODY: Final = UUID(int=0)
"""``risk.ack.by`` for principals without a user id (dev tokens) — same value the viewer shell uses."""


# --- SLA escalation (ticker) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueMember:
    tenant_id: UUID
    risk_event_id: int
    member: str


def parse_members(members: Iterable[str]) -> tuple[list[DueMember], list[str]]:
    """``{tenant}:{risk_event_id}`` → :class:`DueMember`; returns ``(parsed, malformed)`` so the caller
    can drop garbage from the ZSET instead of re-logging it every tick."""
    out: list[DueMember] = []
    bad: list[str] = []
    for raw in members:
        tenant, sep, event_id = raw.rpartition(":")
        try:
            if not sep:
                raise ValueError(raw)
            out.append(DueMember(UUID(tenant), int(event_id), raw))
        except ValueError:
            bad.append(raw)
    if bad:
        log.warning("alerts:sla members malformed; dropping", extra={"count": len(bad)})
    return out, bad


async def escalate_due(ctx: Any, now: datetime) -> list[int]:
    """One SLA ticker pass (§7.2): ``ZRANGEBYSCORE alerts:sla -inf now`` → per tenant, in one
    transaction, ``escalation_level=1, escalated_at=now`` + audit ``alert.escalated`` → after commit
    ``PUBLISH risk.escalated`` on the session channel and ``tenant:{tid}:alerts`` → ``ZREM``.

    One step only: a member is removed whether or not the row was still open (an acknowledged or
    already-escalated row simply loses its stale member). Returns the ids escalated this pass.
    """
    redis = ctx.redis
    raw = await redis.zrangebyscore(keys.ALERTS_SLA, "-inf", now.timestamp() * 1000.0)
    due, malformed = parse_members(raw)
    if malformed:
        await redis.zrem(keys.ALERTS_SLA, *malformed)
    if not due:
        return []
    by_tenant: dict[UUID, list[DueMember]] = defaultdict(list)
    for item in due:
        by_tenant[item.tenant_id].append(item)

    escalated: list[int] = []
    for tenant_id, items in by_tenant.items():
        rows: list[RiskEvent] = []
        async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as s:
            for item in items:
                row = await risk_repo.escalate(s, item.risk_event_id, now=now)
                if row is None:
                    continue
                rows.append(row)
                await audit.record(
                    s,
                    tenant_id=tenant_id,
                    actor_id=None,
                    actor_role="service",
                    action="alert.escalated",
                    resource_type="risk_event",
                    resource_id=str(row.id),
                    detail={
                        "session_id": str(row.session_id),
                        "severity": int(row.severity),
                        "sla_deadline_at": _iso(row.sla_deadline_at),
                        "escalation_level": 1,
                    },
                )
        for row in rows:
            msg = orjson.dumps({"t": "risk.escalated", "risk_event_id": int(row.id)}).decode()
            await redis.publish(keys.sess_events(row.session_id), msg)
            await redis.publish(keys.tenant_alerts(tenant_id), msg)
            escalated.append(int(row.id))
        await redis.zrem(keys.ALERTS_SLA, *[item.member for item in items])
    if escalated:
        log.info("alerts escalated", extra={"count": len(escalated), "tenants": len(by_tenant)})
    return escalated


async def over_sla_count(ctx: Any, now: datetime) -> int:
    """``risk_unacked_over_sla`` from PostgreSQL (truth even when Redis lost the ZSET): open alerts
    whose deadline has passed, summed over active tenants via the ``ix_risk_open_sla`` order."""
    total = 0
    for tenant_id in await active_tenant_ids(ctx.engine):
        async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as s:
            rows = await risk_repo.list_open(s, tenant_id, limit=OPEN_SCAN_LIMIT)
        for row in rows:  # ordered by sla_deadline_at NULLS LAST → stop at the first future deadline
            if row.sla_deadline_at is None or row.sla_deadline_at >= now:
                break
            total += 1
    metrics.RISK_UNACKED_OVER_SLA.set(total)
    return total
