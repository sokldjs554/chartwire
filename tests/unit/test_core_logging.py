import json

import structlog

from chartwire.core import logging as cw_logging


def test_redacts_phi_keys_patterns_and_nested_structures():
    event = {
        "event": "segment.final",
        "text": "죽고 싶어요",
        "phone": "x",
        "meta": {"name": "가상환자-0001", "count": 3, "ids": [{"token": "t"}, "010-1234-5678"]},
        "rrn": "900101-1234567",
        "ok": "call 02-123-4567 later",
        "n": 7,
    }
    out = cw_logging.redact_phi(None, "info", event)
    assert out["text"] == out["phone"] == cw_logging.REDACTED
    assert out["meta"]["name"] == cw_logging.REDACTED and out["meta"]["count"] == 3
    assert out["meta"]["ids"] == [{"token": cw_logging.REDACTED}, cw_logging.REDACTED]
    assert out["rrn"] == cw_logging.REDACTED and out["ok"] == cw_logging.REDACTED
    assert out["event"] == "segment.final" and out["n"] == 7
    assert event["text"] == "죽고 싶어요"  # input not mutated


def test_configure_emits_json_with_bound_context(capsys):
    cw_logging.configure("INFO")
    cw_logging.bind_context(request_id="r1", tenant_id="t1", node_id="n1")
    try:
        cw_logging.get_logger("test").info("hello", text="secret", session_id="s1")
    finally:
        cw_logging.clear_context()
        structlog.reset_defaults()
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["event"] == "hello" and line["request_id"] == "r1" and line["node_id"] == "n1"
    assert line["text"] == cw_logging.REDACTED and "secret" not in json.dumps(line)
