"""Verifier false-rejection rate on legitimate rewrites (spec §11.1 ``paraphrase.json``).

``ParaphrasingMockProvider`` rewrites the ``S`` statements of the extractive draft the way a
careful model would (endings, particles, synonyms, merges, number words, honorifics) while
keeping every evidence quote verbatim. A rewritten statement that comes back ``unsupported`` is a
false rejection. The table is per transform; the residual causes (transform × verdict reason)
are listed as they are — ADR-0003 forbids loosening a rule to make this number look better.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

from chartwire.eval.corpus import segment_views
from chartwire.notes.extractive import build_draft
from chartwire.notes.providers.paraphrase import TRANSFORMS, Transform, paraphrase
from chartwire.notes.verifier import VERIFIER_VERSION, verify
from chartwire.synth.scripts import Script


def evaluate(scripts: list[Script], *, passes: int = 3, seed: int = 42) -> dict[str, Any]:
    """``passes`` rounds over the corpus with different random transform choices per statement."""
    rng = random.Random(seed)
    per: dict[Transform, dict[str, Any]] = {
        t: {"total": 0, "rejected": 0, "reasons": Counter()} for t in TRANSFORMS
    }
    residual: Counter[str] = Counter()
    for _ in range(passes):
        for script in scripts:
            segments = segment_views(script)
            draft, applied = paraphrase(build_draft(segments), rng)
            verified = verify(draft, segments)
            for a in applied:
                st = verified.statements[a.index]
                row = per[a.transform]
                row["total"] += 1
                if st.verdict == "unsupported":
                    row["rejected"] += 1
                    row["reasons"][st.verdict_reason or "?"] += 1
                    residual[f"{a.transform}:{st.verdict_reason}"] += 1
    total = sum(r["total"] for r in per.values())
    rejected = sum(r["rejected"] for r in per.values())
    return {
        "verifier_version": VERIFIER_VERSION,
        "n_sessions": len(scripts),
        "passes": passes,
        "n_statements": total,
        "rejected": rejected,
        "false_rejection_rate": round(rejected / total, 4) if total else 0.0,
        "by_transform": [
            {
                "transform": t,
                "total": r["total"],
                "rejected": r["rejected"],
                "rate": round(r["rejected"] / r["total"], 4) if r["total"] else 0.0,
                "reasons": dict(r["reasons"].most_common()),
            }
            for t, r in per.items()
        ],
        "residual_causes": [{"cause": cause, "count": count} for cause, count in residual.most_common()],
    }
