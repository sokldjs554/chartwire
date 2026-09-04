"""Corpus helpers: frozen-hash gate on the held-out set, script → SegmentView conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from chartwire.eval import corpus


def test_frozen_hash_matches_the_committed_file() -> None:
    digest = corpus.verify_frozen()
    assert len(digest) == 64 and digest == corpus.frozen_hash()
    rows = corpus.load_heldout()
    assert 200 <= len(rows) <= 300
    assert {r["kind"] for r in rows} >= {"positive", "negated", "idiom", "past"}
    assert all(isinstance(r["alert"], bool) for r in rows)


def test_modified_heldout_is_refused(tmp_path: Path) -> None:
    tampered = tmp_path / "heldout_risk_ko.jsonl"
    tampered.write_bytes(
        corpus.HELDOUT_FILE.read_bytes()
        + b'{"text": "x", "speaker": "patient", "alert": false, "kind": "unrelated"}\n'
    )
    frozen = tmp_path / "FROZEN.txt"
    frozen.write_text(corpus.FROZEN_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(corpus.FrozenArtifactChanged, match="동결"):
        corpus.verify_frozen(tampered, frozen)
    frozen.write_text("deadbeef  other.jsonl\n", encoding="utf-8")
    with pytest.raises(corpus.FrozenArtifactChanged, match="항목이 없습니다"):
        corpus.verify_frozen(tampered, frozen)


def test_heldout_rows_require_the_label_fields(tmp_path: Path) -> None:
    bad = tmp_path / "h.jsonl"
    bad.write_text('{"text": "x", "speaker": "patient"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="alert"):
        corpus.load_heldout(bad)


def test_segment_views_follow_the_script_timeline() -> None:
    script = corpus.eval_scripts(42, 3)[0]
    views = corpus.segment_views(script)
    assert [v.seq for v in views] == [u.idx for u in script.utterances] == list(range(len(views)))
    assert [v.text for v in views] == [u.text for u in script.utterances]
    assert all(v.t_end_ms > v.t_start_ms and v.speaker in ("clinician", "patient") for v in views)
    assert corpus.eval_scripts(42, 3) == corpus.eval_scripts(42, 3)  # deterministic
