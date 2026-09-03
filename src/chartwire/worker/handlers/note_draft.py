"""``session.transcribed`` → :func:`chartwire.notes.service.draft_for_session` (spec §7.2).

Importing this module registers the handler; ``worker/main.py`` imports it by name. Delivery is
at-least-once: the poller's ``processed_events`` row and the note commit together, so a redelivery
after a crash between commit and ``mark_done`` is skipped, never drafted twice. A consent that no
longer includes ``ai_drafting`` is not an error — the service records ``abstained(consent_scope_missing)``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from chartwire.notes import service
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.registry import SESSION_TRANSCRIBED, handler

log = logging.getLogger(__name__)

LEASE_S = 120
"""An LLM provider may take up to its 30 s timeout twice (schema retry); the lease covers that."""


@handler(SESSION_TRANSCRIBED, lease_s=LEASE_S)
async def note_draft(ctx: HandlerContext, event: OutboxEvent) -> None:
    session_id = UUID(str(event.payload.get("session_id") or event.aggregate_id))
    outcome = await service.draft_for_session(ctx, session_id, tenant_id=event.tenant_id)
    log.info(
        "note_draft handled",
        extra={
            "event_id": event.id,
            "session_id": str(session_id),
            "note_id": str(outcome.note_id) if outcome.note_id else None,
            "status": outcome.status,
            "reason": outcome.abstain_reason,
        },
    )
