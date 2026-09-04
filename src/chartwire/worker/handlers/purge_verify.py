"""``purge.completed`` → :func:`chartwire.purge.verify.run` (spec §7.2, §8.4 step 7).

The verifier records its verdict (``verified`` / ``failed`` + ``verify_result``) in its own
transaction and then raises on failure. Raising is deliberate: the poller retries with backoff and,
after ``max_attempts``, parks the event in ``dead_letters`` — the one place an operator looks for a
purge that did not hold. The ``failed`` state on the job is already durable by then.
"""

from __future__ import annotations

import logging
from uuid import UUID

from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.registry import PURGE_COMPLETED, handler
from chartwire.purge import verify

log = logging.getLogger(__name__)

LEASE_S = 300


@handler(PURGE_COMPLETED, lease_s=LEASE_S)
async def purge_verify(ctx: HandlerContext, event: OutboxEvent) -> None:
    job_id = UUID(str(event.payload.get("purge_job_id") or event.aggregate_id))
    job = await verify.run(ctx, job_id, tenant_id=event.tenant_id)
    log.info(
        "purge_verify handled", extra={"event_id": event.id, "purge_job_id": str(job.id), "state": job.state}
    )
