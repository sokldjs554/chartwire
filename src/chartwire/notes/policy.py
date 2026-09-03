"""Note status policy (spec §9.3, last paragraph).

The verifier says *what* is unsupported; the policy says what the note becomes. Thresholds are
module constants so the eval report and the docs quote one source.
"""

from __future__ import annotations

from dataclasses import dataclass

from chartwire.notes.schema import AbstainReason, NoteStatus, VerifiedDraft

MAX_SCHEMA_FAILURES = 2
"""A provider whose output fails schema validation this many times → ``abstained(schema)``."""
REVIEW_MAX_UNSUPPORTED = 3
"""Fewer than this many unsupported statements may go to clinician review …"""
REVIEW_MIN_COVERAGE = 0.85
"""… provided supported/total is at least this."""


@dataclass(frozen=True)
class Decision:
    status: NoteStatus
    reason: AbstainReason | None = None

    @property
    def abstained(self) -> bool:
        return self.status is NoteStatus.abstained


def decide(
    verified: VerifiedDraft | None,
    *,
    schema_failures: int = 0,
    provider_error: bool = False,
) -> Decision:
    """Map a verified draft (or the absence of one) to a note status.

    Order matters: a provider that could not answer or could not produce a schema-valid draft
    abstains regardless of anything else — there is no silent fallback to another provider.
    """
    if provider_error:
        return Decision(NoteStatus.abstained, "provider_error")
    if schema_failures >= MAX_SCHEMA_FAILURES or verified is None:
        return Decision(NoteStatus.abstained, "schema")
    if verified.abstain_requested:
        return Decision(NoteStatus.abstained, "provider_abstain")
    if not verified.statements:
        return Decision(NoteStatus.abstained, "empty")
    if verified.unsupported_count == 0:
        return Decision(NoteStatus.verified)
    if verified.unsupported_count < REVIEW_MAX_UNSUPPORTED and verified.coverage >= REVIEW_MIN_COVERAGE:
        return Decision(NoteStatus.needs_review)
    return Decision(NoteStatus.abstained, "low_coverage")
