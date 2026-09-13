"""Adoption decisions — the road not taken, measured on the same fixed set (``adoption.json``).

The shipped detector and the shipped draft selector each embody choices a reasonable reader of the
spec could have made differently. Instead of arguing about them, this harness scores the shipped
policy and its rival on the same corpus, applies a rule that is declared *here, in code, per family*,
and records adopt / reject together with the numbers that decided it. ``docs/eval/adoption.json`` is
the result; README and the console home copy its numbers through the marker pipeline (§11.4).

Rules (also written into the report so a reader never has to open this file):

* ``risk`` — decision set: the frozen held-out sentences (300; ``FROZEN.txt`` is checked first).
  Primary: **precision**, adopt needs Δ ≥ +0.02. Guards: total recall not lower; **no category's
  recall lower** — a safety net that trades one suicidal-ideation sentence for cleaner precision has
  become thinner, whatever the headline number says.
* ``notes`` — decision set: the eval corpus (200 sessions, seed 42; ``docs/eval/README.md``).
  Primary: **macro fact recall** — the mean of the per-type recalls — adopt needs Δ ≥ +0.02. The micro
  total is reported but does not decide: ``duration`` alone is about half of the gold facts, so a
  selector that drops every medication and alcohol fact can *raise* the micro total (interpretation
  rule 2). Guards: coverage not lower, abstain rate not higher, fact types at recall 0 not more.

What a row is: ``baseline`` and ``candidate`` are two policies of the same code
(:class:`chartwire.risk.detector.Policy`, :class:`chartwire.notes.extractive.Selection`) and
``shipped`` says which of them runs today. A row's decision must agree with what ships —
``tests/unit/test_eval_adoption.py`` fails when the rule adopts something the code does not run, or the
code runs something the rule rejects. That is the point of writing the rule down.

Leakage (§10.3): the held-out set is scored once per candidate *version*. Candidates are principled
variants — the spec's literal wording, a threshold, full suppression — declared without reading a
sentence, and :data:`CANDIDATES` is fixed: it is not extended until a variant passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any, Final, Literal

from chartwire.eval import corpus, grounding_eval, risk_eval
from chartwire.notes.extractive import DEFAULT_SELECTION, Selection
from chartwire.risk.detector import DEFAULT_POLICY, DETECTOR_VERSION, Policy
from chartwire.synth.scripts import Script

Family = Literal["risk", "notes"]
Decision = Literal["adopt", "reject", "not_measured"]
Direction = Literal["not_lower", "not_higher", "each_not_lower"]
Metrics = dict[str, Any]

MIN_DELTA: Final = 0.02
"""Smallest primary-metric gain that counts as an improvement rather than noise on these set sizes."""
_EPS: Final = 1e-9


@dataclass(frozen=True)
class Guard:
    metric: str
    direction: Direction
    why: str
    """One line, Korean, printed next to the check so the reader sees what the guard protects."""

    def check(self, baseline: Metrics, candidate: Metrics) -> tuple[bool, str]:
        b, c = baseline[self.metric], candidate[self.metric]
        if self.direction == "each_not_lower":
            worse = {k: (b[k], c.get(k, 0.0)) for k in b if c.get(k, 0.0) < b[k] - _EPS}
            detail = "; ".join(f"{k} {bv:.4f}→{cv:.4f}" for k, (bv, cv) in sorted(worse.items()))
            return not worse, detail or "모든 항목 유지"
        if self.direction == "not_lower":
            return c >= b - _EPS, f"{b:.4f}→{c:.4f}"
        return c <= b + _EPS, f"{b:.4f}→{c:.4f}"


@dataclass(frozen=True)
class Rule:
    primary: str
    min_delta: float
    guards: tuple[Guard, ...]
    decision_set: str
    """Korean, what corpus decides this family."""

    def decide(self, baseline: Metrics, candidate: Metrics) -> tuple[Decision, list[dict[str, Any]]]:
        delta = candidate[self.primary] - baseline[self.primary]
        checks: list[dict[str, Any]] = [
            {
                "name": f"{self.primary} Δ ≥ +{self.min_delta:g}",
                "ok": delta >= self.min_delta - _EPS,
                "detail": f"{baseline[self.primary]:.4f}→{candidate[self.primary]:.4f} ({delta:+.4f})",
            }
        ]
        for guard in self.guards:
            ok, detail = guard.check(baseline, candidate)
            checks.append(
                {"name": f"{guard.metric} {guard.direction}", "ok": ok, "detail": detail, "why": guard.why}
            )
        return ("adopt" if all(c["ok"] for c in checks) else "reject"), checks

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_set": self.decision_set,
            "primary": self.primary,
            "min_delta": self.min_delta,
            "guards": [{"metric": g.metric, "direction": g.direction, "why": g.why} for g in self.guards],
        }


RISK_RULE: Final = Rule(
    primary="precision",
    min_delta=MIN_DELTA,
    guards=(
        Guard("recall", "not_lower", "안전망의 재현율은 정밀도를 사서 내릴 수 없다"),
        Guard(
            "category_recall", "each_not_lower", "총합이 유지돼도 자살사고 한 문장을 잃으면 그물이 얇아진 것"
        ),
    ),
    decision_set="동결 held-out 300문장 (FROZEN.txt, 튜닝 대상 아님 — 후보 버전당 한 번만 본다)",
)
NOTES_RULE: Final = Rule(
    primary="fact_recall_macro",
    min_delta=MIN_DELTA,
    guards=(
        Guard("coverage", "not_lower", "근거 없는 문장이 늘어나면 안 된다"),
        Guard("abstain_rate", "not_higher", "초안을 못 내는 세션이 늘어나면 안 된다"),
        Guard("fact_types_at_zero", "not_higher", "어떤 사실 유형도 통째로 빠지면 안 된다 — 총합이 올라도"),
    ),
    decision_set="eval 코퍼스 200 세션 (seed 42) · 1차 지표는 유형별 재현율의 평균(macro) — 총합(micro)은 duration 이 절반이라 결정에 쓰지 않는다",
)
RULES: Final[dict[Family, Rule]] = {"risk": RISK_RULE, "notes": NOTES_RULE}


@dataclass(frozen=True)
class Candidate:
    id: str
    family: Family
    title: str
    change: str
    """Korean: what the candidate does differently from the baseline, in one or two sentences."""
    baseline_label: str
    candidate_label: str
    baseline: Policy | Selection | None
    candidate: Policy | Selection | None
    """``None`` on both sides marks a row that is listed but not measured (the LLM tier)."""
    not_measured_because: str = ""

    @property
    def measured(self) -> bool:
        return self.baseline is not None and self.candidate is not None

    def shipped(self) -> Literal["baseline", "candidate", "neither"]:
        """Which side is the code that runs today (whole policy equality, not one field)."""
        default: Policy | Selection = DEFAULT_POLICY if self.family == "risk" else DEFAULT_SELECTION
        if self.candidate == default:
            return "candidate"
        if self.baseline == default or not self.measured:
            return "baseline"
        return "neither"


CANDIDATES: Final[tuple[Candidate, ...]] = (
    Candidate(
        id="risk.past_split",
        family="risk",
        title="과거 사고를 두 갈래로 — 지금은 아니라고 하면 억제, 아니면 한 등급 낮춰 경보",
        change=(
            "스펙 §9.4 원문은 과거 표지가 있으면 무조건 severity −1 이고, 「작년엔 죽고 싶었는데 지금은 아니에요」도 "
            "경보한다. 후보는 히트 뒤에 현재 표지 + 부정이 오면 억제하고, 없으면 원문대로 한 등급 낮춰 경보한다."
        ),
        baseline_label="§9.4 원문 (과거 표지 → severity −1, 부인 무시)",
        candidate_label="lex-1 (부인 있으면 억제 · 없으면 severity −1)",
        baseline=Policy(past="spec_literal"),
        candidate=Policy(past="split"),
    ),
    Candidate(
        id="risk.past_suppress_all",
        family="risk",
        title="과거 표지가 있으면 전부 억제",
        change=(
            "남은 오탐의 가장 큰 갈래가 past 다. 과거 표지가 보이면 부인 여부와 관계없이 경보를 끄면 그 오탐이 "
            "거의 사라진다 — 대신 「작년부터 죽고 싶었어요」처럼 지금도 이어지는 사고까지 조용해진다."
        ),
        baseline_label="lex-1",
        candidate_label="past → 무조건 억제",
        baseline=Policy(past="split"),
        candidate=Policy(past="suppress_all"),
    ),
    Candidate(
        id="risk.severity_floor_2",
        family="risk",
        title="severity 2 부터만 경보",
        change="가장 약한 등급(1)을 경보에서 뺀다. 「살아야 할 이유를 모르겠어요」 같은 문장이 조용해진다.",
        baseline_label="lex-1 (severity ≥ 1)",
        candidate_label="severity ≥ 2",
        baseline=Policy(min_severity=1),
        candidate=Policy(min_severity=2),
    ),
    Candidate(
        id="notes.selection",
        family="notes",
        title="사실 가족마다 한 문장씩 먼저, 그 안에서는 숫자 있는 문장 먼저",
        change=(
            "섹션당 12문장 상한을 seq 순으로 채우면 이른 단계(수면·기간)가 자리를 다 차지해 약물·음주 사실이 통째로 "
            "빠진다. 후보는 §10.1 사실 가족마다 한 문장씩 먼저 뽑고, 같은 가족 안에서는 10mg · 5시간처럼 숫자가 있는 "
            "문장을 먼저 뽑는다. 출력 순서는 그대로 seq 다."
        ),
        baseline_label="seq 순으로 12칸 채우기",
        candidate_label="가족 우선 + 숫자 우선 (지금 코드)",
        baseline=Selection(family_first=False, quantified_first=False),
        candidate=Selection(family_first=True, quantified_first=True),
    ),
    Candidate(
        id="notes.family_first_alone",
        family="notes",
        title="가족 우선만 (숫자 우선 없이)",
        change=(
            "위 변경의 첫 절반만 적용한 상태 — 가족마다 한 문장씩 뽑되 그 안에서는 seq 순. 가족의 첫 발화가 대개 "
            "막연한 문장(「잠을 잘 못 자요」)이라 숫자가 있는 사실이 뒤로 밀린다."
        ),
        baseline_label="seq 순으로 12칸 채우기",
        candidate_label="가족 우선만",
        baseline=Selection(family_first=False, quantified_first=False),
        candidate=Selection(family_first=True, quantified_first=False),
    ),
    Candidate(
        id="notes.anthropic_provider",
        family="notes",
        title="LLM 프로바이더 (같은 검증기 뒤에서)",
        change=(
            "AnthropicProvider 는 추출형과 같은 스키마·같은 8규칙 검증기 뒤에 붙는다. 키 없이 빌드하므로 실측이 없고, "
            "픽스처 7건(assessment 키 · 환각 인용 · 숫자 불일치 · 주입 …)은 검증기 회귀 테스트이지 비교 측정이 아니다."
        ),
        baseline_label="추출형 (지금 코드)",
        candidate_label="Anthropic 프로바이더",
        baseline=None,
        candidate=None,
        not_measured_because="실제 키로 돌린 측정이 없다 — 재지 않은 것은 채택하지 않는다 (docs/eval/README.md `anthropic.json`)",
    ),
)


# --------------------------------------------------------------------------- metrics


def risk_metrics(rows: list[dict[str, Any]], policy: Policy) -> Metrics:
    report = risk_eval.evaluate_heldout(rows, policy=policy)
    return {
        "precision": report["precision"],
        "recall": report["recall"],
        "f1": report["f1"],
        "tp": report["tp"],
        "fp": report["fp"],
        "fn": report["fn"],
        "past_fp": report["per_kind"].get("past", {}).get("fp", 0),
        "category_recall": {c: v["recall"] for c, v in report["per_category"].items() if c != "none"},
    }


def notes_metrics(scripts: list[Script], selection: Selection) -> Metrics:
    report = grounding_eval.evaluate(scripts, selection=selection)
    by_type = {t: v["recall"] for t, v in report["fact_recall_by_type"].items()}
    return {
        "fact_recall_macro": round(fmean(by_type.values()), 4) if by_type else 0.0,
        "fact_recall": report["fact_recall"],
        "fact_types": len(by_type),
        "fact_types_at_zero": sum(1 for r in by_type.values() if r == 0),
        "coverage": report["coverage"],
        "abstain_rate": report["abstain_rate"],
        "statements_per_session": report["statements_per_session"],
        "recall_by_type": by_type,
    }


def _delta(baseline: Metrics, candidate: Metrics) -> dict[str, float]:
    return {
        k: round(candidate[k] - baseline[k], 4)
        for k, v in baseline.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }


def _reason(decision: Decision, checks: list[dict[str, Any]]) -> str:
    if decision == "adopt":
        return "1차 지표가 기준을 넘었고 보호 지표를 하나도 잃지 않았다 — " + checks[0]["detail"]
    failed = [c for c in checks if not c["ok"]]
    return "기각 — " + "; ".join(f"{c['name']}: {c['detail']}" for c in failed)


def _policy_dict(policy: Policy | Selection | None) -> dict[str, Any] | None:
    return None if policy is None else dict(vars(policy))


def evaluate(rows: list[dict[str, Any]], scripts: list[Script], *, seed: int) -> dict[str, Any]:
    """Score every candidate against its baseline and decide; the body of ``adoption.json``."""
    out: list[dict[str, Any]] = []
    counts = {"adopt": 0, "reject": 0, "not_measured": 0}
    for cand in CANDIDATES:
        row: dict[str, Any] = {
            "id": cand.id,
            "family": cand.family,
            "title": cand.title,
            "change": cand.change,
            "baseline_label": cand.baseline_label,
            "candidate_label": cand.candidate_label,
            "baseline_policy": _policy_dict(cand.baseline),
            "candidate_policy": _policy_dict(cand.candidate),
            "shipped": cand.shipped(),
        }
        if not cand.measured:
            row.update(decision="not_measured", reason=cand.not_measured_because, checks=[])
        else:
            if cand.family == "risk":
                assert isinstance(cand.baseline, Policy) and isinstance(cand.candidate, Policy)
                base, new = risk_metrics(rows, cand.baseline), risk_metrics(rows, cand.candidate)
            else:
                assert isinstance(cand.baseline, Selection) and isinstance(cand.candidate, Selection)
                base, new = notes_metrics(scripts, cand.baseline), notes_metrics(scripts, cand.candidate)
            decision, checks = RULES[cand.family].decide(base, new)
            row.update(
                baseline=base, candidate=new, delta=_delta(base, new), checks=checks, decision=decision
            )
            row["reason"] = _reason(decision, checks)
        counts[row["decision"]] += 1
        out.append(row)
    return {
        "rules": {family: rule.as_dict() for family, rule in RULES.items()},
        "decision_sets": {
            "risk": {
                "set": "heldout",
                "n": len(rows),
                "frozen_sha256": corpus.sha256_file(corpus.HELDOUT_FILE),
            },
            "notes": {"set": "eval_scripts", "seed": seed, "n_sessions": len(scripts)},
        },
        "shipped": {
            "risk": {"detector_version": DETECTOR_VERSION, **vars(DEFAULT_POLICY)},
            "notes": dict(vars(DEFAULT_SELECTION)),
        },
        "candidates": out,
        "counts": counts,
    }


def redecide(row: dict[str, Any]) -> Decision:
    """The decision the stored numbers imply — what the consistency test compares to ``row['decision']``."""
    if row.get("decision") == "not_measured":
        return "not_measured"
    family: Family = row["family"]
    decision, _ = RULES[family].decide(row["baseline"], row["candidate"])
    return decision


def shipped_agrees(row: dict[str, Any]) -> bool:
    """A row may not adopt what does not run, nor reject what does (``neither`` rows are ablation steps)."""
    shipped, decision = row["shipped"], row["decision"]
    if shipped == "candidate":
        return decision == "adopt"
    if shipped == "baseline":
        return decision != "adopt"
    return True
