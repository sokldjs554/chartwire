"""One-time WebSocket tickets (spec §5, §6.1): ``ticket:{token}`` → JSON payload, 30 s, consumed with ``GETDEL``.

A ticket binds tenant, user, role, session and endpoint kind; the WS shell trusts it instead of
re-reading the JWT. The token itself never appears in logs (``redact_phi`` key ``ticket``)."""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

import orjson
from redis.asyncio import Redis

from chartwire.redis import keys

Kind = Literal["ingest", "watch"]


@dataclass(frozen=True, slots=True)
class TicketPayload:
    tenant_id: UUID
    user_id: UUID | None
    role: str
    session_id: UUID
    kind: Kind
    sub: str | None = None
    """Raw JWT ``sub`` when it is not a UUID (dev tokens ``dev:<role>``)."""


async def issue(
    redis: Redis,
    *,
    tenant_id: UUID | str,
    user_id: UUID | str | None,
    role: str,
    session_id: UUID | str,
    kind: Kind,
    ttl_s: int = keys.TTL_TICKET,
) -> str:
    """Store a fresh ticket and return the opaque token (43 url-safe chars)."""
    token = secrets.token_urlsafe(32)
    uid, sub = _split_user(user_id)
    payload = TicketPayload(UUID(str(tenant_id)), uid, role, UUID(str(session_id)), kind, sub)
    await redis.set(keys.ticket(token), orjson.dumps(_dump(payload)).decode(), ex=ttl_s)
    return token


async def consume(redis: Redis, ticket: str) -> TicketPayload | None:
    """Atomically read-and-delete (``GETDEL``): a ticket is usable exactly once."""
    raw = await redis.getdel(keys.ticket(ticket))
    if raw is None:
        return None
    data = orjson.loads(raw)
    return TicketPayload(
        tenant_id=UUID(data["tenant_id"]),
        user_id=UUID(data["user_id"]) if data.get("user_id") else None,
        role=str(data["role"]),
        session_id=UUID(data["session_id"]),
        kind=data["kind"],
        sub=data.get("sub"),
    )


def _split_user(user_id: UUID | str | None) -> tuple[UUID | None, str | None]:
    if user_id is None:
        return None, None
    try:
        return UUID(str(user_id)), None
    except ValueError:
        return None, str(user_id)


def _dump(payload: TicketPayload) -> dict[str, str | None]:
    return {k: (None if v is None else str(v)) for k, v in asdict(payload).items()}
