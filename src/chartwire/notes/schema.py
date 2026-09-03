"""Note-drafting contracts (spec §9.1, §9.2, §9.3).

Everything a provider may return is :class:`NoteDraftOut`. It deliberately has no field for an
assessment, diagnosis, ICD/DSM code, risk level, medication recommendation or verdict: a
statement is either a *reported* patient utterance, an *observed* clinician remark or a *plan
item*, and every one of them must cite verbatim evidence. Unknown keys are rejected
(``extra="forbid"``), so a model that "helpfully" adds ``assessment`` fails schema validation
before anything downstream sees it (ADR-0003).

``SegmentView`` is the read model the verifier works on; ``VerifiedStatement`` / ``VerifiedDraft``
are the verifier's output, and ``NoteStatus`` mirrors the ``notes.status`` CHECK constraint.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Section = Literal["S", "O", "P"]
StatementKind = Literal["reported", "observed", "plan_item"]
Speaker = Literal["clinician", "patient", "unknown"]
Verdict = Literal["supported", "unsupported"]
MatchMethod = Literal["exact", "normalized"]

VerdictReason = Literal[
    "fabricated_segment",
    "quote_mismatch",
    "numeric_mismatch",
    "entity_mismatch",
    "negation_mismatch",
    "speaker_mismatch",
    "verdict_language",
    "injection_pattern",
]
VERDICT_REASONS: tuple[VerdictReason, ...] = (
    "fabricated_segment",
    "quote_mismatch",
    "numeric_mismatch",
    "entity_mismatch",
    "negation_mismatch",
    "speaker_mismatch",
    "verdict_language",
    "injection_pattern",
)

AbstainReason = Literal[
    "low_coverage",
    "schema",
    "empty",
    "provider_abstain",
    "provider_error",
    "consent_scope_missing",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- provider output (§9.1)


class Evidence(_Strict):
    seq: int
    quote: str = Field(min_length=4, max_length=200)


class Statement(_Strict):
    section: Section
    text: str = Field(max_length=200)
    evidence: list[Evidence] = Field(min_length=1, max_length=4)
    kind: StatementKind


class NoteDraftOut(_Strict):
    statements: list[Statement] = Field(max_length=36)
    abstain: bool = False
    abstain_reason: str | None = None


class DraftSchemaError(ValueError):
    """The provider's raw text is not a valid :class:`NoteDraftOut` (extra keys, bad JSON, limits)."""


def parse_draft(text: str) -> NoteDraftOut:
    """The single entry point from provider text to a typed draft; used by every provider path."""
    try:
        return NoteDraftOut.model_validate_json(text)
    except ValidationError as exc:
        raise DraftSchemaError(_schema_error_summary(exc)) from exc


def _schema_error_summary(exc: ValidationError) -> str:
    """Error types and locations only — never the offending values (they may be transcript text)."""
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}:{err['type']}")
    return "; ".join(parts[:8])


# --------------------------------------------------------------------------- provider input (§9.2)


class SegmentView(_Strict):
    segment_id: int
    seq: int
    speaker: Speaker
    text: str
    t_start_ms: int
    t_end_ms: int
    created_at: datetime
    confidence: float


class DraftContext(_Strict):
    session_id: UUID
    segments: list[SegmentView]
    max_per_section: int = 12


class RawDraft(_Strict):
    text: str
    provider: str
    model: str | None
    prompt_hash: str | None
    latency_ms: int


class NoteProvider(Protocol):
    name: str

    async def draft(self, ctx: DraftContext) -> RawDraft: ...


# --------------------------------------------------------------------------- verifier output (§9.3)


class VerifiedEvidence(_Strict):
    """One evidence item after matching. ``start``/``end`` are character offsets into the NFC form
    of the segment text; ``None`` when the evidence failed to match (or its seq does not exist)."""

    seq: int
    quote: str
    start: int | None = None
    end: int | None = None
    method: MatchMethod | None = None


class VerifiedStatement(_Strict):
    section: Section
    text: str
    kind: StatementKind
    evidence: list[VerifiedEvidence]
    verdict: Verdict
    verdict_reason: VerdictReason | None = None


class VerifiedDraft(_Strict):
    statements: list[VerifiedStatement]
    coverage: float
    unsupported_count: int
    abstain_requested: bool = False

    @property
    def supported_count(self) -> int:
        return len(self.statements) - self.unsupported_count


class NoteStatus(str, Enum):
    drafting = "drafting"
    needs_review = "needs_review"
    verified = "verified"
    abstained = "abstained"
    signed = "signed"
    rejected = "rejected"
