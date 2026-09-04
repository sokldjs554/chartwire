"""``Idempotency-Key`` records (spec §5): ``idem:{tenant}:{key}`` → JSON, ``SET NX``, 24 h.

Life cycle of one key:

1. :func:`begin` — ``SET NX`` a *pending* marker carrying the request body hash. If the key already
   exists the stored record is returned instead, so the caller can replay (same body hash) or reject
   (different body → the client reused a key for another request).
2. :func:`complete` — overwrite the marker with the final response (status, content type, body).
3. :func:`release` — drop the marker when the request failed before producing a storable response
   (5xx, exception), so the client can retry with the same key.

The stored body is the exact response the same principal already received; nothing else is kept —
and the record carries that principal's ``sub`` so it can only ever be replayed *to them*. The key
space is per tenant (``idem:{tenant}:{key}``, spec §5), but a replay bypasses the route and therefore
``rbac.require``: without the owner check a ``staff`` user who guessed an admin's key would be handed
the admin's purge receipt with a 202 the RBAC matrix forbids. A mismatch is a key collision between
two principals, so it is rejected exactly like a body mismatch.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

import orjson
from redis.asyncio import Redis

from chartwire.redis import keys

PENDING: Final = "pending"
DONE: Final = "done"
MAX_KEY_LEN: Final = 128


@dataclass(frozen=True, slots=True)
class Record:
    state: Literal["pending", "done"]
    body_hash: str
    sub: str = ""
    """``Principal.sub`` of the request that claimed the key; a replay to any other principal is a 422."""
    status: int = 0
    content_type: str = ""
    body: bytes = b""

    def to_json(self) -> str:
        return orjson.dumps(
            {
                "state": self.state,
                "body_hash": self.body_hash,
                "sub": self.sub,
                "status": self.status,
                "content_type": self.content_type,
                "body": base64.b64encode(self.body).decode("ascii"),
            }
        ).decode()

    @classmethod
    def from_json(cls, raw: str | bytes) -> Record:
        data = orjson.loads(raw)
        return cls(
            state=data["state"],
            body_hash=str(data["body_hash"]),
            sub=str(data.get("sub", "")),
            status=int(data.get("status", 0)),
            content_type=str(data.get("content_type", "")),
            body=base64.b64decode(data.get("body", "")),
        )


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def valid_key(key: str) -> bool:
    return 0 < len(key) <= MAX_KEY_LEN and key.isprintable() and " " not in key


async def begin(
    redis: Redis, tenant: UUID | str, key: str, request_body_hash: str, *, sub: str = ""
) -> Record | None:
    """Claim ``key`` for this request. ``None`` = claimed (first use); otherwise the existing record."""
    marker = Record(state=PENDING, body_hash=request_body_hash, sub=sub)
    claimed = await redis.set(
        keys.idempotency(tenant, key), marker.to_json(), nx=True, ex=keys.TTL_IDEMPOTENCY
    )
    if claimed:
        return None
    raw = await redis.get(keys.idempotency(tenant, key))
    return Record.from_json(raw) if raw is not None else marker


async def complete(
    redis: Redis,
    tenant: UUID | str,
    key: str,
    *,
    request_body_hash: str,
    sub: str = "",
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    record = Record(DONE, request_body_hash, sub, status, content_type, body)
    await redis.set(keys.idempotency(tenant, key), record.to_json(), ex=keys.TTL_IDEMPOTENCY)


async def release(redis: Redis, tenant: UUID | str, key: str) -> None:
    await redis.delete(keys.idempotency(tenant, key))
