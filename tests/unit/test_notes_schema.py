"""§9.1: the output schema has no place for a verdict, and unknown keys are rejected."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from chartwire.notes.schema import DraftSchemaError, Evidence, NoteDraftOut, Statement, parse_draft

GOOD = {
    "statements": [
        {
            "section": "S",
            "text": "입맛이 없다고 함",
            "evidence": [{"seq": 4, "quote": "입맛이 없어요"}],
            "kind": "reported",
        }
    ]
}


def test_valid_draft_parses():
    d = parse_draft(json.dumps(GOOD))
    assert d.statements[0].section == "S" and d.abstain is False and d.abstain_reason is None


@pytest.mark.parametrize(
    "extra",
    ["assessment", "diagnosis", "icd", "dsm", "risk_level", "medication_recommendation", "verdict"],
)
def test_forbidden_top_level_keys_reject(extra: str):
    with pytest.raises(DraftSchemaError) as exc:
        parse_draft(json.dumps({**GOOD, extra: "주요우울장애"}))
    assert "extra_forbidden" in str(exc.value)
    assert "주요우울장애" not in str(exc.value)  # never echo the offending value


def test_extra_keys_inside_statement_and_evidence_reject():
    st = {**GOOD["statements"][0], "verdict": "supported"}
    with pytest.raises(DraftSchemaError):
        parse_draft(json.dumps({"statements": [st]}))
    ev = {**GOOD["statements"][0], "evidence": [{"seq": 4, "quote": "입맛이 없어요", "score": 0.9}]}
    with pytest.raises(DraftSchemaError):
        parse_draft(json.dumps({"statements": [ev]}))


def test_non_json_and_wrong_shape_reject():
    with pytest.raises(DraftSchemaError):
        parse_draft("not json at all")
    with pytest.raises(DraftSchemaError):
        parse_draft(json.dumps({"statements": "S: 입맛이 없다고 함"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [("section", "A"), ("kind", "assessment"), ("text", "x" * 201), ("evidence", [])],
)
def test_statement_limits(field: str, value: object):
    with pytest.raises(ValidationError):
        Statement.model_validate({**GOOD["statements"][0], field: value})


def test_evidence_quote_length_and_count_limits():
    with pytest.raises(ValidationError):
        Evidence(seq=1, quote="짧다")
    with pytest.raises(ValidationError):
        Evidence(seq=1, quote="가" * 201)
    five = [{"seq": 4, "quote": "입맛이 없어요"}] * 5
    with pytest.raises(ValidationError):
        Statement.model_validate({**GOOD["statements"][0], "evidence": five})


def test_statement_count_limit_is_36():
    many = {"statements": [GOOD["statements"][0]] * 37}
    with pytest.raises(DraftSchemaError):
        parse_draft(json.dumps(many))
    assert len(NoteDraftOut.model_validate({"statements": [GOOD["statements"][0]] * 36}).statements) == 36


def test_json_schema_has_no_verdict_like_property():
    schema = json.dumps(NoteDraftOut.model_json_schema())
    for word in ("assessment", "diagnosis", "icd", "dsm", "risk_level", "recommendation", "verdict"):
        assert word not in schema
    assert '"additionalProperties": false' in schema
