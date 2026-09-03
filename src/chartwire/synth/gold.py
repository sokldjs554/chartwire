"""Session-level gold aggregation over a generated :class:`Script`.

The per-utterance gold lives inside the script; this module derives what
the evaluation harnesses need per session: the unique fact set (grounding
recall), the risk spans with alert expectation (in-grammar risk P/R), the
PII items with their redaction tokens, and one JSON record per script for
``gold.jsonl``.
"""

from __future__ import annotations

import json
from typing import Any

from chartwire.synth.scripts import GoldPii, Script, Utterance

REDACTION_TOKENS: dict[str, str] = {"name": "[이름]", "phone": "[전화]", "address": "[주소]"}


def alert_expected(utt: Utterance) -> bool:
    """An alert is expected only for an unsuppressed (``positive``) hit with severity ≥ 1."""
    risk = utt.gold.risk
    return risk is not None and risk.kind == "positive" and risk.severity >= 1


def session_facts(script: Script) -> list[dict[str, Any]]:
    """Unique gold facts of a session, in first-occurrence order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for utt in script.utterances:
        for fact in utt.gold.facts:
            record = fact.model_dump()
            key = json.dumps(record, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                out.append(record)
    return out


def risk_spans(script: Script) -> list[dict[str, Any]]:
    return [
        {"idx": utt.idx, "speaker": utt.speaker, **utt.gold.risk.model_dump(), "alert": alert_expected(utt)}
        for utt in script.utterances
        if utt.gold.risk is not None
    ]


def expected_alerts(script: Script) -> list[int]:
    return [utt.idx for utt in script.utterances if alert_expected(utt)]


def pii_items(script: Script) -> list[dict[str, Any]]:
    return [
        {"idx": utt.idx, "kind": item.kind, "value": item.value, "token": REDACTION_TOKENS[item.kind]}
        for utt in script.utterances
        for item in utt.gold.pii
    ]


def redact(text: str, pii: list[GoldPii]) -> str:
    """Expected ``segment_search.text`` after PII redaction (spec §10.2)."""
    for item in pii:
        text = text.replace(item.value, REDACTION_TOKENS[item.kind])
    return text


def session_gold(script: Script) -> dict[str, Any]:
    """One ``gold.jsonl`` record."""
    return {
        "script_ref": script.script_ref,
        "template": script.template,
        "n_utterances": len(script.utterances),
        "total_ms": script.total_ms,
        "risk_kinds": script.meta.risk_kinds,
        "facts": session_facts(script),
        "risk": risk_spans(script),
        "expected_alerts": expected_alerts(script),
        "pii": pii_items(script),
        "injection_utterances": script.meta.injection_utterances,
    }
