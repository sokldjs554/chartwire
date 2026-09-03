#!/usr/bin/env python3
"""README number pipeline (spec §0 rule 2, §11.4).

Every measured number in ``README.md`` sits inside a marker::

    | N=200 ack p95 | <!-- num:load.A.n200.ack_p95_ms -->—<!-- /num --> ms |

and a table row (or any block) that must disappear when its numbers were
never measured is wrapped in::

    <!-- row:load.A.n200 --> ... <!-- /row -->

``KEYS`` maps every dotted key to ``<json file>:<path>`` under the repo root.
Path grammar (deliberately tiny): ``$`` root, ``.name`` object member,
``[3]`` list index, ``[?field==value]`` first list element whose ``field``
equals ``value`` (int / float / bool / string).

Modes:

* ``--write``  fill every ``num`` marker; delete every ``row`` block in which at
  least one key cannot be resolved (missing file, missing path, ``null``).
* ``--check``  exit 1 when ``--write`` would change the README (stale value or
  unmeasured row still present); exit 2 for an unregistered key.
* ``--list``   print the registry.

Stdlib only: CI runs it without installing the package.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_README = "README.md"


@dataclass(frozen=True)
class Key:
    file: str  # repo-relative JSON report
    path: str  # see module docstring
    fmt: str = ""  # Python format spec for numbers ("" → ints verbatim, floats with 2 decimals)


def _load_runs(prefix: str, file: str, ns: tuple[int, ...], fields: dict[str, str]) -> dict[str, Key]:
    return {
        f"{prefix}.n{n}.{field}": Key(file, f"$.runs[?n=={n}].{field}", fmt)
        for n in ns
        for field, fmt in fields.items()
    }


_A_FIELDS = {
    "ack_p50_ms": ".0f",
    "ack_p95_ms": ".0f",
    "ack_p99_ms": ".0f",
    "final_e2e_p95_ms": ".0f",
    "alert_e2e_p95_ms": ".0f",
    "chunks_per_s": ".0f",
    "credit_min": "d",
    "loss": "d",
    "dup": "d",
}
_PERF_QUERIES = ("Q1", "Q2", "Q2b", "Q2c", "Q3", "Q4", "Q5", "Q6")

KEYS: dict[str, Key] = {
    # ---- load tests (docs/loadtest/<scenario>.json) ----
    **_load_runs("load.A", "docs/loadtest/A.json", (50, 100, 200), _A_FIELDS),
    "load.B.credit_zero_at_s": Key("docs/loadtest/B.json", "$.credit_zero_at_s", ".1f"),
    "load.B.pause_count": Key("docs/loadtest/B.json", "$.pause_count", "d"),
    "load.B.stream_len_max": Key("docs/loadtest/B.json", "$.stream_len_max", "d"),
    "load.B.api_rss_slope_mb_per_min": Key("docs/loadtest/B.json", "$.api_rss_slope_mb_per_min", ".2f"),
    "load.B.loss": Key("docs/loadtest/B.json", "$.loss", "d"),
    "load.C.ack_p95_ms": Key("docs/loadtest/C.json", "$.ack_p95_ms", ".0f"),
    "load.C.ack_p95_delta_pct": Key("docs/loadtest/C.json", "$.ack_p95_delta_pct", "+.1f"),
    "load.C.dropped_partials": Key("docs/loadtest/C.json", "$.dropped_partials", "d"),
    "load.D.resume_success_pct": Key("docs/loadtest/D.json", "$.resume_success_pct", ".1f"),
    "load.D.superseded_closes": Key("docs/loadtest/D.json", "$.superseded_closes", "d"),
    "load.D.rebuild_count": Key("docs/loadtest/D.json", "$.rebuild_count", "d"),
    "load.D.loss": Key("docs/loadtest/D.json", "$.loss", "d"),
    "load.D.dup": Key("docs/loadtest/D.json", "$.dup", "d"),
    "load.H.events_per_s": Key("docs/loadtest/H.json", "$.events_per_s", ".0f"),
    "load.H.dlq_count": Key("docs/loadtest/H.json", "$.dlq_count", "d"),
    "load.E.loss": Key("docs/loadtest/E.json", "$.loss", "d"),
    "load.E.reconnect_p95_ms": Key("docs/loadtest/E.json", "$.reconnect_p95_ms", ".0f"),
    # ---- perf study (docs/perf/study.json: {"queries":[{"id":"Q1","before_ms":..,"after_ms":..}]}) ----
    **{
        f"perf.{q}.{side}_ms": Key("docs/perf/study.json", f"$.queries[?id=={q}].{side}_ms", ".1f")
        for q in _PERF_QUERIES
        for side in ("before", "after")
    },
    # ---- evaluation (docs/eval/*.json) ----
    "eval.risk_heldout.precision": Key("docs/eval/risk_heldout.json", "$.precision", ".2f"),
    "eval.risk_heldout.recall": Key("docs/eval/risk_heldout.json", "$.recall", ".2f"),
    "eval.risk_heldout.f1": Key("docs/eval/risk_heldout.json", "$.f1", ".2f"),
    "eval.risk_heldout.n": Key("docs/eval/risk_heldout.json", "$.n", "d"),
    "eval.risk_ingrammar.precision": Key("docs/eval/risk_ingrammar.json", "$.precision", ".2f"),
    "eval.risk_ingrammar.recall": Key("docs/eval/risk_ingrammar.json", "$.recall", ".2f"),
    "eval.risk_ingrammar.f1": Key("docs/eval/risk_ingrammar.json", "$.f1", ".2f"),
    "eval.alert_latency.p50_ms": Key("docs/eval/alert_latency.json", "$.p50_ms", ".0f"),
    "eval.alert_latency.p95_ms": Key("docs/eval/alert_latency.json", "$.p95_ms", ".0f"),
    "eval.grounding.coverage": Key("docs/eval/grounding.json", "$.coverage", ".2f"),
    "eval.grounding.fact_recall": Key("docs/eval/grounding.json", "$.fact_recall", ".2f"),
    "eval.grounding.abstain_rate": Key("docs/eval/grounding.json", "$.abstain_rate", ".2f"),
    "eval.paraphrase.false_rejection_rate": Key("docs/eval/paraphrase.json", "$.false_rejection_rate", ".3f"),
    **{
        f"eval.inject.{cls}.detection_rate": Key(
            "docs/eval/inject.json", f"$.classes[?class=={cls}].detection_rate", ".2f"
        )
        for cls in ("fabricated", "seq", "diagnosis", "number", "drug", "negation", "speaker")
    },
    "eval.inject.false_flag_rate": Key("docs/eval/inject.json", "$.false_flag_rate", ".3f"),
    "eval.injection.leaks": Key("docs/eval/injection.json", "$.injection_leaks", "d"),
    "eval.purge.residual_rows": Key("docs/eval/purge.json", "$.residual_rows", "d"),
    "eval.purge.residual_objects": Key("docs/eval/purge.json", "$.residual_objects", "d"),
    "eval.purge.residual_keys": Key("docs/eval/purge.json", "$.residual_keys", "d"),
    "eval.purge.unwrap_failure_pct": Key("docs/eval/purge.json", "$.unwrap_failure_pct", ".0f"),
    "eval.purge.decrypt_failure_pct": Key("docs/eval/purge.json", "$.decrypt_failure_pct", ".0f"),
    "eval.purge.receipts_verified_pct": Key("docs/eval/purge.json", "$.receipts_verified_pct", ".0f"),
    "eval.rls.attempts": Key("docs/eval/rls.json", "$.attempts", "d"),
    "eval.rls.leaks": Key("docs/eval/rls.json", "$.leaks", "d"),
    "eval.protocol.hypothesis_examples": Key("docs/eval/protocol.json", "$.hypothesis_examples", "d"),
    "eval.protocol.chaos_loss": Key("docs/eval/protocol.json", "$.chaos_loss", "d"),
    "eval.protocol.chaos_dup": Key("docs/eval/protocol.json", "$.chaos_dup", "d"),
}

# ------------------------------------------------------------------ path resolution
_STEP = re.compile(
    r"\.(?P<name>[A-Za-z0-9_]+)|\[(?P<index>\d+)\]|\[\?(?P<field>[A-Za-z0-9_]+)==(?P<value>[^\]]+)\]"
)


class Missing(LookupError):
    """The path cannot be resolved (or resolves to ``null``)."""


def _literal(raw: str) -> Any:
    if raw in ("true", "false"):
        return raw == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    return raw.strip("'\"")


def parse_path(path: str) -> list[tuple[str, Any]]:
    body = path.removeprefix("$")
    steps: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(body):
        m = _STEP.match(body, pos)
        if m is None:
            raise ValueError(f"잘못된 경로 {path!r} (위치 {pos})")
        if m.group("name") is not None:
            steps.append(("member", m.group("name")))
        elif m.group("index") is not None:
            steps.append(("index", int(m.group("index"))))
        else:
            steps.append(("filter", (m.group("field"), _literal(m.group("value")))))
        pos = m.end()
    return steps


def resolve(doc: Any, path: str) -> Any:
    node = doc
    for kind, arg in parse_path(path):
        node = _step(node, kind, arg)
        if node is None:
            raise Missing(path)
    return node


def _step(node: Any, kind: str, arg: Any) -> Any:
    if kind == "member" and isinstance(node, dict):
        return node.get(arg)
    if kind == "index" and isinstance(node, list) and arg < len(node):
        return node[arg]
    if kind == "filter" and isinstance(node, list):
        field, value = arg
        return next((item for item in node if isinstance(item, dict) and item.get(field) == value), None)
    return None


def format_value(value: Any, fmt: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        if fmt:
            return format(value, fmt)
        return str(value) if isinstance(value, int) else f"{value:.2f}"
    return str(value)


def load_values(root: Path, keys: dict[str, Key] = KEYS) -> dict[str, str | None]:
    """Resolve every registered key; ``None`` marks an unmeasured (missing) number."""
    docs: dict[str, Any] = {}
    values: dict[str, str | None] = {}
    for name, key in keys.items():
        if key.file not in docs:
            file = root / key.file
            docs[key.file] = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
        doc = docs[key.file]
        try:
            values[name] = None if doc is None else format_value(resolve(doc, key.path), key.fmt)
        except Missing:
            values[name] = None
    return values


# ------------------------------------------------------------------ README rendering
NUM_RE = re.compile(r"<!-- num:(?P<key>[A-Za-z0-9_.]+) -->(?P<body>.*?)<!-- /num -->", re.DOTALL)
ROW_RE = re.compile(r"<!-- row:(?P<key>[A-Za-z0-9_.]+) -->(?P<body>.*?)<!-- /row -->\n?", re.DOTALL)


@dataclass
class Rendered:
    text: str
    filled: list[str]
    deleted_rows: list[str]
    unknown: list[str]
    unmeasured_outside_rows: list[str]

    @property
    def ok(self) -> bool:
        return not self.unknown and not self.unmeasured_outside_rows


def render(readme: str, values: dict[str, str | None], keys: dict[str, Key] = KEYS) -> Rendered:
    result = Rendered(readme, [], [], [], [])
    unknown = {m.group("key") for m in NUM_RE.finditer(readme) if m.group("key") not in keys}
    result.unknown = sorted(unknown)

    def drop_row(m: re.Match[str]) -> str:
        inner_keys = [n.group("key") for n in NUM_RE.finditer(m.group("body"))]
        if any(values.get(k) is None for k in inner_keys if k in keys):
            result.deleted_rows.append(m.group("key"))
            return ""
        return m.group(0)

    text = ROW_RE.sub(drop_row, readme)

    def fill(m: re.Match[str]) -> str:
        key = m.group("key")
        value = values.get(key)
        if key not in keys:
            return m.group(0)
        if value is None:
            result.unmeasured_outside_rows.append(key)
            return m.group(0)
        result.filled.append(key)
        return f"<!-- num:{key} -->{value}<!-- /num -->"

    result.text = NUM_RE.sub(fill, text)
    return result


def _report(result: Rendered) -> None:
    for key in result.unknown:
        print(
            f"[오류] 등록되지 않은 키: {key} (scripts/readme_numbers.py::KEYS 에 추가하세요)", file=sys.stderr
        )
    for key in result.unmeasured_outside_rows:
        print(
            f"[오류] 미측정 키 {key} 가 row 블록 밖에 있습니다 — 삭제 가능한 <!-- row --> 로 감싸세요",
            file=sys.stderr,
        )
    for row in result.deleted_rows:
        print(f"[삭제] 미측정 행 {row}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="README 숫자 마커를 JSON 리포트에서 채웁니다")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="마커를 채우고 미측정 행을 삭제")
    mode.add_argument("--check", action="store_true", help="README가 최신인지 검사 (stale → exit 1)")
    mode.add_argument("--list", action="store_true", help="키 레지스트리 출력")
    parser.add_argument("--readme", default=DEFAULT_README)
    parser.add_argument("--root", default=".", help="JSON 리포트 경로의 기준 디렉터리")
    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.list:
        for name, key in KEYS.items():
            print(f"{name}\t{key.file}:{key.path}")
        return 0

    readme_path = root / args.readme if not Path(args.readme).is_absolute() else Path(args.readme)
    original = readme_path.read_text(encoding="utf-8")
    result = render(original, load_values(root))
    _report(result)
    if not result.ok:
        return 2
    if args.write:
        if result.text != original:
            readme_path.write_text(result.text, encoding="utf-8")
        print(f"{len(result.filled)}개 마커 채움, {len(result.deleted_rows)}개 행 삭제")
        return 0
    if result.text != original:
        stale = [k for k in result.filled if _current(original, k) != _current(result.text, k)]
        print(
            f"[stale] README가 리포트와 다릅니다: 값 변경 {stale}, 삭제 대상 행 {result.deleted_rows}",
            file=sys.stderr,
        )
        return 1
    print("README 숫자 마커가 최신입니다")
    return 0


def _current(text: str, key: str) -> str | None:
    m = re.search(rf"<!-- num:{re.escape(key)} -->(.*?)<!-- /num -->", text, re.DOTALL)
    return None if m is None else m.group(1)


if __name__ == "__main__":
    sys.exit(main())
