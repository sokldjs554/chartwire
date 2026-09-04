"""``risk/alerts.py`` + the ``alert_sla`` ticker against real PostgreSQL/Redis (§7.2, §7.4, §8.5):
create (row + ZSET + publish), acknowledge (RLS-scoped, idempotent, audited, ZREM + ``risk.ack``),
escalate after the deadline (one step only, both channels, audited) with a ``FakeClock``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import pytest
from sqlalchemy import select

from chartwire.core.config import Settings
from chartwire.db.models import AuditEvent
from chartwire.db.repo import risk as risk_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops import metrics
from chartwire.outbox.context import HandlerContext
from chartwire.redis import keys
from chartwire.risk import alerts
from chartwire.risk.detector import RiskHit
from chartwire.risk.scope import ScopeFlags
from chartwire.worker.handlers import alert_sla

pytestmark = pytest.mark.integration


def make_ctx(engine: Any, redis: Any, clock: Any) -> HandlerContext:
    return HandlerContext(
        engine=engine,
        redis=redis,
        objectstore=None,
        clock=clock,
        settings=Settings(),
        kek=None,
        keycache=None,
    )


def hit(severity: int, *, category: str = "suicidal_ideation") -> RiskHit:
    return RiskHit(
        category=category, severity=severity, phrase="사라지고 싶", start=4, end=10, scope=ScopeFlags()
    )


async def create_alert(
    ctx: HandlerContext, tenant_id, session_row, patient_id, *, severity: int, seq: int = 0
):
    """A segment + its alert exactly as the stt-worker commits them, then the after-commit effects."""
    now = ctx.clock.now()
    async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as s:
        segment = await segments_repo.insert_final(
            s,
            tenant_id=tenant_id,
            session_id=session_row.id,
            patient_id=patient_id,
            seq=seq,
            speaker="patient",
            t_start_ms=seq * 1000,
            t_end_ms=seq * 1000 + 900,
            text_enc=b"\x00" * 40,
            text_len=12,
            confidence=0.9,
            provider="simulator",
            created_at=session_row.started_at + timedelta(milliseconds=seq * 1000),
        )
        event = await alerts.create_event(
            s,
            tenant_id=tenant_id,
            session_id=session_row.id,
            patient_id=patient_id,
            segment_id=segment.id,
            segment_created_at=segment.created_at,
            segment_seq=seq,
            hit=hit(severity),
            now=now,
        )
    msg = await alerts.after_commit(ctx.redis, event, committed_at=now)
    return event, msg


async def subscribe(redis: Any, *channels: str) -> Any:
    ps = redis.pubsub()
    await ps.subscribe(*channels)
    for _ in channels:  # consume the subscribe confirmations so nothing published afterwards is missed
        assert (await ps.get_message(timeout=2.0)) is not None
    return ps


async def messages(ps: Any, settle_s: float = 0.3) -> list[tuple[str, dict[str, Any]]]:
    """Everything published so far: keep reading until ``settle_s`` passes without a message."""
    out: list[tuple[str, dict[str, Any]]] = []
    last = time.monotonic()
    while time.monotonic() - last < settle_s:
        raw = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if raw is not None:
            out.append((raw["channel"], orjson.loads(raw["data"])))
            last = time.monotonic()
    return out


async def audit_actions(engine: Any, tenant_id) -> list[tuple[str, str | None]]:
    async with tenant_tx(engine, TenantCtx.service(tenant_id)) as s:
        rows = await s.execute(select(AuditEvent.action, AuditEvent.actor_role).order_by(AuditEvent.id))
        return [(a, r) for a, r in rows]


# --------------------------------------------------------------------------- tests


async def test_create_event_sets_sla_member_and_publishes_alert(
    app_engine, redis, fake_clock, tenant_a, patient_a, session_a
):
    ctx = make_ctx(app_engine, redis, fake_clock)
    ps = await subscribe(redis, keys.sess_events(session_a.id))
    sev3, msg3 = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=3, seq=0)
    sev1, msg1 = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=1, seq=1)

    assert sev3.sla_deadline_at == fake_clock.now() + timedelta(seconds=60)
    assert sev1.sla_deadline_at is None, "severity 1: no timer (§7.4)"
    assert sev3.detector_version == "lex-1" and sev3.scope == {
        "negated": False,
        "hypothetical": False,
        "past": False,
        "third_person": False,
        "clinician_question": False,
        "idiom": False,
        "present_denial": False,
    }
    member = keys.alerts_sla_member(tenant_a.id, sev3.id)
    assert await redis.zscore(keys.ALERTS_SLA, member) == sev3.sla_deadline_at.timestamp() * 1000
    assert await redis.zcard(keys.ALERTS_SLA) == 1

    assert set(msg3) == {
        "t",
        "risk_event_id",
        "category",
        "severity",
        "segment_seq",
        "span",
        "sla_deadline_at",
        "committed_at",
    }
    assert msg3["span"] == [4, 10] and msg3["segment_seq"] == 0 and msg3["severity"] == 3
    assert msg3["committed_at"] == fake_clock.now().isoformat()
    assert "sla_deadline_at" not in msg1
    published = [m for _, m in await messages(ps)]
    assert published == [msg3, msg1]
    assert await audit_actions(app_engine, tenant_a.id) == [("alert.created", "service")] * 2

    with pytest.raises(ValueError):
        suppressed = RiskHit("suicidal_ideation", 2, "죽고 싶", 0, 4, ScopeFlags(negated=True))
        async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
            await alerts.create_event(
                s,
                tenant_id=tenant_a.id,
                session_id=session_a.id,
                patient_id=patient_a.id,
                segment_id=1,
                segment_created_at=fake_clock.now(),
                segment_seq=9,
                hit=suppressed,
                now=fake_clock.now(),
            )


async def test_ack_is_rls_scoped_idempotent_audited_and_published(
    app_engine, redis, fake_clock, tenant_a, tenant_b, clinician_a, patient_a, session_a
):
    ctx = make_ctx(app_engine, redis, fake_clock)
    event, _ = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=2)
    ps = await subscribe(redis, keys.sess_events(session_a.id))
    fake_clock.advance(5)

    assert (
        await alerts.ack(ctx, tenant_id=tenant_b.id, risk_event_id=event.id, by=None, actor_role="clinician")
        is None
    )
    assert await alerts.ack(ctx, tenant_id=tenant_a.id, risk_event_id=event.id + 99, by=None) is None
    assert await redis.zcard(keys.ALERTS_SLA) == 1, "no effect without a visible row"

    acked = await alerts.ack(
        ctx,
        tenant_id=tenant_a.id,
        risk_event_id=event.id,
        by=clinician_a.id,
        actor_role="clinician",
        via="ws",
    )
    assert acked is not None and acked.acknowledged_by == clinician_a.id
    assert acked.acknowledged_at == fake_clock.now()
    assert await redis.zcard(keys.ALERTS_SLA) == 0
    assert [m for _, m in await messages(ps)] == [
        {"t": "risk.ack", "risk_event_id": event.id, "by": str(clinician_a.id)}
    ]

    again = await alerts.ack(ctx, tenant_id=tenant_a.id, risk_event_id=event.id, by=None, actor_role="admin")
    assert again is not None and again.acknowledged_by == clinician_a.id, "idempotent: first ack wins"
    assert await audit_actions(app_engine, tenant_a.id) == [
        ("alert.created", "service"),
        ("alert.acked", "clinician"),
    ]
    assert [m for _, m in await messages(ps)] == [
        {"t": "risk.ack", "risk_event_id": event.id, "by": str(alerts.NOBODY)}
    ]


async def test_escalate_due_one_step_after_deadline(
    app_engine, redis, fake_clock, tenant_a, clinician_a, patient_a, session_a
):
    ctx = make_ctx(app_engine, redis, fake_clock)
    urgent, _ = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=3, seq=0)  # +60 s
    later, _ = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=2, seq=1)  # +300 s
    acked, _ = await create_alert(ctx, tenant_a.id, session_a, patient_a.id, severity=3, seq=2)
    await alerts.ack(ctx, tenant_id=tenant_a.id, risk_event_id=acked.id, by=clinician_a.id)
    await redis.zadd(keys.ALERTS_SLA, {keys.alerts_sla_member(tenant_a.id, acked.id): 1.0})  # stale member
    await redis.zadd(keys.ALERTS_SLA, {"not-a-member": 1.0})
    ps = await subscribe(redis, keys.sess_events(session_a.id), keys.tenant_alerts(tenant_a.id))

    assert await alerts.escalate_due(ctx, fake_clock.now() + timedelta(seconds=59)) == []
    assert sorted(await redis.zrange(keys.ALERTS_SLA, 0, -1)) == sorted(
        [keys.alerts_sla_member(tenant_a.id, urgent.id), keys.alerts_sla_member(tenant_a.id, later.id)]
    ), "due-but-acked and malformed members are dropped without escalating anything"

    fake_clock.advance(61)
    assert await alert_sla.tick(ctx) == [urgent.id]
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as s:
        row = await risk_repo.get_event(s, urgent.id)
        untouched = await risk_repo.get_event(s, later.id)
    assert row is not None and row.escalation_level == 1 and row.escalated_at == fake_clock.now()
    assert untouched is not None and untouched.escalation_level == 0
    escalated_msg = {"t": "risk.escalated", "risk_event_id": urgent.id}
    got = await messages(ps)
    assert sorted(got) == sorted(
        [(keys.sess_events(session_a.id), escalated_msg), (keys.tenant_alerts(tenant_a.id), escalated_msg)]
    )
    assert await redis.zrange(keys.ALERTS_SLA, 0, -1) == [keys.alerts_sla_member(tenant_a.id, later.id)]
    assert metrics.REGISTRY.get_sample_value("risk_unacked_over_sla") == 1.0, "escalated but still unacked"

    fake_clock.advance(1000)
    assert await alerts.escalate_due(ctx, fake_clock.now()) == [later.id]
    assert await alerts.escalate_due(ctx, fake_clock.now()) == [], "one escalation step only"
    actions = [a for a, _ in await audit_actions(app_engine, tenant_a.id)]
    assert actions.count("alert.escalated") == 2 and actions.count("alert.created") == 3
    assert await alerts.over_sla_count(ctx, fake_clock.now()) == 2


def test_parse_members_skips_garbage():
    tenant = "7b6f8d3a-2a2e-4c8e-9f3d-0f1e2d3c4b5a"
    parsed, malformed = alerts.parse_members([f"{tenant}:42", "garbage", f"{tenant}:x", "1:2:3"])
    assert [(str(p.tenant_id), p.risk_event_id) for p in parsed] == [(tenant, 42)]
    assert malformed == ["garbage", f"{tenant}:x", "1:2:3"]


def test_sla_deadline_table():
    at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert alerts.sla_deadline(3, at) == at + timedelta(seconds=60)
    assert alerts.sla_deadline(2, at) == at + timedelta(seconds=300)
    assert alerts.sla_deadline(1, at) is None
