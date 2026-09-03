"""Handler used by the SIGKILL chaos test (§7.1): waits for a flag file, then writes one audit row.

Registered by ``run_worker.py`` in the subprocess and by the test itself when it inspects the registry;
the effect (an ``audit_events`` row with ``action='session.transcribed'``) is what the test counts.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from chartwire.audit import service as audit
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.registry import Registry

EVENT_TYPE = "chaos.sleep"
LEASE_S = 4
STARTED_ENV = "CHAOS_STARTED"
FLAG_ENV = "CHAOS_FLAG"


def register(registry: Registry) -> None:
    @registry.handler(EVENT_TYPE, lease_s=LEASE_S, name="chaos_sleep")
    async def chaos_sleep(ctx: HandlerContext, event: OutboxEvent) -> None:
        started = os.environ.get(STARTED_ENV)
        flag = os.environ.get(FLAG_ENV)
        if started:
            await asyncio.to_thread(Path(started).write_text, str(os.getpid()))
        while flag and not await asyncio.to_thread(os.path.exists, flag):  # noqa: ASYNC110 - cross-process flag file
            await asyncio.sleep(0.05)  # the test creates the flag file from the outside
        async with ctx.tenant_tx(event.tenant_id) as session:
            await audit.record(
                session,
                tenant_id=event.tenant_id,
                actor_id=None,
                actor_role="service",
                action="session.transcribed",
                resource_type="outbox_event",
                resource_id=str(event.id),
                detail={"pid": os.getpid()},
            )
