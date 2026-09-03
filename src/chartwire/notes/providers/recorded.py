"""Replay a hand-made LLM response from ``tests/fixtures/anthropic/*.json`` (spec §9.2).

A fixture records what a model *would* have handed back through the tool-use channel (``raw``,
an arbitrary JSON value — deliberately untyped so schema rejections can be recorded too), the
segments it was shown, and the outcome the pipeline must produce. ``RecordedProvider`` returns
``raw`` verbatim; the test then runs the same ``parse_draft → verify → decide`` code the live
provider goes through.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chartwire.notes.providers.base import Stopwatch
from chartwire.notes.schema import DraftContext, NoteStatus, RawDraft, SegmentView, Speaker, VerdictReason


class FixtureSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    speaker: Speaker
    text: str


class FixtureExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_ok: bool
    status: NoteStatus
    reasons: list[VerdictReason] = Field(default_factory=list)
    """``verdict_reason`` per statement, in order (``None`` entries are written as omitted → use
    ``supported`` count instead); only checked when ``schema_ok``."""
    supported: int = 0


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    model: str
    segments: list[FixtureSegment] = Field(min_length=1)
    raw: Any
    expected: FixtureExpectation

    def segment_views(self) -> list[SegmentView]:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            SegmentView(
                segment_id=i + 1,
                seq=s.seq,
                speaker=s.speaker,
                text=s.text,
                t_start_ms=i * 2000,
                t_end_ms=i * 2000 + 1500,
                created_at=t0,
                confidence=0.9,
            )
            for i, s in enumerate(self.segments)
        ]


def load_fixture(path: Path) -> Fixture:
    return Fixture.model_validate_json(path.read_text(encoding="utf-8"))


def iter_fixtures(directory: Path) -> list[Fixture]:
    return [load_fixture(p) for p in sorted(directory.glob("*.json"))]


class RecordedProvider:
    """Replays one fixture's ``raw`` response; ignores the context it is given (it is a recording)."""

    name = "recorded"

    def __init__(self, fixture_path: Path) -> None:
        self.fixture = load_fixture(fixture_path)

    async def draft(self, ctx: DraftContext) -> RawDraft:
        watch = Stopwatch()
        return RawDraft(
            text=json.dumps(self.fixture.raw, ensure_ascii=False),
            provider=self.name,
            model=self.fixture.model,
            prompt_hash=None,
            latency_ms=watch.elapsed_ms,
        )
