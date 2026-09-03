"""STT adapter contract (spec §3.1).

Everything the stt-worker (§7.4) needs from a speech-to-text backend is expressed with the
five types below. Adapters are pure asyncio objects: ``open()`` returns a per-session stream,
``feed()`` consumes one audio chunk and returns whatever events became available, ``flush()``
closes the stream and returns the remaining events. No adapter touches PostgreSQL or Redis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

Speaker = Literal["clinician", "patient", "unknown"]


@dataclass(frozen=True)
class Chunk:
    """One audio chunk as received on the ingest WebSocket (§6.2 header + payload)."""

    session_id: UUID
    seq: int
    offset_ms: int
    flags: int
    payload: bytes


@dataclass(frozen=True)
class Partial:
    """Interim hypothesis for the utterance in progress; never persisted (§7.4)."""

    from_seq: int
    text: str


@dataclass(frozen=True)
class Final:
    """A finished utterance; persisted as one ``transcript_segments`` row (§7.4)."""

    seq_start: int
    seq_end: int
    speaker: Speaker
    t_start_ms: int
    t_end_ms: int
    text: str
    confidence: float


SttEvent = Partial | Final


@dataclass(frozen=True)
class SessionInfo:
    """The subset of ``sessions`` an adapter needs to open a stream."""

    session_id: UUID
    tenant_id: UUID
    script_ref: str | None
    chunk_ms: int = 200
    started_at: datetime | None = None


class SttStream(Protocol):
    async def feed(self, chunk: Chunk) -> list[SttEvent]:
        """Consume one chunk; return the events it produced (possibly none)."""
        ...

    async def flush(self) -> list[SttEvent]:
        """End of audio: return every event still pending. The stream is unusable afterwards."""
        ...


class SttAdapter(Protocol):
    async def open(self, session: SessionInfo) -> SttStream:
        """Start a stream for one session."""
        ...
