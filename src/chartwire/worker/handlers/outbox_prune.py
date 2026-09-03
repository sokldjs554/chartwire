"""``outbox_prune`` ticker (§7.1): delete ``done`` outbox rows older than 24 h, per tenant, in
batches of 5,000 — keeps the partial index ``ix_outbox_pending`` and the table small (Q4 bloat).

Each batch is its own short transaction so a large backlog never holds one long lock; a tick stops
after ``MAX_BATCHES_PER_TENANT`` and the next tick continues.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from chartwire.db.repo import outbox as outbox_repo
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.runtime import active_tenant_ids

log = logging.getLogger(__name__)

INTERVAL_S = 600.0
BATCH = 5000
OLDER_THAN = timedelta(hours=24)
MAX_BATCHES_PER_TENANT = 20


async def tick(ctx: HandlerContext, *, older_than: timedelta = OLDER_THAN, batch: int = BATCH) -> int:
    """Returns the number of rows deleted in this tick (all tenants)."""
    total = 0
    for tenant_id in await active_tenant_ids(ctx.engine):
        for _ in range(MAX_BATCHES_PER_TENANT):
            async with ctx.tenant_tx(tenant_id) as session:
                deleted = await outbox_repo.prune_done(
                    session, older_than=older_than, batch=batch, now=ctx.clock.now()
                )
            total += deleted
            if deleted < batch:
                break
    if total:
        log.info("outbox pruned", extra={"rows": total})
    return total
