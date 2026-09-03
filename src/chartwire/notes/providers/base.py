"""Shared provider plumbing (spec §9.2).

A provider returns :class:`RawDraft` — text that *claims* to be a :class:`NoteDraftOut`. Nothing
here trusts it: parsing happens in ``schema.parse_draft`` and grounding in ``verifier.verify``.
``ProviderError`` is the one exception a caller has to handle; the service maps it to
``abstained(provider_error)`` (never to a silent fallback provider — provenance).
"""

from __future__ import annotations

import time

from chartwire.notes.schema import NoteDraftOut, NoteProvider, RawDraft

__all__ = ["NoteProvider", "ProviderError", "Stopwatch", "raw_from_draft"]


class ProviderError(Exception):
    """The provider could not produce a draft (timeout, transport, malformed response)."""


class Stopwatch:
    """Monotonic elapsed milliseconds for ``RawDraft.latency_ms``."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


def raw_from_draft(
    draft: NoteDraftOut,
    *,
    provider: str,
    latency_ms: int,
    model: str | None = None,
    prompt_hash: str | None = None,
) -> RawDraft:
    """Serialise a typed draft the way an LLM provider would have returned it, so every provider
    — offline or not — goes through the identical ``parse_draft → verify → decide`` path."""
    return RawDraft(
        text=draft.model_dump_json(),
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        latency_ms=latency_ms,
    )
