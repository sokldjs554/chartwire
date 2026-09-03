"""Registry: decorator defaults, duplicate protection, lookup, ordering, validation."""

from __future__ import annotations

import pytest

from chartwire.outbox import registry as registry_module
from chartwire.outbox.context import HandlerContext, OutboxEvent
from chartwire.outbox.registry import (
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    KNOWN_EVENT_TYPES,
    DuplicateHandlerError,
    HandlerSpec,
    Registry,
    RegistryError,
    UnknownEventTypeError,
)


async def noop(ctx: HandlerContext, event: OutboxEvent) -> None:
    return None


@pytest.fixture
def reg() -> Registry:
    return Registry()


def test_decorator_registers_with_spec_defaults_and_returns_fn(reg: Registry) -> None:
    @reg.handler("session.transcribed")
    async def note_draft(ctx: HandlerContext, event: OutboxEvent) -> None:
        return None

    spec = reg.get("session.transcribed")
    assert spec.fn is note_draft
    assert spec.name == "note_draft"
    assert (spec.lease_s, spec.max_attempts) == (DEFAULT_LEASE_S, DEFAULT_MAX_ATTEMPTS) == (60, 8)
    assert "session.transcribed" in reg
    assert len(reg) == 1


def test_decorator_overrides(reg: Registry) -> None:
    reg.handler("purge.requested", lease_s=300, max_attempts=3, name="purge_run")(noop)
    spec = reg.get("purge.requested")
    assert (spec.name, spec.lease_s, spec.max_attempts) == ("purge_run", 300, 3)


def test_duplicate_registration_raises_and_keeps_first(reg: Registry) -> None:
    reg.handler("consent.revoked", name="purge_run")(noop)
    with pytest.raises(DuplicateHandlerError) as exc:
        reg.handler("consent.revoked", name="other")(noop)
    assert exc.value.event_type == "consent.revoked"
    assert exc.value.existing == "purge_run"
    assert reg.get("consent.revoked").name == "purge_run"
    assert isinstance(exc.value, RegistryError)


def test_unknown_event_type(reg: Registry) -> None:
    with pytest.raises(UnknownEventTypeError) as exc:
        reg.get("nope.never")
    assert exc.value.event_type == "nope.never"
    assert isinstance(exc.value, LookupError)
    assert reg.lookup("nope.never") is None
    assert "nope.never" not in reg


def test_all_is_sorted_by_event_type_and_iterable(reg: Registry) -> None:
    for event_type in ("purge.completed", "consent.revoked", "session.transcribed"):
        reg.handler(event_type)(noop)
    assert [s.event_type for s in reg.all()] == ["consent.revoked", "purge.completed", "session.transcribed"]
    assert [s.event_type for s in reg] == [s.event_type for s in reg.all()]
    assert reg.event_types() == {"consent.revoked", "purge.completed", "session.transcribed"}
    reg.clear()
    assert len(reg) == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"event_type": "Session.Transcribed"}, "invalid event_type"),
        ({"event_type": "nodot"}, "invalid event_type"),
        ({"event_type": "a.b.c"}, "invalid event_type"),
        ({"name": "Bad-Name"}, "invalid handler name"),
        ({"lease_s": 0}, "lease_s"),
        ({"max_attempts": 0}, "max_attempts"),
    ],
)
def test_spec_validation(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "event_type": "session.transcribed",
        "name": "noop",
        "fn": noop,
        "lease_s": 60,
        "max_attempts": 8,
    }
    with pytest.raises(RegistryError, match=message):
        HandlerSpec(**{**base, **kwargs})  # type: ignore[arg-type]


def test_known_event_types_match_spec_table() -> None:
    assert {
        "session.transcribed",
        "consent.revoked",
        "purge.requested",
        "purge.completed",
    } == KNOWN_EVENT_TYPES


def test_module_level_handler_is_bound_to_process_registry() -> None:
    assert registry_module.handler.__self__ is registry_module.REGISTRY  # type: ignore[attr-defined]


async def test_registered_handler_is_awaitable(reg: Registry) -> None:
    calls: list[int] = []

    @reg.handler("purge.completed")
    async def purge_verify(ctx: HandlerContext, event: OutboxEvent) -> None:
        calls.append(event.id)

    spec = reg.get("purge.completed")
    ctx = HandlerContext(None, None, None, None, None, None, None)  # type: ignore[arg-type]
    fake_event = type("E", (), {"id": 7})()
    await spec.fn(ctx, fake_event)  # type: ignore[arg-type]
    assert calls == [7]
