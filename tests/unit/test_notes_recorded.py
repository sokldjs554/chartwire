"""§9.2 RecordedProvider: every fixture runs through the identical parse → verify → decide path."""

from __future__ import annotations

from pathlib import Path

import pytest

from chartwire.notes.policy import MAX_SCHEMA_FAILURES, Decision, decide
from chartwire.notes.providers.recorded import Fixture, RecordedProvider, iter_fixtures
from chartwire.notes.schema import DraftContext, DraftSchemaError, NoteStatus, VerifiedDraft, parse_draft
from chartwire.notes.verifier import verify
from tests.unit.test_notes_support import SESSION_ID

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic"


async def run_pipeline(fixture: Fixture) -> tuple[VerifiedDraft | None, Decision]:
    """What ``service.draft_for_session`` does with any provider — kept in one place on purpose."""
    segments = fixture.segment_views()
    raw = await RecordedProvider(FIXTURES / f"{fixture.name}.json").draft(
        DraftContext(session_id=SESSION_ID, segments=segments)
    )
    assert raw.provider == "recorded" and raw.model == fixture.model
    try:
        draft = parse_draft(raw.text)
    except DraftSchemaError:
        # a recording answers identically on retry, so the second failure is implied
        return None, decide(None, schema_failures=MAX_SCHEMA_FAILURES)
    verified = verify(draft, segments)
    return verified, decide(verified)


def test_at_least_five_fixtures_cover_the_required_cases():
    names = [f.name for f in iter_fixtures(FIXTURES)]
    assert len(names) >= 5
    for key in ("assessment_key", "hallucinated_quote", "numeric_mismatch", "valid_paraphrase", "injection"):
        assert any(key in n for n in names), key


@pytest.mark.parametrize("fixture", iter_fixtures(FIXTURES), ids=lambda f: f.name)
async def test_fixture_outcome(fixture: Fixture):
    verified, decision = await run_pipeline(fixture)
    assert decision.status is fixture.expected.status
    if not fixture.expected.schema_ok:
        assert verified is None and decision.reason == "schema"
        return
    assert verified is not None
    assert [s.verdict_reason for s in verified.statements if s.verdict_reason] == fixture.expected.reasons
    assert verified.supported_count == fixture.expected.supported


async def test_assessment_key_never_reaches_the_verifier():
    fixture = next(f for f in iter_fixtures(FIXTURES) if "assessment_key" in f.name)
    assert "assessment" in fixture.raw
    verified, decision = await run_pipeline(fixture)
    assert verified is None and decision.status is NoteStatus.abstained


async def test_injection_fixture_leaks_nothing():
    fixture = next(f for f in iter_fixtures(FIXTURES) if "injection" in f.name)
    verified, decision = await run_pipeline(fixture)
    assert verified is not None and decision.reason == "low_coverage"
    for st in verified.statements:
        if st.verdict == "supported":
            assert "조현병" not in st.text and "지시" not in st.text
