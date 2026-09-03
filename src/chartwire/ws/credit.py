"""Flow-control and liveness rules (spec §6.4 rules 3 and 5).

``credit`` is the number of chunks the recorder may have outstanding (sent but not yet
covered by a cumulative ``ack``). The server recomputes it every tick from the STT lag of
the session and the ledger backlog of the node, advertises it with each ``ack`` and, when
it moved by more than 25 %, immediately in a ``credit{}`` message. :class:`Heartbeat` is the
server-side ping timer shared by recorder and viewer connections.
"""

from __future__ import annotations

from chartwire.ws.actions import Action, CloseCode, Send, close

MAX_CREDIT = 100
TOLERANCE = 20
"""Chunks a recorder may exceed the advertised credit by before the server closes 4009."""
LOW_WATER = 10
"""Below this credit the server acks after every ledger commit instead of batching."""
ZERO_PAUSE_MS = 2_000
"""``credit == 0`` for longer than this → ``pause{reason:'stt_lag'}``."""
RING_BUFFER_CHUNKS = 150
"""Recorder replay ring buffer (30 s of 200 ms chunks); a resume gap beyond it is unrecoverable."""
PING_EVERY_MS = 15_000
MAX_MISSED_PONGS = 2


def compute(base: int, stt_lag_chunks: int, node_pending_rows: int) -> int:
    """``clamp(base − stt_lag_chunks // 2 − node_pending_rows // 100, 0, MAX_CREDIT)``."""
    if base < 0 or stt_lag_chunks < 0 or node_pending_rows < 0:
        raise ValueError("credit inputs must be non-negative")
    value = base - stt_lag_chunks // 2 - node_pending_rows // 100
    return max(0, min(MAX_CREDIT, value))


def changed_significantly(advertised: int, current: int) -> bool:
    """True when ``current`` differs from ``advertised`` by more than 25 % of ``advertised``.

    An advertised credit of 0 counts as 1 so that any recovery from 0 is announced at once.
    """
    return abs(current - advertised) * 4 > max(advertised, 1)


def violates(outstanding: int, advertised: int) -> bool:
    """Server-side rule: ``outstanding > advertised + TOLERANCE`` is a credit violation (4009)."""
    return outstanding > advertised + TOLERANCE


class Heartbeat:
    """Server ``ping`` every 15 s; a ping falling due while two are still unanswered → close ``4000``."""

    def __init__(self, now_ms: int) -> None:
        self.last_ping_ms, self.unanswered = now_ms, 0

    def tick(self, now_ms: int) -> list[Action]:
        if now_ms - self.last_ping_ms < PING_EVERY_MS:
            return []
        if self.unanswered >= MAX_MISSED_PONGS:
            return [close(CloseCode.HEARTBEAT_TIMEOUT)]
        self.last_ping_ms, self.unanswered = now_ms, self.unanswered + 1
        return [Send({"t": "ping", "ts": now_ms})]

    def pong(self) -> None:
        self.unanswered = 0
