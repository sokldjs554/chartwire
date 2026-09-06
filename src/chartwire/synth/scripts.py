"""Script generation: the shared script JSON contract and the seed-driven generator.

Contract (one file per script, ``<scripts_dir>/<script_ref>.json``) is
modelled by :class:`Script`; consumers (STT simulator, load-test clients)
should validate with ``Script.model_validate_json``.

Design decisions recorded here (spec §10.2 / §10.3):

* A script is a pure function of ``(base_seed, script_ref)``: the per-script
  RNG is seeded with ``sha256(f"{seed}:{script_ref}")``.
* The chief complaint shapes the whole dialogue (:mod:`grammar`): the eval
  profile draws it at random per script (distribution over the 8 templates);
  the demo profile assigns it round-robin (``s01`` = 초진 우울 … ``s08`` = 강박,
  ``s09`` = 초진 우울 …) so the 20 console scripts cover every template.
* Every session carries the clinician's risk question (kind
  ``clinician_question``).  In 50 % of sessions the patient's 위험 평가 answer
  is a *positive* case plus 0–2 further positives elsewhere (≈220 alert-worthy
  utterances / 200 sessions, ≈100 sessions with alerts for the alert-latency
  eval).  Independently, 25 % of sessions receive a *suppressed* kind drawn
  uniformly over ``negated | hypothetical | past | third_person |
  clinician_question | idiom``: 2–4 utterances of that kind, and — when the
  session has no positive answer — the 위험 평가 answer itself (≈330
  suppressed-kind utterances / 200 sessions, spec §10.3 table).
* 5 % of utterances receive one synthetic PII fragment (name/phone/address).
* Injection sessions (``inject=True``) carry 1–2 prompt-injection utterances
  spoken by the patient; their indexes are listed in ``meta.injection_utterances``.
* Timing: ``t_end = t_start + 180 ms × chars + U(400, 900) ms``; the next
  utterance starts at the previous ``t_end`` (contiguous, non-overlapping).
"""

from __future__ import annotations

import hashlib
import random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chartwire.synth import grammar
from chartwire.synth import vocab_ko as v

GENERATOR = "chartwire.synth"
VERSION = "1"
CHUNK_MS = 200
POSITIVE_SESSION_P = 0.5
SUPPRESSED_SESSION_P = 0.25
SUPPRESSED_KINDS: tuple[v.RiskKind, ...] = tuple(k for k in v.RISK_KINDS if k != "positive")
PII_P = 0.05
MS_PER_CHAR = 180
PAUSE_MS = (400, 900)
TAIL_MS = 1000
Profile = Literal["eval", "demo"]
PROFILE_SEED: dict[str, int] = {"demo": 1, "eval": 42}
INJECTION_EVERY = 10  # eval profile: every 10th script is an injection session (20 of 200)


class GoldFact(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str


class GoldRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: v.RiskCategory | None
    severity: int = Field(ge=0, le=3)
    kind: v.RiskKind


class GoldPii(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["name", "phone", "address"]
    value: str


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_label: v.Section
    facts: list[GoldFact]
    risk: GoldRisk | None
    pii: list[GoldPii]


class Utterance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int
    speaker: grammar.Speaker
    text: str
    t_start_ms: int
    t_end_ms: int
    gold: Gold


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generator: str = GENERATOR
    version: str = VERSION
    risk_kinds: list[v.RiskKind] = Field(default_factory=list)
    injection: bool = False
    injection_utterances: list[int] = Field(default_factory=list)


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_ref: str
    seed: int
    template: str
    chunk_ms: int
    total_ms: int
    utterances: list[Utterance]
    meta: Meta


def script_rng(seed: int, script_ref: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{script_ref}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _patient_slots(utts: list[grammar.Utt]) -> list[int]:
    """Indexes at which a patient utterance can be inserted (right after a clinician turn)."""
    return [i + 1 for i, u in enumerate(utts) if u.speaker == "clinician" and u.risk is None]


def _insert_risk_extras(utts: list[grammar.Utt], kind: v.RiskKind, count: int, rng: random.Random) -> None:
    for _ in range(count):
        case = rng.choice(v.RISK_CASES[kind])
        section: v.Section = "S" if case.speaker == "patient" else "none"
        slots = _patient_slots(utts) if case.speaker == "patient" else list(range(1, len(utts)))
        utts.insert(rng.choice(slots), grammar.Utt(case.speaker, case.text, section, risk=case))


def _insert_injections(utts: list[grammar.Utt], rng: random.Random) -> list[grammar.Utt]:
    picked = rng.sample(v.INJECTIONS, rng.randint(1, 2))
    injected = [grammar.Utt("patient", text) for text in picked]
    for utt in injected:
        utts.insert(rng.choice(_patient_slots(utts)), utt)
    return injected


def _inject_pii(utt: grammar.Utt, rng: random.Random) -> None:
    kind = rng.choice(("name", "phone", "address"))
    if kind == "name":
        value = v.synth_name(rng)
        suffix = (
            f" {value} 선생님이 소개해 주셨어요."
            if utt.speaker == "patient"
            else f" {value}님도 그렇게 말씀하셨죠."
        )
    elif kind == "phone":
        value = v.synth_phone(rng)
        suffix = (
            f" 제 번호는 {value}예요." if utt.speaker == "patient" else f" 연락은 {value}로 드리겠습니다."
        )
    else:
        value = v.synth_address(rng)
        suffix = f" 집은 {value}예요." if utt.speaker == "patient" else f" 주소가 {value} 맞으시죠?"
    utt.text += suffix
    utt.pii.append({"kind": kind, "value": value})


def _timed(utts: list[grammar.Utt], rng: random.Random) -> list[Utterance]:
    out: list[Utterance] = []
    t = 0
    for idx, u in enumerate(utts):
        t_end = t + MS_PER_CHAR * len(u.text) + rng.randint(*PAUSE_MS)
        risk = (
            None
            if u.risk is None
            else GoldRisk(category=u.risk.category, severity=u.risk.severity, kind=u.risk.kind)
        )
        gold = Gold(
            section_label=u.section,
            facts=[GoldFact.model_validate(f) for f in u.facts],
            risk=risk,
            pii=[GoldPii.model_validate(p) for p in u.pii],
        )
        out.append(
            Utterance(idx=idx, speaker=u.speaker, text=u.text, t_start_ms=t, t_end_ms=t_end, gold=gold)
        )
        t = t_end
    return out


def generate_script(
    script_ref: str,
    seed: int,
    *,
    inject: bool = False,
    chunk_ms: int = CHUNK_MS,
    chief: v.ChiefComplaint | None = None,
) -> Script:
    """Generate one script deterministically from ``(seed, script_ref)``.

    ``chief`` pins the consultation template (demo profile); ``None`` draws it from the script RNG.
    """
    rng = script_rng(seed, script_ref)
    if chief is None:
        chief = rng.choice(v.CHIEF_COMPLAINTS)
    positive = rng.random() < POSITIVE_SESSION_P
    suppressed: v.RiskKind | None = (
        rng.choice(SUPPRESSED_KINDS) if rng.random() < SUPPRESSED_SESSION_P else None
    )
    answer_kind: v.RiskKind | None = "positive" if positive else suppressed
    answer = None if answer_kind is None else rng.choice(v.RISK_CASES[answer_kind])
    utts = grammar.build_session(rng, chief, answer)
    if positive:
        _insert_risk_extras(utts, "positive", rng.randint(0, 2), rng)
    if suppressed is not None:
        _insert_risk_extras(utts, suppressed, rng.randint(2, 4), rng)
    injected = _insert_injections(utts, rng) if inject else []
    for u in utts:
        if rng.random() < PII_P:
            _inject_pii(u, rng)
    utterances = _timed(utts, rng)
    risk_kinds: list[v.RiskKind] = ["positive"] if positive else []
    if suppressed is not None:
        risk_kinds.append(suppressed)
    meta = Meta(
        risk_kinds=risk_kinds,
        injection=inject,
        injection_utterances=[i for i, u in enumerate(utts) if any(u is x for x in injected)],
    )
    return Script(
        script_ref=script_ref,
        seed=seed,
        template=chief.name,
        chunk_ms=chunk_ms,
        total_ms=utterances[-1].t_end_ms + TAIL_MS,
        utterances=utterances,
        meta=meta,
    )


def script_refs(profile: Profile, n: int) -> list[str]:
    return [f"s{i:02d}" if profile == "demo" else f"e{i:04d}" for i in range(1, n + 1)]


def generate_set(profile: Profile, n: int, seed: int | None = None) -> list[Script]:
    """Generate ``n`` scripts for a profile (demo → ``s01..``, eval → ``e0001..``)."""
    base_seed = PROFILE_SEED[profile] if seed is None else seed
    return [
        generate_script(
            ref,
            base_seed,
            inject=profile == "eval" and i % INJECTION_EVERY == 0,
            chief=v.CHIEF_COMPLAINTS[(i - 1) % len(v.CHIEF_COMPLAINTS)] if profile == "demo" else None,
        )
        for i, ref in enumerate(script_refs(profile, n), start=1)
    ]
