"""Action dataclasses returned by the sans-I/O cores and the WebSocket close codes.

The cores in :mod:`chartwire.ws.core` never touch a socket, Redis or PostgreSQL.
They return a list of these actions and the async shells (Phase 1: ``ingest.py`` /
``watch.py``) translate each one into I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class CloseCode(IntEnum):
    """WebSocket close codes of the chartwire protocol (spec §6.8)."""

    ENDED = 1000
    SERVICE_RESTART = 1012
    HEARTBEAT_TIMEOUT = 4000
    UNAUTHORIZED = 4001
    FORBIDDEN = 4003
    SESSION_NOT_FOUND = 4004
    BAD_HELLO = 4005
    SEQ_GAP_UNRECOVERABLE = 4008
    CREDIT_VIOLATION = 4009
    BAD_FRAME = 4010
    CONSENT_MISSING = 4011
    SESSION_ENDED = 4012
    VIEWER_TOO_SLOW = 4013
    SUPERSEDED = 4409
    DEPENDENCY_UNAVAILABLE = 4503


CLOSE_REASONS: dict[CloseCode, str] = {
    CloseCode.ENDED: "ended",
    CloseCode.SERVICE_RESTART: "drain",
    CloseCode.HEARTBEAT_TIMEOUT: "heartbeat_timeout",
    CloseCode.UNAUTHORIZED: "unauthorized",
    CloseCode.FORBIDDEN: "forbidden",
    CloseCode.SESSION_NOT_FOUND: "session_not_found",
    CloseCode.BAD_HELLO: "bad_hello",
    CloseCode.SEQ_GAP_UNRECOVERABLE: "seq_gap_unrecoverable",
    CloseCode.CREDIT_VIOLATION: "credit_violation",
    CloseCode.BAD_FRAME: "bad_frame",
    CloseCode.CONSENT_MISSING: "consent_missing",
    CloseCode.SESSION_ENDED: "session_ended",
    CloseCode.VIEWER_TOO_SLOW: "viewer_too_slow",
    CloseCode.SUPERSEDED: "superseded",
    CloseCode.DEPENDENCY_UNAVAILABLE: "dependency_unavailable",
}

Range = tuple[int, int]
"""Inclusive ``(from, to)`` seq range as used by ``nack.missing`` / ``welcome.missing``."""


def ranges(seqs: Iterable[int]) -> list[Range]:
    """Collapse seqs (any order, duplicates allowed) into sorted inclusive ranges."""
    out: list[Range] = []
    for s in sorted(set(seqs)):
        if out and out[-1][1] == s - 1:
            out[-1] = (out[-1][0], s)
        else:
            out.append((s, s))
    return out


@dataclass(frozen=True, slots=True)
class Store:
    """Persist chunk ``seq`` (sha256 → encrypt → object store → XADD → ledger)."""

    seq: int
    offset_ms: int
    flags: int


@dataclass(frozen=True, slots=True)
class Ack:
    """Send ``ack{ack_seq, credit}``; ``ack_seq`` is always ledgered (durable)."""

    ack_seq: int
    credit: int


@dataclass(frozen=True, slots=True)
class Nack:
    """Send ``nack{missing}`` asking the recorder to re-send the listed ranges."""

    missing: tuple[Range, ...]


@dataclass(frozen=True, slots=True)
class SendCredit:
    """Send ``credit{credit}`` (credit changed by more than 25 %)."""

    credit: int


@dataclass(frozen=True, slots=True)
class Send:
    """Send an arbitrary JSON message (already a plain ``dict``)."""

    msg: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Close:
    """Close the WebSocket with ``code`` and a short machine-readable ``reason``."""

    code: int
    reason: str


@dataclass(frozen=True, slots=True)
class Transition:
    """Persist a ``sessions.state`` change (``recording`` / ``ended``)."""

    state: str


@dataclass(frozen=True, slots=True)
class Rehydrate:
    """Rebuild ``sess:{sid}`` from PostgreSQL (Redis lost the hot state)."""


@dataclass(frozen=True, slots=True)
class Subscribe:
    """Subscribe the viewer to ``sess:{sid}:events`` — always before ``Replay``."""


@dataclass(frozen=True, slots=True)
class Replay:
    """Replay committed finals with ``seq > after_seq`` from PostgreSQL."""

    after_seq: int


Action = Store | Ack | Nack | SendCredit | Send | Close | Transition | Rehydrate | Subscribe | Replay


def close(code: CloseCode, reason: str | None = None) -> Close:
    """Build a :class:`Close` with the canonical reason unless a more specific one is given."""
    return Close(int(code), reason or CLOSE_REASONS[code])


def error_msg(code: CloseCode, message: str, *, retryable: bool = False) -> dict[str, Any]:
    """Build the ``error{code, message, retryable}`` message body."""
    return {"t": "error", "code": int(code), "message": message, "retryable": retryable}


@dataclass(slots=True)
class IngestStats:
    """Per-connection counters exposed for Prometheus by the shell (never logged with payloads)."""

    received: int = 0
    stored: int = 0
    duplicates: int = 0
    reordered: int = 0
    dropped: int = 0
    nacks: int = 0
    acks: int = 0
