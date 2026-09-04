"""Risk alerts (§6.9): open-alert list (Q3 partial index) and acknowledgement.

The acknowledgement is ``chartwire.risk.alerts.ack`` (WP-C) — the same code path the viewer socket
uses — so REST and WS agree on the audit row, the ``alerts:sla`` ZREM and the ``risk.ack`` message.
The response carries the span, category and scope flags but never the matched phrase.
"""

from __future__ import annotations

from importlib import import_module
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from chartwire.api.deps import get_deps, not_found, open_tx, request_id
from chartwire.api.schemas import AlertOut
from chartwire.audit import service as audit
from chartwire.auth.jwt import Principal
from chartwire.auth.rbac import require
from chartwire.db.models import RiskEvent
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import tenant_tx
from chartwire.api.deps import principal_ctx
from chartwire.redis import keys
import orjson

router = APIRouter(prefix="/v1", tags=["alerts"])
CLINICAL = ("clinician", "staff")


def alert_out(row: RiskEvent) -> AlertOut:
    return AlertOut(
        id=int(row.id),
        session_id=row.session_id,
        patient_id=row.patient_id,
        segment_seq=int(row.segment_seq),
        category=row.category,
        severity=int(row.severity),
        span=[int(row.span_start), int(row.span_end)],
        scope=dict(row.scope or {}),
        detector_version=row.detector_version,
        detected_at=row.detected_at,
        sla_deadline_at=row.sla_deadline_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        escalation_level=int(row.escalation_level),
        escalated_at=row.escalated_at,
    )


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    request: Request,
    open: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require(*CLINICAL)),
) -> list[AlertOut]:
    """Unacknowledged alerts ordered by SLA deadline (Q3). Clinicians see their own sessions only."""
    if not open:
        return []  # acknowledged alerts are reachable per session through the audit trail (repo has no closed listing yet)
    async with open_tx(request, principal) as (_deps, s):
        rows = await risk_repo.list_open(s, principal.tenant_id, limit=limit)
        if principal.role == "clinician":
            own: dict[UUID, bool] = {}
            kept = []
            for row in rows:
                if row.session_id not in own:
                    sess = await sessions_repo.get_session(s, row.session_id)
                    own[row.session_id] = sess is not None and sess.clinician_id == principal.user_id
                if own[row.session_id]:
                    kept.append(row)
            rows = kept
        return [alert_out(r) for r in rows]


@router.post("/alerts/{id}/ack", response_model=AlertOut)
async def ack_alert(id: int, request: Request, principal: Principal = Depends(require(*CLINICAL))) -> AlertOut:
    deps = get_deps(request)
    try:
        alerts = import_module("chartwire.risk.alerts")
    except ImportError:
        alerts = None
    if alerts is not None:
        event = await alerts.ack(
            deps, tenant_id=principal.tenant_id, risk_event_id=id, by=principal.user_id, actor_role=principal.role, via="rest"
        )
    else:  # inline fallback with the same effects (UPDATE + audit + ZREM + publish)
        event = await _ack_inline(deps, principal, id, request_id(request))
    if event is None:
        raise not_found("경보")
    return alert_out(event)


async def _ack_inline(deps, principal: Principal, risk_event_id: int, rid: str | None):  # type: ignore[no-untyped-def]
    async with tenant_tx(deps.engine, principal_ctx(principal)) as s:
        event = await risk_repo.acknowledge(s, risk_event_id, by=principal.user_id, now=deps.clock.now())
        if event is None:
            return None
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="alert.acked",
            resource_type="risk_event",
            resource_id=str(risk_event_id),
            request_id=rid,
            detail={"session_id": str(event.session_id), "via": "rest"},
        )
    await deps.redis.zrem(keys.ALERTS_SLA, keys.alerts_sla_member(principal.tenant_id, risk_event_id))
    msg = {"t": "risk.ack", "risk_event_id": risk_event_id, "by": str(principal.user_id or UUID(int=0))}
    await deps.redis.publish(keys.sess_events(event.session_id), orjson.dumps(msg).decode())
    return event
