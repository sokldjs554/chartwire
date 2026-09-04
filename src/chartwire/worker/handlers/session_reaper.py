"""``session_reaper`` ticker (§7.2, §6.4 rule 5): a ``recording``/``paused`` session whose last
activity is older than ``session_idle_timeout_s`` is ended (state, audit ``session.ended``) and an end
marker is appended to its chunk stream so the stt-worker flushes and emits ``session.transcribed``.

Last activity = ``sess:{sid}.updated_at`` in Redis when the hot hash exists, else ``sessions.updated_at``
(Redis is a cache; a lost hash must not keep a session alive forever). The end marker goes through
``SessionState.xadd_end`` (same entry shape the recorder shell writes).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from chartwire.audit import service as audit
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import sessions as sessions_repo
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.runtime import active_tenant_ids
from chartwire.redis import keys
from chartwire.redis.session_state import SessionState

log = logging.getLogger(__name__)

INTERVAL_S = 60.0
LIVE_STATES = ("recording", "paused")
SCAN_LIMIT = 500


def parse_updated_at(value: object) -> datetime | None:
    """``updated_at`` hash field: epoch seconds or milliseconds, or an ISO-8601 string."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if number > 1e11:  # milliseconds
        number /= 1000.0
    return datetime.fromtimestamp(number, tz=UTC)


async def last_activity(redis: Any, row: SessionModel) -> datetime:
    if redis is not None:
        try:
            value = await redis.hget(keys.sess(row.id), "updated_at")
        except Exception:
            log.warning("session hash unavailable; using sessions.updated_at", exc_info=True)
            value = None
        parsed = parse_updated_at(value)
        if parsed is not None:
            return parsed
    updated = row.updated_at
    return updated if updated.tzinfo else updated.replace(tzinfo=UTC)


async def end_marker(redis: Any, session_id: UUID, epoch: int, *, maxlen: int) -> None:
    """Append ``{"end":"1","ep":epoch}`` to ``sess:{sid}:chunks``; hash ``state=ended``; publish."""
    state = SessionState(redis, stream_maxlen=maxlen)
    await state.xadd_end(session_id, epoch)
    await state.set_fields(session_id, state="ended")
    await state.publish_event(session_id, {"t": "session.state", "state": "ended"})


async def reap(ctx: HandlerContext, tenant_id: UUID, row: SessionModel, *, idle_s: float) -> None:
    now = ctx.clock.now()
    async with ctx.tenant_tx(tenant_id) as session:
        await sessions_repo.set_state(session, row.id, "ended", now=now)
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor_id=None,
            actor_role="service",
            action="session.ended",
            resource_type="session",
            resource_id=row.id,
            detail={"reason": "idle_timeout", "idle_s": int(idle_s), "epoch": row.epoch},
        )
    if ctx.redis is not None:
        try:
            await end_marker(ctx.redis, row.id, row.epoch, maxlen=int(ctx.settings.stream_maxlen))
        except Exception:
            # The row is ended and audited; the stt-worker's own idle path handles a missing marker.
            log.warning("end marker not written", extra={"session_id": str(row.id)}, exc_info=True)
    log.info("session reaped", extra={"session_id": str(row.id), "idle_s": int(idle_s)})


async def tick(ctx: HandlerContext) -> int:
    """Returns the number of sessions ended in this tick (all tenants)."""
    timeout_s = float(ctx.settings.session_idle_timeout_s)
    now = ctx.clock.now()
    reaped = 0
    for tenant_id in await active_tenant_ids(ctx.engine):
        live: list[SessionModel] = []
        async with ctx.tenant_tx(tenant_id) as session:
            for state in LIVE_STATES:
                live.extend(
                    await sessions_repo.list_sessions(
                        session, tenant_id=tenant_id, state=state, limit=SCAN_LIMIT
                    )
                )
        for row in live:
            idle_s = (now - await last_activity(ctx.redis, row)).total_seconds()
            if idle_s > timeout_s:
                await reap(ctx, tenant_id, row, idle_s=idle_s)
                reaped += 1
    return reaped
