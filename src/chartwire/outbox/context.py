"""What an outbox handler receives: the claimed event row and the process-wide dependencies.

The concrete engine / redis / settings types live in other packages; they are typed as ``Any`` or as
minimal structural Protocols here so the outbox package stays importable on its own and mypy-strict.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.tenant import TenantCtx
from chartwire.db.tenant import tenant_tx as _db_tenant_tx

_CURRENT_TX: ContextVar[tuple[UUID, AsyncSession] | None] = ContextVar("chartwire_outbox_tx", default=None)
"""The handler transaction opened by ``runtime.run_handler`` for the task that runs a handler."""


class Clock(Protocol):
    """Structural twin of ``chartwire.core.clock.Clock`` (wall clock + monotonic)."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class ObjectStore(Protocol):
    """Structural twin of ``chartwire.objectstore.ObjectStore`` (§3.1; async like ``LocalFs``/``S3``)."""

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def list(self, prefix: str) -> list[str]: ...


class KekProvider(Protocol):
    """Structural twin of ``chartwire.crypto.KekProvider`` (§3.1)."""

    def wrap(self, dek: bytes, kek_ref: str) -> bytes: ...

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes: ...


class OutboxStatus(StrEnum):
    """``outbox_events.status`` values (CHECK constraint in §4.2)."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class HandlerContext:
    """Dependencies injected into every handler call (§3.1).

    ``engine`` / ``redis`` / ``settings`` / ``keycache`` are deliberately untyped: their classes belong
    to other packages and a handler that needs them imports the real types itself.
    """

    engine: Any
    redis: Any
    objectstore: ObjectStore
    clock: Clock
    settings: Any
    kek: KekProvider
    keycache: Any

    @contextlib.asynccontextmanager
    async def tenant_tx(self, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
        """Transaction under ``app.tenant_id``/``app.role='service'`` for handler DB effects (§7.1).

        Under the poller this *joins* the handler transaction that ``runtime.run_handler`` opened for
        the same tenant, so the handler's rows and ``processed_events`` commit together (and roll back
        together when the handler raises). Outside the poller (tests, CLI, tickers) it opens a fresh
        transaction that commits on exit.
        """
        current = _CURRENT_TX.get()
        if current is not None and current[0] == tenant_id:
            yield current[1]
            return
        async with _db_tenant_tx(self.engine, TenantCtx.service(tenant_id)) as session:
            yield session


@contextlib.contextmanager
def bind_tx(tenant_id: UUID, session: AsyncSession) -> Iterator[None]:
    """Make ``session`` the transaction ``HandlerContext.tenant_tx(tenant_id)`` joins (poller only)."""
    token = _CURRENT_TX.set((tenant_id, session))
    try:
        yield
    finally:
        _CURRENT_TX.reset(token)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Read-only view of a claimed ``outbox_events`` row.

    ``attempts`` is the number of executions *before* the current one; ``payload`` is an immutable
    mapping so a handler cannot accidentally mutate the row image shared with retry/DLQ bookkeeping.
    """

    id: int
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str
    attempts: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OutboxEvent:
        """Build from a ``RETURNING *`` row (driver-neutral: UUIDs/JSON may arrive as strings)."""
        created_at = row["created_at"]
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        return cls(
            id=int(row["id"]),
            tenant_id=_as_uuid(row["tenant_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=_as_uuid(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            payload=_as_payload(row["payload"]),
            idempotency_key=str(row["idempotency_key"]),
            attempts=int(row["attempts"]),
            created_at=created_at,
        )


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError(f"expected UUID or str, got {type(value).__name__}")


def _as_payload(value: object) -> Mapping[str, Any]:
    if isinstance(value, str | bytes):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a JSON object")
    return MappingProxyType(dict(value))
