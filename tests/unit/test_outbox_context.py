"""OutboxEvent.from_row coercion + immutability; HandlerContext is frozen; Protocols are structural."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest

from chartwire.outbox.context import HandlerContext, KekProvider, ObjectStore, OutboxEvent, OutboxStatus

CREATED = datetime(2026, 9, 2, 8, 30, tzinfo=UTC)


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 42,
        "tenant_id": uuid4(),
        "aggregate_type": "session",
        "aggregate_id": uuid4(),
        "event_type": "session.transcribed",
        "payload": {"session_id": "s", "patient_id": "p"},
        "idempotency_key": "session.transcribed:s:1",
        "attempts": 0,
        "created_at": CREATED,
        "status": "in_flight",
        "locked_by": "worker-1",
    }
    return {**base, **overrides}


def test_from_row_keeps_typed_values_and_ignores_unknown_columns() -> None:
    r = row()
    ev = OutboxEvent.from_row(r)
    assert (ev.id, ev.tenant_id, ev.aggregate_id) == (42, r["tenant_id"], r["aggregate_id"])
    assert ev.event_type == "session.transcribed"
    assert ev.payload == {"session_id": "s", "patient_id": "p"}
    assert ev.attempts == 0 and ev.created_at == CREATED


def test_from_row_coerces_strings_from_drivers() -> None:
    tid, aid = uuid4(), uuid4()
    ev = OutboxEvent.from_row(
        row(id="7", tenant_id=str(tid), aggregate_id=str(aid), payload=json.dumps({"k": 1}))
    )
    assert ev.id == 7 and ev.tenant_id == tid and ev.aggregate_id == aid and ev.payload == {"k": 1}
    ev2 = OutboxEvent.from_row(row(payload=b'{"k": 2}'))
    assert ev2.payload == {"k": 2}


def test_payload_is_read_only_and_decoupled_from_source_dict() -> None:
    src = row()
    ev = OutboxEvent.from_row(src)
    assert isinstance(ev.payload, MappingProxyType)
    src["payload"]["session_id"] = "changed"
    assert ev.payload["session_id"] == "s"
    with pytest.raises(TypeError):
        ev.payload["x"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "exc"),
    [
        ({"created_at": CREATED.replace(tzinfo=None)}, ValueError),
        ({"created_at": "2026-09-02"}, ValueError),
        ({"tenant_id": 123}, TypeError),
        ({"payload": [1, 2]}, TypeError),
        ({"payload": "[1]"}, TypeError),
    ],
)
def test_from_row_rejects_bad_shapes(overrides: dict[str, Any], exc: type[Exception]) -> None:
    with pytest.raises(exc):
        OutboxEvent.from_row(row(**overrides))


def test_event_and_context_are_frozen() -> None:
    ev = OutboxEvent.from_row(row())
    with pytest.raises(AttributeError):
        ev.attempts = 3  # type: ignore[misc]
    ctx = HandlerContext(
        engine=1, redis=2, objectstore=None, clock=None, settings=None, kek=None, keycache=None
    )  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        ctx.engine = 9  # type: ignore[misc]


def test_status_values_match_check_constraint() -> None:
    assert {s.value for s in OutboxStatus} == {"pending", "in_flight", "done", "dead"}
    assert OutboxStatus.DEAD == "dead"


class _Store:
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes:
        return b""

    def delete_prefix(self, prefix: str) -> int:
        return 0

    def list(self, prefix: str) -> list[str]:
        return []


class _Kek:
    def wrap(self, dek: bytes, kek_ref: str) -> bytes:
        return dek

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes:
        return wrapped


def test_protocols_are_structural() -> None:
    store: ObjectStore = _Store()
    kek: KekProvider = _Kek()
    ctx = HandlerContext(None, None, store, None, None, kek, None)  # type: ignore[arg-type]
    assert ctx.objectstore.list("t/") == []
    assert ctx.kek.unwrap(b"x", "ref") == b"x"
    assert isinstance(UUID(int=0), UUID)
