"""Consent gates: latest-version semantics, fail-closed revocation, CW-4031 / 4011 error contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from chartwire.consent import gates
from chartwire.consent.gates import ConsentScopeMissing, active_scopes, has_scope, require_scope
from chartwire.consent.scopes import SCOPE_ORDER, SCOPES

NOW = datetime(2026, 9, 2, tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    version: int
    scopes: tuple[str, ...]
    revoked_at: datetime | None = None


def test_scope_constants() -> None:
    assert {"recording", "transcription", "ai_drafting", "search_index"} == SCOPES
    assert SCOPE_ORDER == ("recording", "transcription", "ai_drafting", "search_index")


def test_no_rows_means_no_scopes() -> None:
    assert active_scopes([]) == set()


def test_latest_version_governs_regardless_of_input_order() -> None:
    rows = [Row(2, ("recording", "transcription")), Row(1, ("recording", "transcription", "ai_drafting"))]
    assert active_scopes(rows) == {"recording", "transcription"}
    assert active_scopes(reversed(rows)) == {"recording", "transcription"}


def test_revoked_latest_version_yields_nothing_even_if_older_row_unrevoked() -> None:
    """Fail-closed: a withdrawn consent must not let a superseded version resurrect scopes."""
    rows = [Row(1, ("recording",)), Row(2, ("recording", "transcription"), revoked_at=NOW)]
    assert active_scopes(rows) == set()


def test_all_rows_revoked_yields_nothing() -> None:
    assert active_scopes([Row(1, ("recording",), NOW), Row(2, ("recording",), NOW)]) == set()


def test_unknown_scope_strings_in_a_row_are_dropped() -> None:
    assert active_scopes([Row(1, ("recording", "telepathy"))]) == {"recording"}


def test_require_scope_passes_and_fails() -> None:
    require_scope({"recording", "search_index"}, "recording")
    assert has_scope(["recording"], "recording") is True
    with pytest.raises(ConsentScopeMissing) as info:
        require_scope({"recording"}, "ai_drafting")
    err = info.value
    assert (err.code, err.ws_code, err.status, err.retryable) == ("CW-4031", 4011, 403, False)
    assert err.scope == "ai_drafting"
    assert "CONSENT_SCOPE_MISSING" in err.detail and "ai_drafting" in str(err)
    assert (gates.CODE, gates.WS_CODE, gates.STATUS) == ("CW-4031", 4011, 403)


def test_empty_scopes_fail_every_gate() -> None:
    for scope in SCOPE_ORDER:
        with pytest.raises(ConsentScopeMissing):
            require_scope(set(), scope)


def test_unknown_scope_name_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown consent scope"):
        require_scope({"recording"}, "recordng")


def test_end_to_end_rows_to_gate() -> None:
    rows = [Row(1, ("recording", "transcription", "ai_drafting", "search_index")), Row(2, ("recording",))]
    scopes = active_scopes(rows)
    require_scope(scopes, "recording")
    with pytest.raises(ConsentScopeMissing):
        require_scope(scopes, "transcription")
