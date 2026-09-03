"""What an outbox handler receives: the claimed event row and the process-wide dependencies.

The concrete engine / redis / settings types live in other packages; they are typed as ``Any`` or as
minimal structural Protocols here so the outbox package stays importable on its own and mypy-strict.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID


class Clock(Protocol):
    """Structural twin of ``chartwire.core.clock.Clock`` (wall clock + monotonic)."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class ObjectStore(Protocol):
    """Structural twin of ``chartwire.objectstore.ObjectStore`` (§3.1)."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def list(self, prefix: str) -> list[str]: ...


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
