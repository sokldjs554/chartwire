"""``protocol.json`` aggregator (spec §11.1): hypothesis examples run + chaos loss/dup.

* ``hypothesis_examples`` — counted from a real pytest run of the property suites
  (``tests/ws``) with ``--hypothesis-show-statistics``; the per-test ``N passing examples`` lines
  are summed. Nothing is declared by hand.
* ``chaos_loss`` / ``chaos_dup`` — from the chaos load scenario report ``docs/loadtest/D.json``
  (WP-H) when it exists.

Either half may be missing; the report is written only when at least one is present and the
README rows for the absent half are deleted by ``readme_numbers.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

PROPERTY_SUITES: Final[tuple[str, ...]] = ("tests/ws",)
CHAOS_REPORT: Final = Path("docs/loadtest/D.json")
_PASSING: Final = re.compile(r"(\d+) passing(?: examples|,\s*\d+ failing)")
"""Hypothesis has written both ``N passing examples`` and
``N passing, M failing, and K invalid test cases`` — accept either wording."""
_TEST_HEADER: Final = re.compile(r"^(tests/\S+::\S+):$", re.MULTILINE)


def parse_statistics(output: str) -> dict[str, int]:
    """``test id → passing examples`` from ``--hypothesis-show-statistics`` output."""
    counts: dict[str, int] = {}
    current: str | None = None
    for line in output.splitlines():
        header = _TEST_HEADER.match(line.strip())
        if header:
            current = header.group(1)
            continue
        m = _PASSING.search(line)
        if m and current:
            counts[current] = counts.get(current, 0) + int(m.group(1))
    return counts


def run_property_suites(
    suites: tuple[str, ...] = PROPERTY_SUITES, *, timeout_s: int = 1800
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *suites,
        "-q",
        "-p",
        "no:cacheprovider",
        "--hypothesis-show-statistics",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    counts = parse_statistics(proc.stdout)
    return {
        "suites": list(suites),
        "exit_code": proc.returncode,
        "hypothesis_examples": sum(counts.values()),
        "property_tests": len(counts),
        "per_test": counts,
    }


def chaos_counts(report: Path = CHAOS_REPORT) -> dict[str, Any] | None:
    if not report.is_file():
        return None
    body = json.loads(report.read_text(encoding="utf-8"))
    if "loss" not in body or "dup" not in body:
        return None
    return {"chaos_loss": int(body["loss"]), "chaos_dup": int(body["dup"]), "chaos_source": str(report)}


def collect(*, run_tests: bool = True, chaos_report: Path = CHAOS_REPORT) -> dict[str, Any] | None:
    body: dict[str, Any] = {}
    if run_tests:
        body.update(run_property_suites())
    chaos = chaos_counts(chaos_report)
    if chaos:
        body.update(chaos)
    return body or None
