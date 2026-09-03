"""JSON control messages of the WebSocket protocol (spec §6.3).

Every message is ``{"t": "<kind>", ...}``. Models are pydantic v2 with ``extra='forbid'`` so an
unknown field is a protocol error, never silently ignored. :func:`parse_client_message` and
:func:`parse_server_message` dispatch on ``t`` for one direction and one endpoint kind;
:func:`dump` renders a model to the plain ``dict`` the shell serialises with ``orjson``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import orjson
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from chartwire.ws.actions import Ack, CloseCode, Nack, Send, SendCredit

Kind = Literal["ingest", "watch"]
SeqRange = tuple[Annotated[int, Field(ge=1)], Annotated[int, Field(ge=1)]]
Speaker = Literal["clinician", "patient", "unknown"]
SessionState = Literal[
    "created", "recording", "paused", "ended", "transcribed", "drafted", "signed", "purging", "purged"
]
ByeReason = Literal["drain", "superseded", "ended", "consent_revoked"]
PauseReason = Literal["stt_lag", "storage"]

HEARTBEAT_MS = 15_000


class MessageError(ValueError):
    """Raised for undecodable JSON, unknown ``t`` or a failed field validation."""

    close_code = CloseCode.BAD_HELLO

    def __init__(self, detail: str, *, close_code: CloseCode | None = None) -> None:
        super().__init__(detail)
        if close_code is not None:
            self.close_code = close_code


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- recorder → server -------------------------------------------------------------------


class Hello(Message):
    t: Literal["hello"] = "hello"
    ticket: str = Field(min_length=1, max_length=256)
    proto: Literal[1] = 1
    codec: Literal["pcm16le"] = "pcm16le"
    sample_rate: int = Field(default=16_000, ge=8_000, le=48_000)
    chunk_ms: int = Field(default=200, ge=20, le=1_000)
    resume: bool = False
    last_sent_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _resume_needs_last_sent_seq(self) -> Hello:
        if self.resume and self.last_sent_seq is None:
            raise ValueError("resume=true requires last_sent_seq")
        return self


class End(Message):
    t: Literal["end"] = "end"
    final_seq: int = Field(ge=0)


class Pause(Message):
    t: Literal["pause"] = "pause"


class ResumeRec(Message):
    t: Literal["resume_rec"] = "resume_rec"


class Pong(Message):
    t: Literal["pong"] = "pong"
    ts: int = Field(ge=0)


# --- viewer → server ---------------------------------------------------------------------


class WatchHello(Message):
    t: Literal["hello"] = "hello"
    ticket: str = Field(min_length=1, max_length=256)
    from_seq: int | None = Field(default=None, ge=0)


class RiskAckRequest(Message):
    t: Literal["risk.ack"] = "risk.ack"
    risk_event_id: UUID


# --- server → recorder -------------------------------------------------------------------


class Welcome(Message):
    t: Literal["welcome"] = "welcome"
    session_id: str
    epoch: int = Field(ge=0)
    ack_seq: int = Field(ge=0)
    credit: int = Field(ge=0)
    heartbeat_ms: int = HEARTBEAT_MS
    missing: list[SeqRange] = Field(default_factory=list)


class AckMsg(Message):
    t: Literal["ack"] = "ack"
    ack_seq: int = Field(ge=0)
    credit: int = Field(ge=0)


class NackMsg(Message):
    t: Literal["nack"] = "nack"
    missing: list[SeqRange] = Field(min_length=1)


class CreditMsg(Message):
    t: Literal["credit"] = "credit"
    credit: int = Field(ge=0)


class PauseNotice(Message):
    t: Literal["pause"] = "pause"
    reason: PauseReason
    retry_ms: int = Field(ge=0)


class ErrorMsg(Message):
    t: Literal["error"] = "error"
    code: int
    message: str
    retryable: bool = False


class Ping(Message):
    t: Literal["ping"] = "ping"
    ts: int = Field(ge=0)


class Bye(Message):
    t: Literal["bye"] = "bye"
    reason: ByeReason
    ack_seq: int | None = Field(default=None, ge=0)


# --- server → viewer (transcript/risk/state events are also mirrored to the recorder) ----


class WatchWelcome(Message):
    t: Literal["welcome"] = "welcome"
    session_id: str
    state: SessionState
    last_final_seq: int = Field(ge=-1)


class TranscriptPartial(Message):
    t: Literal["transcript.partial"] = "transcript.partial"
    from_seq: int = Field(ge=0)
    text: str


class TranscriptFinal(Message):
    t: Literal["transcript.final"] = "transcript.final"
    seq: int = Field(ge=0)
    speaker: Speaker
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=0)
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    segment_id: int
    committed_at: str | None = None


class RiskAlert(Message):
    t: Literal["risk.alert"] = "risk.alert"
    risk_event_id: UUID
    category: str
    severity: int = Field(ge=1, le=3)
    segment_seq: int = Field(ge=0)
    span: tuple[int, int]
    sla_deadline_at: str | None = None
    committed_at: str | None = None


class RiskAckEvent(Message):
    t: Literal["risk.ack"] = "risk.ack"
    risk_event_id: UUID
    by: UUID


class RiskEscalated(Message):
    t: Literal["risk.escalated"] = "risk.escalated"
    risk_event_id: UUID


class SessionStateMsg(Message):
    t: Literal["session.state"] = "session.state"
    state: SessionState


class NoteStatus(Message):
    t: Literal["note.status"] = "note.status"
    note_id: UUID
    status: str
    coverage: float = Field(ge=0.0, le=1.0)
    unsupported_count: int = Field(ge=0)


class ViewerPresence(Message):
    t: Literal["viewer.presence"] = "viewer.presence"
    count: int = Field(ge=0)


class ViewerLagged(Message):
    t: Literal["viewer.lagged"] = "viewer.lagged"
    dropped_partials: int = Field(ge=0)


class ViewerDegraded(Message):
    t: Literal["viewer.degraded"] = "viewer.degraded"


# --- dispatch ----------------------------------------------------------------------------

IngestClientMessage = Annotated[Hello | End | Pause | ResumeRec | Pong, Field(discriminator="t")]
WatchClientMessage = Annotated[WatchHello | RiskAckRequest | Pong, Field(discriminator="t")]
IngestServerMessage = Annotated[
    Welcome
    | AckMsg
    | NackMsg
    | CreditMsg
    | PauseNotice
    | TranscriptFinal
    | RiskAlert
    | SessionStateMsg
    | ErrorMsg
    | Ping
    | Bye,
    Field(discriminator="t"),
]
WatchServerMessage = Annotated[
    WatchWelcome
    | TranscriptPartial
    | TranscriptFinal
    | RiskAlert
    | RiskAckEvent
    | RiskEscalated
    | SessionStateMsg
    | NoteStatus
    | ViewerPresence
    | ViewerLagged
    | ViewerDegraded
    | ErrorMsg
    | Ping
    | Bye,
    Field(discriminator="t"),
]

_CLIENT: dict[str, TypeAdapter[Any]] = {
    "ingest": TypeAdapter(IngestClientMessage),
    "watch": TypeAdapter(WatchClientMessage),
}
_SERVER: dict[str, TypeAdapter[Any]] = {
    "ingest": TypeAdapter(IngestServerMessage),
    "watch": TypeAdapter(WatchServerMessage),
}


def _parse(adapter: TypeAdapter[Any], raw: bytes | str | dict[str, Any]) -> Message:
    if isinstance(raw, dict):
        obj: Any = raw
    else:
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError as e:
            raise MessageError(f"invalid json: {e.msg}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise MessageError("message must be an object with a string 't'")
    try:
        model: Message = adapter.validate_python(obj)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "t"
        raise MessageError(f"{loc}: {first['msg']}") from None
    return model


def parse_client_message(kind: Kind, raw: bytes | str | dict[str, Any]) -> Message:
    """Parse a text frame received from a recorder (``ingest``) or a viewer (``watch``)."""
    return _parse(_CLIENT[kind], raw)


def parse_server_message(kind: Kind, raw: bytes | str | dict[str, Any]) -> Message:
    """Parse a server → client message (used by the protocol clients and the tests)."""
    return _parse(_SERVER[kind], raw)


def dump(msg: Message) -> dict[str, Any]:
    """Plain JSON-compatible ``dict`` (UUIDs as strings, ``None`` fields omitted)."""
    return msg.model_dump(mode="json", exclude_none=True)


def render(action: Ack | Nack | SendCredit | Send) -> dict[str, Any]:
    """Message body for a core action that maps to exactly one JSON message."""
    if isinstance(action, Ack):
        return dump(AckMsg(ack_seq=action.ack_seq, credit=action.credit))
    if isinstance(action, Nack):
        return dump(NackMsg(missing=list(action.missing)))
    if isinstance(action, SendCredit):
        return dump(CreditMsg(credit=action.credit))
    return action.msg
