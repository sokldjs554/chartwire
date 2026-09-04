"""Shared corpus helpers for the evaluation runners (spec §10.3, §11.1).

* the eval script set (``e0001``–``e0200``, seed 42) generated in memory — no files needed;
* script utterances → :class:`SegmentView` (what the notes layer consumes);
* the frozen held-out risk set with its ``FROZEN.txt`` hash check — the runner **refuses** to
  score a modified file (leakage control: rules may not be tuned against the headline set).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from chartwire.notes.schema import SegmentView
from chartwire.synth.scripts import Profile, Script, generate_set

DATA_DIR: Final = Path(__file__).resolve().parent / "data"
HELDOUT_FILE: Final = DATA_DIR / "heldout_risk_ko.jsonl"
FROZEN_FILE: Final = DATA_DIR / "FROZEN.txt"
EVAL_SEED: Final = 42
EVAL_SIZE: Final = 200
_T0: Final = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


class FrozenArtifactChanged(RuntimeError):
    """``heldout_risk_ko.jsonl`` no longer matches ``FROZEN.txt``."""


def eval_scripts(seed: int = EVAL_SEED, n: int = EVAL_SIZE, profile: Profile = "eval") -> list[Script]:
    return generate_set(profile, n, seed)


def segment_views(script: Script) -> list[SegmentView]:
    """One segment per utterance; ``seq`` = utterance index (the stt-worker numbers finals the same way)."""
    return [
        SegmentView(
            segment_id=u.idx + 1,
            seq=u.idx,
            speaker=u.speaker,
            text=u.text,
            t_start_ms=u.t_start_ms,
            t_end_ms=u.t_end_ms,
            created_at=_T0,
            confidence=0.9,
        )
        for u in script.utterances
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def frozen_hash(frozen: Path = FROZEN_FILE) -> str:
    """The recorded sha256 (``sha256sum`` format: ``<hex>  <name>``)."""
    for line in frozen.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == HELDOUT_FILE.name:
            return parts[0]
    raise FrozenArtifactChanged(f"{frozen} 에 {HELDOUT_FILE.name} 항목이 없습니다")


def verify_frozen(heldout: Path = HELDOUT_FILE, frozen: Path = FROZEN_FILE) -> str:
    """Return the hash when it matches; raise :class:`FrozenArtifactChanged` otherwise."""
    expected = frozen_hash(frozen)
    actual = sha256_file(heldout)
    if actual != expected:
        raise FrozenArtifactChanged(
            f"{heldout.name} 의 sha256 이 FROZEN.txt 와 다릅니다 (기대 {expected[:12]}…, 실제 {actual[:12]}…) — "
            "held-out 세트는 동결되어 있습니다; 평가를 거부합니다"
        )
    return actual


def load_heldout(heldout: Path = HELDOUT_FILE) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in heldout.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        for key in ("text", "speaker", "alert", "kind"):
            if key not in row:
                raise ValueError(f"held-out 행에 {key} 가 없습니다")
    return rows
