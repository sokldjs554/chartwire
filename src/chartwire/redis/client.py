"""Async Redis client factory."""

from __future__ import annotations

from redis.asyncio import Redis

from chartwire.core.config import get_settings


def get_redis(
    url: str | None = None, *, decode_responses: bool = True, max_connections: int | None = None
) -> Redis:
    """One client per process; ``url`` defaults to ``CHARTWIRE_REDIS_URL``.

    ``decode_responses=True`` because every value chartwire stores is text (JSON, ids, counters);
    audio bytes never enter Redis.

    ``max_connections`` defaults to ``CHARTWIRE_REDIS_MAX_CONNECTIONS``. It must be set explicitly:
    redis-py's own default (100) is a hard ceiling that raises ``MaxConnectionsError`` rather than
    queueing, and the stt-worker parks one connection per owned session inside ``XREADGROUP … BLOCK``.
    """
    settings = get_settings()
    return Redis.from_url(
        url or settings.redis_url,
        decode_responses=decode_responses,
        max_connections=max_connections or settings.redis_max_connections,
    )


def with_db(url: str, db: int) -> str:
    """Rewrite the database index of a Redis URL (test isolation: one index per work package)."""
    base, _, _ = url.rpartition("/") if url.count("/") >= 3 else (url, "", "")
    return f"{base}/{db}"
