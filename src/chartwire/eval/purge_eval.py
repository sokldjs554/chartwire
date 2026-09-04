"""``purge.json`` aggregator (spec §11.1).

The purge numbers are produced by the purge pipeline's own end-to-end run (WP-E: 50 sessions +
10 patients purged, then ``verify``). This module does not re-implement that run; it validates
the measurement file the pipeline writes and re-emits it with the common report header so the
README pipeline sees one layout for every report. Missing input → no report → the README rows
are deleted (never an "expected" number).

Expected input (``var/eval/purge.json`` by default)::

    {"sessions_purged": 50, "patients_purged": 10, "residual_rows": 0, "residual_objects": 0,
     "residual_keys": 0, "unwrap_failure_pct": 100.0, "decrypt_failure_pct": 100.0,
     "receipts_verified_pct": 100.0}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

DEFAULT_INPUT: Final = Path("var/eval/purge.json")
REQUIRED: Final[tuple[str, ...]] = (
    "residual_rows",
    "residual_objects",
    "residual_keys",
    "unwrap_failure_pct",
    "decrypt_failure_pct",
    "receipts_verified_pct",
)


class MissingMeasurement(FileNotFoundError):
    """The producing run has not happened; the report must not be written."""


def collect(source: Path = DEFAULT_INPUT, *, required: tuple[str, ...] = REQUIRED) -> dict[str, Any]:
    if not source.is_file():
        raise MissingMeasurement(f"{source} 가 없습니다 — 파기 파이프라인 측정이 아직 실행되지 않았습니다")
    body = json.loads(source.read_text(encoding="utf-8"))
    missing = [k for k in required if k not in body]
    if missing:
        raise ValueError(f"{source}: 필수 필드가 없습니다: {missing}")
    return {"source": str(source), **body}
