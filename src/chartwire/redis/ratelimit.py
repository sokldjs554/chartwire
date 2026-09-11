"""Fixed-window rate limiting (spec §5): ``rl:{tenant}:{principal}:{bucket}:{minute}`` → ``INCR`` + ``EXPIRE 60``.

One key per principal, bucket and wall-clock minute. The api middleware uses two buckets: ``rest``
(120/min per principal) and ``ws-ticket`` (30/min). The WS shell's ``hello`` budget (10/min per
session) uses the same primitive with the session id as the principal.

``window_s`` widens the window: the public consultation form counts ``consultation`` per client
address per wall-clock *hour* (``rl:-:{client}:consultation:{hour}``, 10/h) — same key family, same
``INCR`` + ``EXPIRE`` shape, so a Redis ``SCAN rl:*`` still shows every throttle in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from chartwire.redis import keys

REST_PER_MIN: Final = 120
WS_TICKET_PER_MIN: Final = 30
HELLO_PER_MIN: Final = 10
BUCKET_REST: Final = "rest"
BUCKET_WS_TICKET: Final = "ws-ticket"
BUCKET_HELLO: Final = "hello"
CONSULTATION_PER_HOUR: Final = 10
BUCKET_CONSULTATION: Final = "consultation"
WINDOW_MINUTE: Final = 60
WINDOW_HOUR: Final = 3600


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    count: int
    limit: int
    retry_after_s: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)


def minute_of(now: datetime) -> int:
    return int(now.timestamp()) // WINDOW_MINUTE


def window_of(now: datetime, window_s: int) -> int:
    """Index of the fixed window ``now`` falls in (``window_s=60`` → :func:`minute_of`)."""
    return int(now.timestamp()) // window_s


async def hit(
    redis: Redis,
    tenant: UUID | str,
    principal: UUID | str,
    bucket: str,
    limit_per_window: int,
    *,
    now: datetime,
    window_s: int = WINDOW_MINUTE,
) -> Decision:
    """Count one request and decide. ``EXPIRE`` is set on every hit (cheap, and it makes the key
    self-cleaning even if the first ``INCR`` and its ``EXPIRE`` were split by a crash). The key
    expires with its window (``TTL_RATELIMIT`` = 60 s for the default minute window)."""
    key = keys.ratelimit(tenant, principal, bucket, window_of(now, window_s))
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, keys.TTL_RATELIMIT if window_s == WINDOW_MINUTE else window_s)
        count = int((await pipe.execute())[0])
    retry_after = window_s - int(now.timestamp()) % window_s
    return Decision(
        allowed=count <= limit_per_window, count=count, limit=limit_per_window, retry_after_s=retry_after
    )


async def check(
    redis: Redis,
    tenant: UUID | str,
    principal: UUID | str,
    bucket: str,
    limit_per_min: int,
    *,
    now: datetime,
) -> bool:
    """§3.1 contract: ``True`` when the request is within the per-minute budget."""
    return (await hit(redis, tenant, principal, bucket, limit_per_min, now=now)).allowed
