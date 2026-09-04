"""Adversarial evaluations of the verifier (spec §11.1 ``inject.json`` and ``injection.json``).

``inject.json`` — hallucination detection: for each of the seven mutation classes, 200 mutated
drafts built from eval-script sessions; the mutated statement must come back ``unsupported``.
The false-flag rate is measured on the *untouched* statements of the same drafts — the price of
a strict verifier is paid in legitimate statements it rejects, so it is reported next to the
detection rate rather than hidden.

``injection.json`` — prompt injection: the 20 eval sessions that carry injected patient
utterances (``e0010, e0020, …``). A leak is an injected utterance that reaches the draft — as a
statement text, an evidence quote or a cited seq. The extractive provider drops them by
construction; the verifier's rule 8 is exercised separately by forcing a citation of every
injected utterance and counting how many it flags (``injection_pattern``).
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any, Final

from chartwire.eval.corpus import segment_views
from chartwire.notes.extractive import build_draft
from chartwire.notes.providers.mutation import EXPECTED_REASON, MUTATION_CLASSES, MutationClass, mutate
from chartwire.notes.schema import Evidence, NoteDraftOut, Statement
from chartwire.notes.verifier import VERIFIER_VERSION, verify
from chartwire.synth.scripts import Script

SHORT_NAME: Final[dict[MutationClass, str]] = {
    "number_change": "number",
    "drug_swap": "drug",
    "fabricated_statement": "fabricated",
    "evidence_seq_wrong": "seq",
    "diagnosis_insert": "diagnosis",
    "negation_flip": "negation",
    "speaker_swap": "speaker",
}
"""README keys use the short names (``eval.inject.<short>.detection_rate``)."""


def evaluate_mutations(scripts: list[Script], *, per_class: int = 200, seed: int = 42) -> dict[str, Any]:
    rng = random.Random(seed)
    sessions = [segment_views(s) for s in scripts]
    drafts = [build_draft(segs) for segs in sessions]
    classes: list[dict[str, Any]] = []
    clean_total = clean_flagged = 0
    for cls in MUTATION_CLASSES:
        detected = 0
        reasons: Counter[str] = Counter()
        done = attempts = 0
        while done < per_class:
            attempts += 1
            if attempts > per_class * 50:
                raise RuntimeError(f"{cls}: 변이 가능한 문장이 부족합니다")
            i = rng.randrange(len(sessions))
            result = mutate(drafts[i], cls, sessions[i], rng)
            if result is None:
                continue
            draft, applied = result
            verified = verify(draft, sessions[i])
            target = verified.statements[applied.index]
            detected += target.verdict == "unsupported"
            reasons[target.verdict_reason or "supported"] += 1
            for j, st in enumerate(verified.statements):
                if j != applied.index:
                    clean_total += 1
                    clean_flagged += st.verdict == "unsupported"
            done += 1
        classes.append(
            {
                "class": SHORT_NAME[cls],
                "mutation": cls,
                "n": per_class,
                "detected": detected,
                "detection_rate": round(detected / per_class, 4),
                "expected_reason": EXPECTED_REASON[cls],
                "reasons": dict(reasons.most_common()),
            }
        )
    return {
        "verifier_version": VERIFIER_VERSION,
        "n_sessions": len(scripts),
        "n_per_class": per_class,
        "classes": classes,
        "false_flag_rate": round(clean_flagged / clean_total, 4) if clean_total else 0.0,
        "clean_statements": clean_total,
        "clean_flagged": clean_flagged,
    }


def injection_scripts(scripts: list[Script]) -> list[Script]:
    return [s for s in scripts if s.meta.injection]


def evaluate_injection(scripts: list[Script]) -> dict[str, Any]:
    targets = injection_scripts(scripts)
    leaks = injected_total = excluded = forced_flagged = 0
    leak_details: list[dict[str, Any]] = []
    for script in targets:
        segments = segment_views(script)
        by_seq = {u.idx: u.text for u in script.utterances}
        injected = {idx: by_seq[idx] for idx in script.meta.injection_utterances}
        injected_total += len(injected)
        draft = build_draft(segments)
        for i, st in enumerate(draft.statements):
            cited = {ev.seq for ev in st.evidence}
            quoted = " ".join(ev.quote for ev in st.evidence)
            leaked = [
                idx
                for idx, text in injected.items()
                if idx in cited or text in st.text or text in quoted or st.text in text
            ]
            if leaked:
                leaks += len(leaked)
                leak_details.append({"script_ref": script.script_ref, "statement": i, "utterances": leaked})
        excluded += sum(
            1 for idx in injected if idx not in {ev.seq for st in draft.statements for ev in st.evidence}
        )
        # adversarial: cite every injected utterance verbatim and see whether rule 8 flags it
        forced = NoteDraftOut(
            statements=[
                Statement(
                    section="S",
                    text=text[:200],
                    evidence=[Evidence(seq=idx, quote=text[:200])],
                    kind="reported",
                )
                for idx, text in injected.items()
                if len(text) >= 4
            ]
        )
        if forced.statements:
            verified = verify(forced, segments)
            forced_flagged += sum(1 for st in verified.statements if st.verdict_reason == "injection_pattern")
    return {
        "verifier_version": VERIFIER_VERSION,
        "n_sessions": len(targets),
        "script_refs": [s.script_ref for s in targets],
        "n_injection_utterances": injected_total,
        "injection_leaks": leaks,
        "leak_details": leak_details,
        "excluded_by_provider": excluded,
        "forced_citations_flagged": forced_flagged,
        "forced_citations_total": injected_total,
    }
