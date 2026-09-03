"""Process-wide ledger batcher (spec §6.6): the only writer of ``audio_chunks`` on the ingest path.

Every stored chunk becomes one :class:`ChunkRow`; :meth:`LedgerBatcher.submit` returns a future
that resolves when the transaction containing the row has **committed** — that is the moment an
``ack`` may cover it (§0 rule 3). Rows are flushed after ``flush_ms`` (50 ms) or ``flush_rows``
(500), grouped by tenant into one transaction each under ``chartwire_app`` with ``app.tenant_id``
set: a multi-row ``INSERT … ON CONFLICT DO NOTHING`` plus one ``UPDATE sessions SET ack_seq =
GREATEST(ack_seq, v)``. The 50 ms window is the floor under the ack round-trip.

``ack_hint`` is the contiguous durable prefix the ingest core will hold once the row commits
(``contig_seq`` at store time). PostgreSQL does not take the hint on trust: ``sessions.ack_seq`` is
raised to it only when the same transaction can count every row between the old ``ack_seq`` and the
hint (§0 rule 3 enforced in SQL). A hint that skips rows of an earlier, failed batch therefore never
moves the persisted ack, whatever the shells believe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, cast, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chartwire.db.models import AudioChunk
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx

try:  # metrics are optional at import time (ops package is WP-G)
    from chartwire.ops import metrics as _metrics
except ImportError:  # pragma: no cover
    _metrics = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChunkRow:
    tenant_id: UUID
    session_id: UUID
    seq: int
    byte_len: int
    sha256: bytes
    storage_key: str
    offset_ms: int
    flags: int
    received_at: datetime

    def values(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "byte_len": self.byte_len,
            "sha256": self.sha256,
            "storage_key": self.storage_key,
            "offset_ms": self.offset_ms,
            "flags": self.flags,
            "received_at": self.received_at,
        }


@dataclass(slots=True)
class _Pending:
    row: ChunkRow
    ack_hint: int
    future: asyncio.Future[None]


class LedgerError(RuntimeError):
    """A flush failed; every future of the batch carries this (the shell closes ``4503``)."""


def _raise_ack_stmt(hints: dict[UUID, int]) -> Any:
    """One ``UPDATE sessions … FROM unnest(ids, acks)`` for every session of the batch.

    The ack moves only if the rows ``(ack_seq, hint]`` are all present — a count over the primary key
    of exactly the rows this batch is about to acknowledge."""
    v = select(
        func.unnest(cast(list(hints), ARRAY(PG_UUID(as_uuid=True)))).label("id"),
        func.unnest(cast(list(hints.values()), ARRAY(BigInteger))).label("ack"),
    ).subquery("v")
    present = (
        select(func.count())
        .select_from(AudioChunk)
        .where(
            AudioChunk.session_id == SessionModel.id,
            AudioChunk.seq > SessionModel.ack_seq,
            AudioChunk.seq <= v.c.ack,
        )
        .scalar_subquery()
    )
    return (
        update(SessionModel)
        .where(
            SessionModel.id == v.c.id,
            v.c.ack > SessionModel.ack_seq,
            present == v.c.ack - SessionModel.ack_seq,
        )
        .values(ack_seq=v.c.ack, updated_at=func.now())
    )


class LedgerBatcher:
    def __init__(self, engine: AsyncEngine, *, flush_ms: int = 50, flush_rows: int = 500) -> None:
        self._engine = engine
        self._flush_s = flush_ms / 1000
        self._flush_rows = flush_rows
        self._pending: list[_Pending] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.flushes = 0
        self.rows_committed = 0

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._closed = False
            self._task = asyncio.get_running_loop().create_task(self._run(), name="ledger-batcher")

    async def stop(self) -> None:
        """Flush what is pending and stop; further ``submit`` calls fail immediately."""
        self._closed = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    # --- API used by the ingest shell ------------------------------------------------------------

    def submit(self, row: ChunkRow, ack_hint: int) -> asyncio.Future[None]:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self._closed or self._task is None:
            fut.set_exception(LedgerError("ledger batcher is not running"))
            return fut
        self._pending.append(_Pending(row, ack_hint, fut))
        self._set_gauge()
        if len(self._pending) == 1 or len(self._pending) >= self._flush_rows:
            self._wake.set()  # first row: open the window; row cap: flush now
        return fut

    def pending_rows(self) -> int:
        return len(self._pending)

    async def flush_now(self) -> None:
        """Commit everything pending right away (tests, drain)."""
        if self._pending:
            await self._flush(self._take())

    # --- internals -------------------------------------------------------------------------------

    def _take(self) -> list[_Pending]:
        batch, self._pending = self._pending, []
        self._set_gauge()
        return batch

    async def _run(self) -> None:
        while not self._closed:
            if not self._pending:  # idle: wait for the first row of the next batch
                self._wake.clear()
                await self._wake.wait()
                continue
            if len(self._pending) < self._flush_rows:  # give the window a chance to fill …
                self._wake.clear()  # … unless the row cap (or stop) wakes us first
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._flush_s)
            if self._pending:
                await self._safe_flush(self._take())
        if self._pending:
            await self._safe_flush(self._take())

    async def _safe_flush(self, batch: list[_Pending]) -> None:
        """A bug in the flush path must never strand futures (the shells would wait for acks forever)."""
        try:
            await self._flush(batch)
        except Exception as exc:
            log.exception("ledger flush crashed")
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(LedgerError(type(exc).__name__))

    async def _flush(self, batch: list[_Pending]) -> None:
        started = time.perf_counter()
        by_tenant: dict[UUID, list[_Pending]] = defaultdict(list)
        for item in batch:
            by_tenant[item.row.tenant_id].append(item)
        results = await asyncio.gather(
            *(self._flush_tenant(tid, items) for tid, items in by_tenant.items()), return_exceptions=True
        )
        for (tid, items), result in zip(by_tenant.items(), results, strict=True):
            if isinstance(result, BaseException):
                log.warning("ledger flush failed", extra={"tenant_id": str(tid), "rows": len(items)})
                for item in items:
                    if not item.future.done():
                        item.future.set_exception(LedgerError(type(result).__name__))
            else:
                self.rows_committed += len(items)
                for item in items:
                    if not item.future.done():
                        item.future.set_result(None)
        self.flushes += 1
        if _metrics is not None:
            _metrics.LEDGER_FLUSH_SECONDS.observe(time.perf_counter() - started)
            _metrics.LEDGER_FLUSH_ROWS.observe(len(batch))

    async def _flush_tenant(self, tenant_id: UUID, items: list[_Pending]) -> None:
        async with tenant_tx(self._engine, TenantCtx.service(tenant_id)) as session:
            await sessions_repo.insert_chunks(session, [item.row.values() for item in items])
            await self._update_ack(session, items)

    async def _update_ack(self, session: AsyncSession, items: list[_Pending]) -> None:
        hints: dict[UUID, int] = {}
        for item in items:
            sid = item.row.session_id
            if item.ack_hint > hints.get(sid, 0):
                hints[sid] = item.ack_hint
        if hints:
            await session.execute(_raise_ack_stmt(hints))

    def _set_gauge(self) -> None:
        if _metrics is not None:
            _metrics.LEDGER_PENDING_ROWS.set(len(self._pending))
