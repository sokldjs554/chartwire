"""§9.3 policy thresholds."""

from __future__ import annotations

import pytest

from chartwire.notes.policy import MAX_SCHEMA_FAILURES, REVIEW_MAX_UNSUPPORTED, REVIEW_MIN_COVERAGE, decide
from chartwire.notes.schema import NoteStatus, VerifiedDraft, VerifiedEvidence, VerifiedStatement


def verified(supported: int, unsupported: int, *, abstain: bool = False) -> VerifiedDraft:
    def st(ok: bool) -> VerifiedStatement:
        return VerifiedStatement(
            section="S",
            text="입맛이 없다고 함",
            kind="reported",
            evidence=[VerifiedEvidence(seq=1, quote="입맛이 없어요", method="exact" if ok else None)],
            verdict="supported" if ok else "unsupported",
            verdict_reason=None if ok else "quote_mismatch",
        )

    total = supported + unsupported
    return VerifiedDraft(
        statements=[st(True)] * supported + [st(False)] * unsupported,
        coverage=supported / total if total else 0.0,
        unsupported_count=unsupported,
        abstain_requested=abstain,
    )


def test_thresholds_are_the_spec_values():
    assert (MAX_SCHEMA_FAILURES, REVIEW_MAX_UNSUPPORTED, REVIEW_MIN_COVERAGE) == (2, 3, 0.85)


@pytest.mark.parametrize(
    ("supported", "unsupported", "status", "reason"),
    [
        (5, 0, NoteStatus.verified, None),
        (1, 0, NoteStatus.verified, None),
        (6, 1, NoteStatus.needs_review, None),  # 0.857
        (12, 2, NoteStatus.needs_review, None),  # 0.857, 2 < 3
        (5, 1, NoteStatus.abstained, "low_coverage"),  # 0.833
        (20, 3, NoteStatus.abstained, "low_coverage"),  # 3 is not < 3
        (0, 1, NoteStatus.abstained, "low_coverage"),
        (0, 0, NoteStatus.abstained, "empty"),
    ],
)
def test_decide(supported: int, unsupported: int, status: NoteStatus, reason: str | None):
    d = decide(verified(supported, unsupported))
    assert (d.status, d.reason) == (status, reason)
    assert d.abstained is (status is NoteStatus.abstained)


def test_schema_failures_and_provider_error_abstain_first():
    assert decide(verified(5, 0), schema_failures=2).reason == "schema"
    assert decide(verified(5, 0), schema_failures=1).status is NoteStatus.verified
    assert decide(None).reason == "schema"
    assert decide(verified(5, 0), provider_error=True).reason == "provider_error"
    assert decide(None, provider_error=True).reason == "provider_error"


def test_provider_abstain_wins_over_statements():
    assert decide(verified(3, 0, abstain=True)).reason == "provider_abstain"
    assert decide(verified(0, 0, abstain=True)).reason == "provider_abstain"
