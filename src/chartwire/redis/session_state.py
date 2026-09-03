"""Hot ingest state in Redis (spec §5): the ``sess:{sid}`` hash, the chunk stream and the
control/event channels. Key names come only from :mod:`chartwire.redis.keys`; the two Lua
scripts (``hello.lua``, ``xadd_chunk.lua``) are loaded once per client and re-loaded on
``NOSCRIPT`` (``redis.register_script``).

Redis is a rebuildable cache (ADR-0002): when the hash is missing the ingest shell calls
:meth:`SessionState.rehydrate` with values read from PostgreSQL *before* ``hello`` so the epoch
keeps counting from the persisted one and an old connection is still fenced after a flush.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson
from redis.asyncio import Redis

from chartwire.redis import keys

HASH_FIELDS = (
    "epoch",
    "state",
    "ledger_seq",
    "ack_seq",
    "credit",
    "node",
    "conn",
    "started_at",
    "stt_lag",
    "updated_at",
)


class StateLost(RuntimeError):
    """``sess:{sid}`` vanished mid-session (Redis flushed/restarted): fail closed, rehydrate on reconnect."""


def _script_source(name: str, scripts_dir: Path | None) -> str:
    if scripts_dir is not None:
        return (scripts_dir / name).read_text(encoding="utf-8")
    return resources.files("chartwire.redis.scripts").joinpath(name).read_text(encoding="utf-8")


class SessionState:
    def __init__(self, redis: Redis, *, stream_maxlen: int = 2000, scripts_dir: Path | None = None) -> None:
        self.redis = redis
        self.stream_maxlen = stream_maxlen
        self._hello = redis.register_script(_script_source("hello.lua", scripts_dir))
        self._xadd = redis.register_script(_script_source("xadd_chunk.lua", scripts_dir))

    # --- hash ------------------------------------------------------------------------------------

    async def get(self, sid: UUID | str) -> dict[str, str] | None:
        """The whole hash, or ``None`` when Redis has no hot state for the session."""
        data: dict[str, str] = await self.redis.hgetall(keys.sess(sid))
        return data or None

    async def set_fields(self, sid: UUID | str, **fields: Any) -> None:
        await self.redis.hset(keys.sess(sid), mapping={k: str(v) for k, v in fields.items()})

    async def rehydrate(
        self,
        sid: UUID | str,
        ack_seq: int,
        state: str,
        started_at: datetime | None,
        *,
        epoch: int = 0,
        ledger_seq: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Rebuild the hash from PostgreSQL values. Returns ``False`` (and writes nothing) when a
        concurrent hello already recreated it — ``HSETNX`` on ``epoch`` is the guard."""
        key = keys.sess(sid)
        if not await self.redis.hsetnx(key, "epoch", str(epoch)):
            return False
        fields = {
            "state": state,
            "ack_seq": str(ack_seq),
            "ledger_seq": str(ack_seq if ledger_seq is None else ledger_seq),
            "started_at": started_at.isoformat() if started_at else "",
            "updated_at": (now or datetime.now(tz=UTC)).isoformat(),
        }
        await self.redis.hset(key, mapping=fields)
        return True

    # --- hello / stream ------------------------------------------------------------------------

    async def hello(self, sid: UUID | str, node: str, conn: str, *, now: datetime) -> dict[str, Any]:
        """``HINCRBY epoch`` + node/conn + ``PUBLISH ctl superseded`` + consumer group + ``SADD stt:active``.

        Returns the hash after the increment with ``epoch`` as ``int``."""
        epoch, flat = await self._hello(
            keys=[keys.sess(sid), keys.ctl(sid), keys.sess_chunks(sid), keys.STT_ACTIVE],
            args=[node, conn, now.isoformat(), str(sid), keys.STT_CONSUMER_GROUP],
        )
        data: dict[str, Any] = dict(zip(flat[0::2], flat[1::2], strict=True))
        data["epoch"] = int(epoch)
        return data

    async def xadd_chunk(
        self, sid: UUID | str, epoch: int, fields: Mapping[str, Any], *, now: datetime
    ) -> bool:
        """Epoch-checked ``XADD MAXLEN ~ N``; ``False`` = stale epoch (dropped), raises :class:`StateLost`."""
        flat: list[str] = []
        for name in keys.CHUNK_FIELDS:
            flat += [name, str(fields[name])]
        result = await self._xadd(
            keys=[keys.sess(sid), keys.sess_chunks(sid)],
            args=[str(epoch), str(self.stream_maxlen), now.isoformat(), *flat],
        )
        if int(result) < 0:
            raise StateLost(str(sid))
        return int(result) == 1

    async def xadd_end(self, sid: UUID | str, epoch: int) -> None:
        """End marker ``{"end":"1","ep":<epoch>}`` — the stt-worker flushes and marks the session transcribed."""
        await self.redis.xadd(
            keys.sess_chunks(sid),
            {**keys.END_MARKER, "ep": str(epoch)},
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def expire_after_end(self, sid: UUID | str, ttl_s: int = keys.TTL_SESSION_AFTER_END) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            for key in (keys.sess(sid), keys.sess_chunks(sid), keys.sess_viewers(sid)):
                pipe.expire(key, ttl_s)
            await pipe.execute()

    # --- credit input / fan-out ------------------------------------------------------------------

    async def stt_lag(self, sid: UUID | str) -> int:
        value = await self.redis.hget(keys.STT_LAG, str(sid))
        return int(value) if value else 0

    async def publish_event(self, sid: UUID | str, msg: Mapping[str, Any]) -> None:
        await self.redis.publish(keys.sess_events(sid), orjson.dumps(msg).decode())

    async def publish_ctl(self, sid: UUID | str, msg: Mapping[str, Any]) -> None:
        await self.redis.publish(keys.ctl(sid), orjson.dumps(msg).decode())

    # --- viewer presence ---------------------------------------------------------------------------

    async def viewer_join(self, sid: UUID | str, conn: str) -> int:
        key = keys.sess_viewers(sid)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.sadd(key, conn)
            pipe.expire(key, keys.TTL_VIEWERS)
            pipe.scard(key)
            return int((await pipe.execute())[-1])

    async def viewer_leave(self, sid: UUID | str, conn: str) -> int:
        key = keys.sess_viewers(sid)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.srem(key, conn)
            pipe.scard(key)
            return int((await pipe.execute())[-1])
