"""Handler registry for outbox events (§7.1–§7.2).

One handler per ``event_type``. The registry is the only place the poller looks up *what* to run,
*how long* the claim lease is and *how many* attempts are allowed before a row goes to the DLQ.
Handler names are the ``processed_events.handler`` key, so they must be stable across deploys.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

from chartwire.outbox.context import HandlerContext, OutboxEvent

Handler = Callable[[HandlerContext, OutboxEvent], Awaitable[None]]

DEFAULT_LEASE_S = 60
DEFAULT_MAX_ATTEMPTS = 8

SESSION_TRANSCRIBED = "session.transcribed"
CONSENT_REVOKED = "consent.revoked"
PURGE_REQUESTED = "purge.requested"
PURGE_COMPLETED = "purge.completed"
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {SESSION_TRANSCRIBED, CONSENT_REVOKED, PURGE_REQUESTED, PURGE_COMPLETED}
)
"""Event types with a fixed payload contract (§15). Registration is not limited to these."""

_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RegistryError(ValueError):
    """Invalid registration (bad event type, bad limits, duplicate)."""


class DuplicateHandlerError(RegistryError):
    def __init__(self, event_type: str, existing: str) -> None:
        super().__init__(f"event_type {event_type!r} already handled by {existing!r}")
        self.event_type = event_type
        self.existing = existing


class UnknownEventTypeError(LookupError):
    def __init__(self, event_type: str) -> None:
        super().__init__(f"no handler registered for event_type {event_type!r}")
        self.event_type = event_type


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    event_type: str
    name: str
    fn: Handler
    lease_s: int
    max_attempts: int

    def __post_init__(self) -> None:
        if not _EVENT_TYPE_RE.match(self.event_type):
            raise RegistryError(f"invalid event_type {self.event_type!r} (expected 'noun.verb')")
        if not _NAME_RE.match(self.name):
            raise RegistryError(f"invalid handler name {self.name!r}")
        if self.lease_s <= 0:
            raise RegistryError("lease_s must be positive")
        if self.max_attempts < 1:
            raise RegistryError("max_attempts must be at least 1")


class Registry:
    """Mapping ``event_type -> HandlerSpec`` with duplicate protection."""

    def __init__(self) -> None:
        self._specs: dict[str, HandlerSpec] = {}

    def handler(
        self,
        event_type: str,
        *,
        lease_s: int = DEFAULT_LEASE_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        name: str | None = None,
    ) -> Callable[[Handler], Handler]:
        """Decorator: ``@registry.handler("session.transcribed", lease_s=120)``.

        The decorated coroutine function is returned unchanged; ``name`` defaults to its ``__name__``.
        """

        def decorate(fn: Handler) -> Handler:
            spec = HandlerSpec(
                event_type=event_type,
                name=name if name is not None else fn.__name__,
                fn=fn,
                lease_s=lease_s,
                max_attempts=max_attempts,
            )
            self.register(spec)
            return fn

        return decorate

    def register(self, spec: HandlerSpec) -> None:
        existing = self._specs.get(spec.event_type)
        if existing is not None:
            raise DuplicateHandlerError(spec.event_type, existing.name)
        self._specs[spec.event_type] = spec

    def get(self, event_type: str) -> HandlerSpec:
        try:
            return self._specs[event_type]
        except KeyError:
            raise UnknownEventTypeError(event_type) from None

    def lookup(self, event_type: str) -> HandlerSpec | None:
        return self._specs.get(event_type)

    def all(self) -> list[HandlerSpec]:
        """Registered specs ordered by event type (stable for logs, docs and `outbox stats`)."""
        return sorted(self._specs.values(), key=lambda s: s.event_type)

    def event_types(self) -> frozenset[str]:
        return frozenset(self._specs)

    def clear(self) -> None:
        self._specs.clear()

    def __contains__(self, event_type: object) -> bool:
        return event_type in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[HandlerSpec]:
        return iter(self.all())


REGISTRY = Registry()
"""Process-wide registry the worker loads handlers into (``worker/handlers/*`` import this)."""

handler = REGISTRY.handler
"""``@registry.handler(event_type, *, lease_s=60, max_attempts=8)`` — the §3.1 contract."""
