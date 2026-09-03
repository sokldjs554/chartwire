"""Script JSON contract shared by the STT simulator, the synthetic generator and the load clients.

One file per script at ``<scripts_dir>/<script_ref>.json``::

    {"script_ref": "s01", "seed": 1, "template": "초진 우울", "chunk_ms": 200, "total_ms": 12345,
     "utterances": [{"idx": 0, "speaker": "clinician", "text": "...", "t_start_ms": 0, "t_end_ms": 2000,
                     "gold": {"section_label": "S", "facts": [...], "risk": {...} | null, "pii": [...]}}],
     "meta": {"generator": "chartwire.synth", "version": "1"}}

Utterances are sorted by ``t_start_ms`` and never overlap. The simulator only consumes the
timeline (``speaker``, ``text``, ``t_start_ms``, ``t_end_ms``); ``gold`` is carried for the
evaluation harness and validated here so a malformed script fails at load time, not mid-session.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCRIPT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ScriptError(ValueError):
    """Raised for a missing, unreadable or contract-violating script file."""


class GoldFact(BaseModel):
    """Open-ended fact record; only ``type`` is mandatory (the generator owns the rest)."""

    model_config = ConfigDict(extra="allow")
    type: str


class GoldRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["suicidal_ideation", "self_harm", "harm_to_others", "substance_acute"] | None
    severity: int = Field(ge=0, le=3)
    kind: Literal[
        "positive", "negated", "hypothetical", "past", "third_person", "clinician_question", "idiom"
    ]


class GoldPii(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["name", "phone", "address"]
    value: str


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_label: Literal["S", "O", "P", "none"]
    facts: list[GoldFact] = Field(default_factory=list)
    risk: GoldRisk | None = None
    pii: list[GoldPii] = Field(default_factory=list)


class Utterance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=0)
    speaker: Literal["clinician", "patient"]
    text: str = Field(min_length=1)
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=1)
    gold: Gold

    @model_validator(mode="after")
    def _end_after_start(self) -> Utterance:
        if self.t_end_ms <= self.t_start_ms:
            raise ValueError(f"utterance {self.idx}: t_end_ms must be greater than t_start_ms")
        return self


class Meta(BaseModel):
    """Generator-owned block: the two required keys are fixed, anything else is passed through."""

    model_config = ConfigDict(extra="allow")
    generator: str
    version: str


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_ref: str = Field(pattern=SCRIPT_REF_RE.pattern)
    seed: int
    template: str
    chunk_ms: int = Field(ge=20, le=5_000)
    total_ms: int = Field(ge=1)
    utterances: list[Utterance] = Field(min_length=1)
    meta: Meta

    @model_validator(mode="after")
    def _timeline_is_sound(self) -> Script:
        prev_end = 0
        for pos, utt in enumerate(self.utterances):
            if utt.idx != pos:
                raise ValueError(f"utterance at position {pos} has idx {utt.idx}; idx must equal position")
            if utt.t_start_ms < prev_end:
                raise ValueError(
                    f"utterance {utt.idx} starts at {utt.t_start_ms} before previous end {prev_end}"
                )
            prev_end = utt.t_end_ms
        if self.total_ms < prev_end:
            raise ValueError(f"total_ms {self.total_ms} is shorter than the last utterance end {prev_end}")
        return self


def script_path(scripts_dir: Path | str, script_ref: str) -> Path:
    """``<scripts_dir>/<script_ref>.json``; rejects refs that could escape the directory."""
    if not SCRIPT_REF_RE.match(script_ref):
        raise ScriptError(f"invalid script_ref: {script_ref!r}")
    return Path(scripts_dir) / f"{script_ref}.json"


def load_script(path: Path | str) -> Script:
    """Read and validate one script file. Raises :class:`ScriptError` on any problem."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise ScriptError(f"cannot read script {p.name}: {exc.strerror}") from exc
    try:
        return Script.model_validate_json(raw)
    except ValidationError as exc:
        raise ScriptError(f"script {p.name} violates the contract: {exc.error_count()} error(s)") from exc
