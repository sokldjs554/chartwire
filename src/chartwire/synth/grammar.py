"""Session grammar (spec §10.2): template rendering and the phase sequence.

A consultation is a fixed sequence of phases.  Each phase yields a clinician
opener, 2–4 patient statements, one follow-up pair and optional filler /
observation utterances.  Everything is drawn from a ``random.Random`` passed
by the caller, so a session is a pure function of its seed.
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


def render(template: v.Template, rng: random.Random) -> tuple[str, list[Fact]]:
    """Fill a template's slots; return the text and its gold facts."""
    values: dict[str, str | int] = {name: rng.choice(choices) for name, choices in template.slots.items()}
    fact_fields: dict[str, str | int] = dict(values)
    if "{drug" in template.text:
        drug = rng.choice(sorted(v.DRUGS))
        dose = rng.choice(v.DRUGS[drug])
        values.update(
            drug=drug, dose=dose, drug_eul=drug + v.josa(drug, "을/를"), drug_eun=drug + v.josa(drug, "은/는")
        )
        fact_fields.update(name=drug)
        if "{dose}" in template.text:
            fact_fields.update(dose=f"{dose}mg")
        if template.fact == "plan_medication":
            fact_fields.update(action="increase" if "{dose}" in template.text else "keep")
    text = template.text.format(**values)
    if template.fact is None:
        return text, []
    return text, [{"type": template.fact, **fact_fields}]


def _patient(template: v.Template, rng: random.Random) -> Utt:
    text, facts = render(template, rng)
    return Utt("patient", text, template.section, facts)


def _clinician(template: v.Template, rng: random.Random) -> Utt:
    text, facts = render(template, rng)
    return Utt("clinician", text, template.section, facts)


def _sample(pool: tuple[v.Template, ...], k: int, rng: random.Random) -> list[v.Template]:
    return rng.sample(pool, min(k, len(pool)))


def _standard_phase(phase: Phase, opening_pool: tuple[v.Template, ...], rng: random.Random) -> list[Utt]:
    out = [Utt("clinician", rng.choice(v.QUESTIONS[phase.question_key]))]
    if rng.random() < FILLER_P:
        out.append(Utt("patient", rng.choice(v.FILLERS)))
    pool = opening_pool or phase.patient_pool
    out.extend(_patient(t, rng) for t in _sample(pool, rng.randint(*phase.n_statements), rng))
    if phase.follow_up:
        out.append(Utt("clinician", rng.choice(v.FOLLOW_UPS)))
        out.append(_patient(rng.choice(v.DURATION), rng))
    if rng.random() < phase.observation_p:
        out.append(_clinician(rng.choice(v.OBSERVATIONS), rng))
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


def plan_phase(rng: random.Random) -> list[Utt]:
    out = [Utt("clinician", rng.choice(v.QUESTIONS["plan"]))]
    out.extend(_clinician(t, rng) for t in _sample(v.PLANS[:2] + v.PLANS[3:], rng.randint(1, 3), rng))
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
            utts.extend(plan_phase(rng))
        elif phase.question_key == "closing":
            utts.extend(closing_phase(rng))
        elif phase.question_key == "greeting":
            openings = tuple(v.Template(text) for text in chief.openings)
            utts.extend(_standard_phase(phase, openings, rng))
        else:
            utts.extend(_standard_phase(phase, (), rng))
    return utts
