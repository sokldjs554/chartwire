"""``partition_ensure`` ticker (§7.2): keep monthly ``transcript_segments`` partitions ahead of time
and assert the DEFAULT partition stays empty (``segments_default_partition_rows`` gauge).

``ensure_segment_partition(date)`` is SECURITY DEFINER and granted to ``chartwire_app``, so the ticker
needs no owner credentials. The default partition is FORCE-RLS like every other partition, so its row
count is the sum over active tenants (ADR-0001).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select, table

from chartwire.db import cli as dbcli
from chartwire.ops.metrics import SEGMENTS_DEFAULT_PARTITION_ROWS
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.runtime import active_tenant_ids

log = logging.getLogger(__name__)

INTERVAL_S = 3600.0
MONTHS_AHEAD = 2
"""Current month plus the next two (§7.2 "current+1, +2"; the current one is idempotent insurance)."""

_DEFAULT_PARTITION = table("transcript_segments_default")


async def default_partition_rows(ctx: HandlerContext) -> int:
    total = 0
    for tenant_id in await active_tenant_ids(ctx.engine):
        async with ctx.tenant_tx(tenant_id) as session:
            count = await session.execute(select(func.count()).select_from(_DEFAULT_PARTITION))
            total += int(count.scalar_one())
    return total


async def tick(ctx: HandlerContext) -> dict[str, Any]:
    """Create missing partitions, then measure the default partition. Returns a small summary."""
    partitions = await asyncio.to_thread(
        dbcli.ensure_partitions, ctx.settings.database_url, months_ahead=MONTHS_AHEAD, months_back=0
    )
    rows = await default_partition_rows(ctx)
    SEGMENTS_DEFAULT_PARTITION_ROWS.set(rows)
    if rows:
        log.error("segments in default partition", extra={"rows": rows})
    else:
        log.info("partitions ensured", extra={"partitions": partitions})
    return {"partitions": partitions, "default_rows": rows}
