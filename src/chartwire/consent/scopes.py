"""Consent scopes (§8.3) — the one place their names are spelled out."""

from __future__ import annotations

from typing import Final, Literal, get_args

Scope = Literal["recording", "transcription", "ai_drafting", "search_index"]

SCOPE_ORDER: Final[tuple[Scope, ...]] = get_args(Scope)
"""Display order (console checkboxes, ``scopes_snapshot``)."""

SCOPES: Final[frozenset[str]] = frozenset(SCOPE_ORDER)

RECORDING: Final[Scope] = "recording"
TRANSCRIPTION: Final[Scope] = "transcription"
AI_DRAFTING: Final[Scope] = "ai_drafting"
SEARCH_INDEX: Final[Scope] = "search_index"
