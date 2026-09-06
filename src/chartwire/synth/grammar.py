"""Session grammar (spec §10.2): template rendering and the phase sequence.

A consultation is a fixed sequence of phases (인사·주호소 → 수면 → … → 마무리).  What happens
inside a phase follows the chief complaint (:class:`~chartwire.synth.vocab_ko.ChiefComplaint`):
the greeting phase explores the complaint with a clinician probe and complaint-specific answers,
*focus* phases get 3–4 statements including a complaint-specific one, *brief* phases get one short
answer and no follow-up, and the plan always carries a complaint-specific item.  Everything is
drawn from a ``random.Random`` passed by the caller, so a session is a pure function of its seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal

from chartwire.synth import vocab_ko as v

Speaker = Literal["clinician", "patient"]
Fact = dict[str, str | int]


@dataclass
class Utt:
    """An utterance before timing is assigned (gold attached, text mutable)."""

    speaker: Speaker
    text: str
    section: v.Section = "none"
    facts: list[Fact] = field(default_factory=list)
    risk: v.RiskCase | None = None
    pii: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Phase:
    name: str
    question_key: str
    patient_pool: tuple[v.Template, ...]
    n_statements: tuple[int, int] = (2, 4)
    follow_up: bool = True
    observation_p: float = 0.0


PHASES: tuple[Phase, ...] = (
    Phase("인사·주호소", "greeting", (), n_statements=(1, 2), observation_p=0.6),
    Phase("수면", "sleep", v.SLEEP),
    Phase("식욕/체중", "appetite", v.APPETITE),
    Phase("기분·흥미·에너지", "mood", v.MOOD + v.ANXIETY, observation_p=0.7),
    Phase("집중·직장/학교", "concentration", v.CONCENTRATION, n_statements=(2, 2)),
    Phase("약물·부작용", "medication", v.MEDICATION),
    Phase("음주", "alcohol", v.ALCOHOL, n_statements=(1, 1)),
    Phase("위험 평가", "risk", (), follow_up=False),
    Phase("계획", "plan", (), follow_up=False),
    Phase("마무리", "closing", (), follow_up=False),
)
FILLER_P = 0.4
BACKCHANNEL_P = 0.5  # clinician "네." between patient statements when a phase has three or more
FOCUS_STATEMENTS = (3, 4)
DETAIL_STATEMENTS = (2, 3)  # answers to the complaint probe in 인사·주호소


def plan_action(text: str) -> str:
    """``plan_medication`` action from the plan template's verb."""
    if "올려" in text:
        return "increase"
    if "줄여" in text:
        return "decrease"
    if "처방" in text:
        return "start"
    return "keep"


def render(template: v.Template, rng: random.Random, drugs: tuple[str, ...] = ()) -> tuple[str, list[Fact]]:
    """Fill a template's slots; return the text and its gold facts.

    ``drugs`` narrows the drug table to the complaint's plausible medication (empty = any drug);
    a newly *started* drug gets its lowest listed dose.
    """
    values: dict[str, str | int] = {name: rng.choice(choices) for name, choices in template.slots.items()}
    fact_fields: dict[str, str | int] = dict(values)
    if "{drug" in template.text:
        drug = rng.choice(drugs or tuple(sorted(v.DRUGS)))
        action = plan_action(template.text) if template.fact == "plan_medication" else None
        dose = v.DRUGS[drug][0] if action == "start" else rng.choice(v.DRUGS[drug])
        values.update(
            drug=drug, dose=dose, drug_eul=drug + v.josa(drug, "을/를"), drug_eun=drug + v.josa(drug, "은/는")
        )
        fact_fields.update(name=drug)
        if "{dose}" in template.text:
            fact_fields.update(dose=f"{dose}mg")
        if action is not None:
            fact_fields.update(action=action)
    text = template.text.format(**values)
    if template.fact is None:
        return text, []
    return text, [{"type": template.fact, **fact_fields}]


def _patient(template: v.Template, rng: random.Random, drugs: tuple[str, ...] = ()) -> Utt:
    text, facts = render(template, rng, drugs)
    return Utt("patient", text, template.section, facts)


def _clinician(template: v.Template, rng: random.Random, drugs: tuple[str, ...] = ()) -> Utt:
    text, facts = render(template, rng, drugs)
    return Utt("clinician", text, template.section, facts)


def _sample(pool: tuple[v.Template, ...], k: int, rng: random.Random) -> list[v.Template]:
    return rng.sample(pool, min(k, len(pool)))


def _sample_with(
    pool: tuple[v.Template, ...], must: tuple[v.Template, ...], k: int, rng: random.Random
) -> list[v.Template]:
    """``k`` templates from ``pool``, at least one of them from ``must`` (when ``must`` is non-empty)."""
    picks = _sample(pool, k, rng)
    if must and not any(t in must for t in picks):
        picks[rng.randrange(len(picks))] = rng.choice(must)
    return picks


def _statements(
    templates: list[v.Template], rng: random.Random, drugs: tuple[str, ...], *, backchannel: bool
) -> list[Utt]:
    """Patient statements in a row; with three or more, the clinician may acknowledge in between."""
    out: list[Utt] = []
    for i, t in enumerate(templates):
        if backchannel and i and len(templates) >= 3 and rng.random() < BACKCHANNEL_P:
            out.append(Utt("clinician", rng.choice(v.BACKCHANNELS)))
        out.append(_patient(t, rng, drugs))
    return out


def _question(phase: Phase, chief: v.ChiefComplaint, rng: random.Random) -> str:
    key = phase.question_key
    if key == "medication" and not chief.on_medication:
        key = "medication_first"
    return rng.choice(v.QUESTIONS[key])


def greeting_phase(phase: Phase, chief: v.ChiefComplaint, rng: random.Random) -> list[Utt]:
    """인사·주호소: greeting → opening → complaint probe → detail → duration → observation."""
    out = [Utt("clinician", v.QUESTIONS["greeting"][1 if chief.revisit else 0])]
    if rng.random() < FILLER_P:
        out.append(Utt("patient", rng.choice(v.FILLERS)))
    openings = tuple(v.Template(text) for text in chief.openings)
    out.extend(_patient(t, rng) for t in _sample(openings, rng.randint(*phase.n_statements), rng))
    out.append(Utt("clinician", rng.choice(chief.probes)))
    detail = _sample(chief.detail, rng.randint(*DETAIL_STATEMENTS), rng)
    out.extend(_statements(detail, rng, chief.drugs, backchannel=True))
    out.append(Utt("clinician", rng.choice(v.FOLLOW_UPS)))
    out.append(_patient(rng.choice(v.DURATION), rng))
    if rng.random() < phase.observation_p:
        out.append(_clinician(rng.choice(v.OBSERVATIONS + chief.observations), rng))
    return out


def _standard_phase(phase: Phase, chief: v.ChiefComplaint, rng: random.Random) -> list[Utt]:
    key = phase.question_key
    out = [Utt("clinician", _question(phase, chief, rng))]
    if rng.random() < FILLER_P:
        out.append(Utt("patient", rng.choice(v.FILLERS)))
    if key in chief.brief:
        out.append(_patient(rng.choice(v.BRIEF_ANSWERS[key]), rng))
        return out
    pool = chief.pool(key, phase.patient_pool)
    focus = key in chief.focus
    lo, hi = FOCUS_STATEMENTS if focus else phase.n_statements
    must = chief.extra.get(key, ()) if focus else ()
    picks = _sample_with(pool, must, rng.randint(lo, hi), rng)
    out.extend(_statements(picks, rng, chief.drugs, backchannel=focus))
    if phase.follow_up:
        out.append(Utt("clinician", rng.choice(v.FOLLOW_UPS)))
        out.append(_patient(rng.choice(v.DURATION), rng))
    if rng.random() < phase.observation_p:
        out.append(_clinician(rng.choice(v.OBSERVATIONS + chief.observations), rng))
    return out


def risk_phase(rng: random.Random, answer: v.RiskCase | None) -> list[Utt]:
    """Clinician risk question + patient answer (``None`` → neutral, risk-free answer)."""
    question = v.RISK_QUESTIONS[0] if answer is None else rng.choice(v.RISK_QUESTIONS)
    out = [Utt("clinician", question.text, risk=question)]
    if answer is None:
        out.append(Utt("patient", rng.choice(v.NEUTRAL_RISK_ANSWERS)))
    elif answer.kind == "clinician_question":
        # The injected case *is* a clinician question: patient answers neutrally, clinician probes again.
        out.append(Utt("patient", rng.choice(v.NEUTRAL_RISK_ANSWERS)))
        out.append(Utt("clinician", answer.text, risk=answer))
        out.append(Utt("patient", rng.choice(v.NEUTRAL_RISK_ANSWERS)))
    else:
        out.append(Utt("patient", answer.text, "S", risk=answer))
    return out


def plan_phase(rng: random.Random, chief: v.ChiefComplaint) -> list[Utt]:
    """계획: 2–3 plan items, one of them complaint-specific; dose changes only for a medicated patient."""
    out = [Utt("clinician", rng.choice(v.QUESTIONS["plan"]))]
    generic = (v.PLANS[:2] if chief.on_medication else ()) + v.PLANS[3:]
    picks = _sample_with(generic + chief.plans, chief.plans, rng.randint(2, 3), rng)
    out.extend(_clinician(t, rng, chief.drugs) for t in picks)
    out.append(_patient(rng.choice(v.ACKNOWLEDGE[:2]), rng))
    return out


def closing_phase(rng: random.Random) -> list[Utt]:
    return [
        _clinician(v.PLANS[2], rng),  # "{weeks}주 뒤에 뵙겠습니다"
        _patient(v.ACKNOWLEDGE[2], rng),
        Utt("clinician", rng.choice(v.QUESTIONS["closing"])),
    ]


def build_session(rng: random.Random, chief: v.ChiefComplaint, risk_answer: v.RiskCase | None) -> list[Utt]:
    """Run every phase in order and return the untimed utterance list."""
    utts: list[Utt] = []
    for phase in PHASES:
        if phase.question_key == "risk":
            utts.extend(risk_phase(rng, risk_answer))
        elif phase.question_key == "plan":
            utts.extend(plan_phase(rng, chief))
        elif phase.question_key == "closing":
            utts.extend(closing_phase(rng))
        elif phase.question_key == "greeting":
            utts.extend(greeting_phase(phase, chief, rng))
        else:
            utts.extend(_standard_phase(phase, chief, rng))
    return utts
