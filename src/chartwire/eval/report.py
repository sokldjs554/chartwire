"""Report header shared by every evaluation / load-test / perf JSON (spec §11.1).

``{seed, git_sha, generated_at, cpu, ram_gb, python, pg_version}`` — every
number that reaches the README is traceable to a header like this.
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

REPO_ROOT = Path(__file__).resolve().parents[3]


def git_sha(root: Path = REPO_ROOT) -> str:
    """Current commit sha; ``GITHUB_SHA`` in CI; ``"unknown"`` outside a checkout."""
    env = os.environ.get("GITHUB_SHA")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def cpu_description() -> str:
    """``"<model name> x<logical cores>"`` from /proc/cpuinfo, falling back to platform info."""
    model = platform.processor() or platform.machine()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return f"{model} x{os.cpu_count() or 1}"


def report_header(seed: int, *, pg_version: str | None = None) -> dict[str, Any]:
    return {
        "seed": seed,
        "git_sha": git_sha(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cpu": cpu_description(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "python": platform.python_version(),
        "pg_version": pg_version,
    }


def build_report(seed: int, body: dict[str, Any], *, pg_version: str | None = None) -> dict[str, Any]:
    """Header first, then the report body — the shape every ``docs/**/*.json`` file has."""
    return {**report_header(seed, pg_version=pg_version), **body}
