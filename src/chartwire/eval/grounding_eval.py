"""Grounding evaluation of the extractive provider (spec §11.1 ``grounding.json``).

Per eval script: ``build_draft`` → ``verify`` → ``decide`` on the script utterances. Reported:

* **coverage** — supported / total statements (1.0 by construction for the extractive provider,
  see ``docs/grounding.md``; reported as the appendix number it is);
* **fact recall** — a gold fact (medication, sleep hours, weight change, plan …) counts as
  recalled when the utterance that carries it is cited as evidence by a *supported* statement.
  This is deliberately strict and explainable: no fuzzy value matching, the fact either made it
  into the note with its source or it did not;
* **abstain rate** — sessions whose policy decision is ``abstained``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from chartwire.eval.corpus import segment_views
from chartwire.notes.extractive import build_draft
from chartwire.notes.policy import decide
from chartwire.notes.verifier import VERIFIER_VERSION, verify
from chartwire.synth.scripts import Script


def evaluate(scripts: list[Script]) -> dict[str, Any]:
    facts_total = facts_recalled = 0
    by_type_total: Counter[str] = Counter()
    by_type_recalled: Counter[str] = Counter()
    statements = supported = sessions_abstained = 0
    statuses: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    coverage_sum = 0.0
    for script in scripts:
        segments = segment_views(script)
        draft = build_draft(segments)
        verified = verify(draft, segments)
        decision = decide(verified)
        statuses[decision.status.value] += 1
        sessions_abstained += decision.abstained
        statements += len(verified.statements)
        supported += verified.supported_count
        coverage_sum += verified.coverage
        cited = {ev.seq for st in verified.statements if st.verdict == "supported" for ev in st.evidence}
        for st in verified.statements:
            sections[st.section] += 1
        for utt in script.utterances:
            for fact in utt.gold.facts:
                facts_total += 1
                by_type_total[fact.type] += 1
                if utt.idx in cited:
                    facts_recalled += 1
                    by_type_recalled[fact.type] += 1
    n = len(scripts)
    return {
        "provider": "extractive",
        "verifier_version": VERIFIER_VERSION,
        "n_sessions": n,
        "coverage": round(coverage_sum / n, 4) if n else 0.0,
        "coverage_note": "추출형 초안은 발화 전체를 인용하므로 구성상 1.0 (docs/grounding.md)",
        "fact_recall": round(facts_recalled / facts_total, 4) if facts_total else 0.0,
        "facts_total": facts_total,
        "facts_recalled": facts_recalled,
        "fact_recall_by_type": {
            t: {
                "total": by_type_total[t],
                "recalled": by_type_recalled[t],
                "recall": round(by_type_recalled[t] / by_type_total[t], 4),
            }
            for t in sorted(by_type_total)
        },
        "abstain_rate": round(sessions_abstained / n, 4) if n else 0.0,
        "status_counts": dict(statuses),
        "statements_total": statements,
        "statements_supported": supported,
        "statements_per_session": round(statements / n, 2) if n else 0.0,
        "section_counts": dict(sections),
    }
