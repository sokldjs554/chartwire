"""Keyholder-only evaluation of ``AnthropicProvider`` (spec §9.2, §11.1 ``anthropic.json``).

Importable and runnable without a key: with no ``--scripts-dir`` it replays the recorded fixtures
through ``RecordedProvider`` (the CI path) and writes the same report shape. With a key it drafts
each script's session through the live provider — the report holds counts only, never text.

    ANTHROPIC_API_KEY=… CHARTWIRE_ANTHROPIC_MODEL=claude-opus-5 \\
      python scripts/eval_anthropic.py --scripts-dir data/scripts --out docs/eval/anthropic.json

README 규칙(§11.4): 이 스크립트가 실행되지 않았다면 README 는 "미실행" 이라고 적는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from chartwire.notes.policy import MAX_SCHEMA_FAILURES, decide
from chartwire.notes.providers.base import NoteProvider, ProviderError
from chartwire.notes.providers.recorded import RecordedProvider, iter_fixtures
from chartwire.notes.schema import DraftContext, DraftSchemaError, SegmentView, parse_draft
from chartwire.notes.verifier import VERIFIER_VERSION, verify

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "anthropic"
DEFAULT_OUT = ROOT / "docs" / "eval" / "anthropic.json"


def header(seed: int) -> dict[str, Any]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = None
    return {
        "seed": seed,
        "git_sha": sha,
        "generated_at": datetime.now(UTC).isoformat(),
        "cpu": platform.processor() or platform.machine(),
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "python": platform.python_version(),
        "pg_version": None,
        "verifier_version": VERIFIER_VERSION,
    }


def segments_from_script(path: Path) -> list[SegmentView]:
    """Minimal reader for the §10.2 script JSON (``utterances[].speaker/text/t_*_ms``)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    t0 = datetime.now(UTC)
    return [
        SegmentView(
            segment_id=u["idx"] + 1,
            seq=u["idx"] + 1,
            speaker=u["speaker"],
            text=u["text"],
            t_start_ms=u["t_start_ms"],
            t_end_ms=u["t_end_ms"],
            created_at=t0,
            confidence=1.0,
        )
        for u in doc["utterances"]
    ]


async def run_one(provider: NoteProvider, segments: list[SegmentView]) -> dict[str, Any]:
    """One session through the exact service path: draft → parse (≤2 tries) → verify → decide."""
    ctx = DraftContext(session_id=uuid4(), segments=segments)
    schema_failures = 0
    latency_ms = 0
    for _ in range(MAX_SCHEMA_FAILURES):
        try:
            raw = await provider.draft(ctx)
        except ProviderError as exc:
            d = decide(None, provider_error=True)
            return {"status": d.status, "reason": d.reason, "error": str(exc), "latency_ms": latency_ms}
        latency_ms += raw.latency_ms
        try:
            draft = parse_draft(raw.text)
            break
        except DraftSchemaError:
            schema_failures += 1
    else:
        d = decide(None, schema_failures=schema_failures)
        return {
            "status": d.status,
            "reason": d.reason,
            "schema_failures": schema_failures,
            "latency_ms": latency_ms,
        }
    verified = verify(draft, segments)
    d = decide(verified)
    return {
        "status": d.status,
        "reason": d.reason,
        "statements": len(verified.statements),
        "unsupported": verified.unsupported_count,
        "coverage": round(verified.coverage, 4),
        "reasons": dict(Counter(s.verdict_reason for s in verified.statements if s.verdict_reason)),
        "schema_failures": schema_failures,
        "latency_ms": latency_ms,
    }


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    reasons: Counter[str] = Counter()
    for r in rows:
        reasons.update(r.get("reasons", {}))
    covered = [r["coverage"] for r in rows if "coverage" in r]
    return {
        "sessions": len(rows),
        "status": dict(Counter(r["status"] for r in rows)),
        "abstain_reason": dict(Counter(r["reason"] for r in rows if r["reason"])),
        "verify_reason_total": dict(reasons),
        "mean_coverage": round(sum(covered) / len(covered), 4) if covered else None,
        "provider_errors": sum("error" in r for r in rows),
    }


async def evaluate_recorded(fixtures_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for fixture in iter_fixtures(fixtures_dir):
        provider: NoteProvider = RecordedProvider(fixtures_dir / f"{fixture.name}.json")
        rows.append({"name": fixture.name, **await run_one(provider, fixture.segment_views())})
    return rows


async def evaluate_live(model: str, api_key: str, scripts: list[Path]) -> list[dict[str, Any]]:
    from chartwire.notes.providers.anthropic import AnthropicProvider  # SDK required only here

    live = AnthropicProvider(model, api_key=api_key)
    return [{"name": path.stem, **await run_one(live, segments_from_script(path))} for path in scripts]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--scripts-dir", type=Path, default=None, help="§10.2 스크립트 JSON 디렉터리 (키 필요)")
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.add_argument("--model", default=os.environ.get("CHARTWIRE_ANTHROPIC_MODEL"))
    p.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.scripts_dir is not None and not (args.api_key and args.model):
        print(
            "ANTHROPIC_API_KEY 와 CHARTWIRE_ANTHROPIC_MODEL 이 모두 필요합니다 (키 없이는 --scripts-dir 없이 실행)."
        )
        return 2
    if args.scripts_dir is None:
        rows, mode = asyncio.run(evaluate_recorded(args.fixtures)), "recorded"
    else:
        scripts = sorted(Path(args.scripts_dir).glob("*.json"))[: args.limit]
        rows, mode = asyncio.run(evaluate_live(args.model, args.api_key, scripts)), "anthropic"
    report = {
        **header(args.seed),
        "mode": mode,
        "model": args.model,
        "summary": summarize(rows),
        "sessions": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
