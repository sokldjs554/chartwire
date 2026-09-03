"""JSON message models: every §6.3 message parses and dumps, extra fields are refused, dispatch works."""

from uuid import UUID

import orjson
import pytest

from chartwire.ws import messages as m
from chartwire.ws.actions import Ack, CloseCode, Nack, Send, SendCredit

RID = 41  # risk_events.id is a bigint identity
UID = UUID("00000000-0000-0000-0000-000000000002")

RECORDER_TO_SERVER = [
    ({"t": "hello", "ticket": "tk", "proto": 1, "codec": "pcm16le", "sample_rate": 16000, "chunk_ms": 200, "resume": False}, m.Hello),
    ({"t": "hello", "ticket": "tk", "resume": True, "last_sent_seq": 42}, m.Hello),
    ({"t": "end", "final_seq": 10}, m.End),
    ({"t": "pause"}, m.Pause),
    ({"t": "resume_rec"}, m.ResumeRec),
    ({"t": "pong", "ts": 123}, m.Pong),
]  # fmt: skip
VIEWER_TO_SERVER = [
    ({"t": "hello", "ticket": "tk"}, m.WatchHello),
    ({"t": "hello", "ticket": "tk", "from_seq": 12}, m.WatchHello),
    ({"t": "risk.ack", "risk_event_id": RID}, m.RiskAckRequest),
    ({"t": "pong", "ts": 5}, m.Pong),
]
SERVER_TO_RECORDER = [
    ({"t": "welcome", "session_id": "s", "epoch": 3, "ack_seq": 0, "credit": 50, "heartbeat_ms": 15000, "missing": [[1, 4], [7, 7]]}, m.Welcome),
    ({"t": "ack", "ack_seq": 8, "credit": 50}, m.AckMsg),
    ({"t": "nack", "missing": [[3, 5]]}, m.NackMsg),
    ({"t": "credit", "credit": 12}, m.CreditMsg),
    ({"t": "pause", "reason": "stt_lag", "retry_ms": 2000}, m.PauseNotice),
    ({"t": "transcript.final", "seq": 0, "speaker": "patient", "t_start_ms": 0, "t_end_ms": 900, "text": "잠을 못 자요", "confidence": 0.9, "segment_id": 1}, m.TranscriptFinal),
    ({"t": "risk.alert", "risk_event_id": RID, "category": "self_harm", "severity": 3, "segment_seq": 4, "span": [2, 9], "sla_deadline_at": "2026-09-02T00:01:00+00:00"}, m.RiskAlert),
    ({"t": "session.state", "state": "ended"}, m.SessionStateMsg),
    ({"t": "error", "code": 4503, "message": "redis", "retryable": True}, m.ErrorMsg),
    ({"t": "ping", "ts": 1}, m.Ping),
    ({"t": "bye", "reason": "drain", "ack_seq": 3}, m.Bye),
]  # fmt: skip
SERVER_TO_VIEWER = [
    ({"t": "welcome", "session_id": "s", "state": "recording", "last_final_seq": -1}, m.WatchWelcome),
    ({"t": "transcript.partial", "from_seq": 3, "text": "잠"}, m.TranscriptPartial),
    ({"t": "transcript.final", "seq": 1, "speaker": "clinician", "t_start_ms": 0, "t_end_ms": 1, "text": "네", "confidence": 1.0, "segment_id": 2, "committed_at": "2026-09-02T00:00:00+00:00"}, m.TranscriptFinal),
    ({"t": "risk.alert", "risk_event_id": RID, "category": "self_harm", "severity": 2, "segment_seq": 1, "span": [0, 1]}, m.RiskAlert),
    ({"t": "risk.ack", "risk_event_id": RID, "by": str(UID)}, m.RiskAckEvent),
    ({"t": "risk.escalated", "risk_event_id": RID}, m.RiskEscalated),
    ({"t": "session.state", "state": "transcribed"}, m.SessionStateMsg),
    ({"t": "note.status", "note_id": str(UID), "status": "drafted", "coverage": 0.8, "unsupported_count": 1}, m.NoteStatus),
    ({"t": "viewer.presence", "count": 2}, m.ViewerPresence),
    ({"t": "viewer.lagged", "dropped_partials": 7}, m.ViewerLagged),
    ({"t": "viewer.degraded"}, m.ViewerDegraded),
    ({"t": "ping", "ts": 9}, m.Ping),
    ({"t": "bye", "reason": "ended"}, m.Bye),
]  # fmt: skip


@pytest.mark.parametrize(("raw", "model"), RECORDER_TO_SERVER)
def test_recorder_messages_parse_and_roundtrip(raw, model):
    msg = m.parse_client_message("ingest", orjson.dumps(raw))
    assert isinstance(msg, model)
    assert m.parse_client_message("ingest", m.dump(msg)) == msg


@pytest.mark.parametrize(("raw", "model"), VIEWER_TO_SERVER)
def test_viewer_messages_parse_and_roundtrip(raw, model):
    msg = m.parse_client_message("watch", raw)
    assert isinstance(msg, model)
    assert m.parse_client_message("watch", orjson.dumps(m.dump(msg))) == msg


@pytest.mark.parametrize(("raw", "model"), SERVER_TO_RECORDER)
def test_server_to_recorder_messages(raw, model):
    msg = m.parse_server_message("ingest", raw)
    assert isinstance(msg, model)
    dumped = m.dump(msg)
    assert dumped["t"] == raw["t"]
    assert m.parse_server_message("ingest", dumped) == msg


@pytest.mark.parametrize(("raw", "model"), SERVER_TO_VIEWER)
def test_server_to_viewer_messages(raw, model):
    msg = m.parse_server_message("watch", raw)
    assert isinstance(msg, model)
    assert m.parse_server_message("watch", m.dump(msg)) == msg


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("ingest", {"t": "hello", "ticket": "tk", "extra": 1}),
        ("ingest", {"t": "end", "final_seq": 1, "x": 0}),
        ("watch", {"t": "hello", "ticket": "tk", "proto": 1}),  # recorder-only field on a viewer hello
        ("watch", {"t": "pong", "ts": 1, "y": 2}),
    ],
)
def test_extra_fields_are_forbidden(kind, raw):
    with pytest.raises(m.MessageError):
        m.parse_client_message(kind, raw)


@pytest.mark.parametrize(
    ("kind", "raw", "fragment"),
    [
        ("ingest", b"{not json", "invalid json"),
        ("ingest", b"[]", "object"),
        ("ingest", {"t": 1}, "string 't'"),
        ("ingest", {"t": "nope"}, "t"),
        ("ingest", {"t": "hello", "ticket": "tk", "proto": 2}, "proto"),
        ("ingest", {"t": "hello", "ticket": "tk", "codec": "opus"}, "codec"),
        ("ingest", {"t": "hello", "ticket": "tk", "resume": True}, "last_sent_seq"),
        ("ingest", {"t": "hello", "ticket": ""}, "ticket"),
        ("ingest", {"t": "end", "final_seq": -1}, "final_seq"),
        ("watch", {"t": "end", "final_seq": 1}, "t"),  # recorder message on the viewer endpoint
        ("watch", {"t": "risk.ack", "risk_event_id": "not-an-id"}, "risk_event_id"),
    ],
)
def test_invalid_client_messages_raise_message_error(kind, raw, fragment):
    with pytest.raises(m.MessageError) as info:
        m.parse_client_message(kind, raw)
    assert fragment in str(info.value)
    assert info.value.close_code is CloseCode.BAD_HELLO


def test_message_error_close_code_override():
    err = m.MessageError("x", close_code=CloseCode.BAD_FRAME)
    assert err.close_code is CloseCode.BAD_FRAME


def test_server_message_validation_errors():
    with pytest.raises(m.MessageError):
        m.parse_server_message("ingest", {"t": "nack", "missing": []})
    with pytest.raises(m.MessageError):
        m.parse_server_message("ingest", {"t": "bye", "reason": "because"})
    with pytest.raises(m.MessageError):
        m.parse_server_message(
            "watch",
            {
                "t": "risk.alert",
                "risk_event_id": RID,
                "category": "c",
                "severity": 4,
                "segment_seq": 0,
                "span": [0, 1],
            },
        )


def test_dump_omits_none_and_stringifies_uuids():
    assert m.dump(m.Bye(reason="ended")) == {"t": "bye", "reason": "ended"}
    assert m.dump(m.RiskEscalated(risk_event_id=RID)) == {"t": "risk.escalated", "risk_event_id": RID}
    assert orjson.dumps(m.dump(m.Welcome(session_id="s", epoch=1, ack_seq=0, credit=50)))


def test_models_are_frozen():
    msg = m.End(final_seq=1)
    with pytest.raises(Exception, match="frozen"):
        msg.final_seq = 2  # type: ignore[misc]


def test_render_maps_core_actions_to_messages():
    assert m.render(Ack(5, 40)) == {"t": "ack", "ack_seq": 5, "credit": 40}
    assert m.render(Nack(((2, 3), (7, 7)))) == {"t": "nack", "missing": [[2, 3], [7, 7]]}
    assert m.render(SendCredit(0)) == {"t": "credit", "credit": 0}
    assert m.render(Send({"t": "ping", "ts": 1})) == {"t": "ping", "ts": 1}
    for rendered in (m.render(Ack(1, 1)), m.render(Nack(((1, 1),))), m.render(SendCredit(3))):
        m.parse_server_message("ingest", rendered)
