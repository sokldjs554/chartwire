"""``chartwire perf study`` — measure the §4.6 queries at the current schema state.

Method (spec §4.6): one superuser psycopg connection; every run is its own transaction with
``SET LOCAL ROLE`` (``chartwire_app`` / ``chartwire_owner`` / none) and the tenant GUCs set with
``set_config(..., true)``; 1 warm-up + ``runs`` timed executions of
``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``; the median run (by client wall time) is the one whose
plan is committed to ``docs/perf/plans/<id>_<state>.json``. Everything is rolled back, so Q4's
``FOR UPDATE SKIP LOCKED`` never changes the data.

The integrator runs the study twice — at revision 0006 (``--state before``) and at head
(``--state after``); ``summary.json`` is merged across the two runs and is the only file the
README number pipeline reads. ``leakproof.txt`` (the ``pg_proc.proleakproof`` query and output)
explains Q2b, and the ``pgstattuple`` observation on ``outbox_events`` accompanies Q4.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from chartwire.eval.report import build_report
from chartwire.perf.queries import (
    APP_ROLE,
    LEAKPROOF_FUNCTIONS,
    OWNER_ROLE,
    PATTERN_BY_ID,
    PerfQuery,
)

STATES: Final = ("before", "after")
REVISION_STATE: Final = {"0006_rls": "before", "0007_perf": "after"}
PLANS_DIR: Final = "plans"
SUMMARY_FILE: Final = "summary.json"
LEAKPROOF_FILE: Final = "leakproof.txt"
BULK_SLUG_PREFIX: Final = "bulk-"


class StudyError(RuntimeError):
    pass


def _one(cursor: Any) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise StudyError("쿼리가 행을 돌려주지 않았습니다")
    return tuple(row)


@dataclass
class Params:
    tenant_id: UUID
    tenant_slug: str
    patient_id: UUID
    session_id: UUID
    started_at: datetime
    after_seq: int
    cursor_at: datetime
    cursor_id: int
    name_hmac: bytes
    limit: int = 50

    def bind(self, query: PerfQuery) -> dict[str, Any]:
        values: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "patient_id": self.patient_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "after_seq": self.after_seq,
            "cursor_at": self.cursor_at,
            "cursor_id": self.cursor_id,
            "name_hmac": self.name_hmac,
            "limit": self.limit,
        }
        if query.id in PATTERN_BY_ID:
            values["pattern"] = PATTERN_BY_ID[query.id]
        return values


@dataclass
class QueryResult:
    id: str
    group: str
    label: str
    role: str
    wall_ms: list[float] = field(default_factory=list)
    exec_ms: list[float] = field(default_factory=list)
    rows: int | None = None
    plan: dict[str, Any] | None = None
    plan_summary: dict[str, Any] = field(default_factory=dict)
    skipped: str | None = None
    error: str | None = None

    @property
    def median_wall_ms(self) -> float | None:
        return round(statistics.median(self.wall_ms), 3) if self.wall_ms else None

    @property
    def median_exec_ms(self) -> float | None:
        return round(statistics.median(self.exec_ms), 3) if self.exec_ms else None


# ------------------------------------------------------------------ connection / context


def connect(dsn: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(dsn, autocommit=False, row_factory=tuple_row)
    if not _one(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))[0]:
        raise StudyError("성능 연구는 superuser 연결이 필요합니다 (SET ROLE 전환)")
    return conn


def current_revision(conn: psycopg.Connection[Any]) -> str | None:
    if _one(conn.execute("SELECT to_regclass('alembic_version')"))[0] is None:
        return None
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    conn.rollback()
    return None if row is None else str(row[0])


def detect_state(revision: str | None) -> str | None:
    if revision is None:
        return None
    for prefix, state in REVISION_STATE.items():
        if revision.startswith(prefix.split("_")[0]):
            return state
    return None


def settings_snapshot(conn: psycopg.Connection[Any]) -> dict[str, str]:
    names = (
        "server_version",
        "shared_buffers",
        "work_mem",
        "effective_cache_size",
        "random_page_cost",
        "jit",
    )
    out = {name: str(_one(conn.execute(f"SHOW {name}"))[0]) for name in names}
    conn.rollback()
    return out


def _set_context(conn: psycopg.Connection[Any], role: str, tenant_id: UUID) -> None:
    if role == "app":
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(APP_ROLE)))
    elif role == "owner":
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', '', true), "
        "set_config('app.role', 'service', true)",
        (str(tenant_id),),
    )


# ------------------------------------------------------------------ parameters from the bulk data


def pick_params(conn: psycopg.Connection[Any], tenant_slug: str | None = None) -> Params:
    """Bind the catalogue to real rows: the first bulk tenant (or ``tenant_slug``), its patient with
    the most segments in the last 6 months, one of that tenant's sessions and one patient hash."""
    if tenant_slug:
        row = conn.execute("SELECT id, slug FROM tenants WHERE slug = %s", (tenant_slug,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id, slug FROM tenants WHERE slug LIKE %s ORDER BY slug LIMIT 1", (BULK_SLUG_PREFIX + "%",)
        ).fetchone()
    if row is None:
        raise StudyError("bulk 테넌트가 없습니다 — 먼저 `chartwire synth bulk` 를 실행하세요")
    tenant_id, slug = row
    patient = conn.execute(
        "SELECT patient_id FROM transcript_segments WHERE tenant_id = %s "
        "AND created_at >= now() - interval '6 months' GROUP BY patient_id ORDER BY count(*) DESC, patient_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if patient is None:
        raise StudyError("최근 6개월 세그먼트가 있는 환자가 없습니다")
    session = conn.execute(
        "SELECT id, started_at FROM sessions WHERE tenant_id = %s AND patient_id = %s AND started_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (tenant_id, patient[0]),
    ).fetchone()
    if session is None:
        raise StudyError("세션이 없습니다")
    cursor = conn.execute(
        "SELECT created_at, id FROM transcript_segments WHERE tenant_id = %s AND patient_id = %s "
        "AND created_at >= now() - interval '6 months' ORDER BY created_at DESC, id DESC OFFSET 100 LIMIT 1",
        (tenant_id, patient[0]),
    ).fetchone()
    if cursor is None:  # fewer than 100 rows: page from the newest row
        cursor = _one(
            conn.execute(
                "SELECT created_at, id FROM transcript_segments WHERE tenant_id = %s AND patient_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id, patient[0]),
            )
        )
    name_hmac = conn.execute(
        "SELECT name_hmac FROM patients WHERE tenant_id = %s AND name_hmac IS NOT NULL ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    conn.rollback()
    return Params(
        tenant_id=tenant_id,
        tenant_slug=slug,
        patient_id=patient[0],
        session_id=session[0],
        started_at=session[1],
        after_seq=50,
        cursor_at=cursor[0],
        cursor_id=cursor[1],
        name_hmac=bytes(name_hmac[0]) if name_hmac else b"",
    )


# ------------------------------------------------------------------ running one query


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Compact, README-quotable facts from an EXPLAIN JSON document."""
    root = plan["Plan"]
    nodes: list[str] = []
    indexes: list[str] = []
    relations: set[str] = set()
    subplans_removed = 0

    def walk(node: dict[str, Any]) -> None:
        nonlocal subplans_removed
        nodes.append(node["Node Type"])
        if "Index Name" in node and node["Index Name"] not in indexes:
            indexes.append(node["Index Name"])
        if "Relation Name" in node:
            relations.add(node["Relation Name"])
        subplans_removed += int(node.get("Subplans Removed", 0))
        for child in node.get("Plans", []):
            walk(child)

    walk(root)
    return {
        "top_node": root["Node Type"],
        "node_types": list(dict.fromkeys(nodes)),
        "indexes": indexes,
        "relations_scanned": len(relations),
        "subplans_removed": subplans_removed,
        "planning_ms": plan.get("Planning Time"),
        "execution_ms": plan.get("Execution Time"),
        "shared_hit_blocks": root.get("Shared Hit Blocks"),
        "shared_read_blocks": root.get("Shared Read Blocks"),
        "actual_rows": root.get("Actual Rows"),
    }


def run_query(
    conn: psycopg.Connection[Any], query: PerfQuery, params: Params, *, runs: int = 5, warmup: int = 1
) -> QueryResult:
    result = QueryResult(query.id, query.group, query.label, query.role)
    bound = params.bind(query)
    samples: list[tuple[float, float | None, dict[str, Any] | None, int]] = []
    for i in range(warmup + runs):
        sample: tuple[float, float | None, dict[str, Any] | None, int]
        try:
            with conn.transaction():
                _set_context(conn, query.role, params.tenant_id)
                t0 = time.perf_counter()
                if query.explain:
                    row = _one(conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query.sql, bound))
                    wall = (time.perf_counter() - t0) * 1000
                    plan = row[0][0]
                    sample = (
                        wall,
                        float(plan.get("Execution Time", 0.0)),
                        plan,
                        int(plan["Plan"].get("Actual Rows", 0)),
                    )
                else:
                    rows = conn.execute(query.sql, bound).fetchall()
                    wall = (time.perf_counter() - t0) * 1000
                    sample = (wall, None, None, len(rows))
                raise _Rollback  # measurement only; never keep locks or effects
        except _Rollback:
            pass
        except psycopg.Error as exc:
            conn.rollback()
            result.error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            return result
        if i >= warmup:
            samples.append(sample)
    result.wall_ms = [round(s[0], 3) for s in samples]
    result.exec_ms = [round(s[1], 3) for s in samples if s[1] is not None]
    median = sorted(samples, key=lambda s: s[0])[len(samples) // 2]
    result.rows = median[3]
    if median[2] is not None:
        result.plan = median[2]
        result.plan_summary = plan_summary(median[2])
    return result


class _Rollback(Exception):
    """Raised inside ``conn.transaction()`` to roll back a measurement run."""


# ------------------------------------------------------------------ side observations


LEAKPROOF_SQL: Final = (
    "SELECT p.proname, p.proleakproof, pg_get_function_identity_arguments(p.oid) AS args "
    "FROM pg_proc p WHERE p.proname = ANY(%s) ORDER BY p.proname, args"
)


def leakproof_report(conn: psycopg.Connection[Any]) -> str:
    rows = conn.execute(LEAKPROOF_SQL, (list(LEAKPROOF_FUNCTIONS),)).fetchall()
    conn.rollback()
    lines = [
        "-- RLS 정책이 걸린 테이블에서는 leakproof 가 아닌 연산자/함수를 인덱스 조건으로 정책 qual 보다 먼저 평가할 수 없다 (spec §4.6 Q2b, ADR-0005).",
        "-- 실행 쿼리:",
        "-- "
        + LEAKPROOF_SQL.replace("%s", "ARRAY[" + ", ".join(f"'{f}'" for f in LEAKPROOF_FUNCTIONS) + "]"),
        "",
        f"{'proname':<18} {'proleakproof':<13} args",
    ]
    lines += [f"{name:<18} {str(flag).lower():<13} {args}" for name, flag, args in rows]
    return "\n".join(lines) + "\n"


def pgstattuple(conn: psycopg.Connection[Any], table: str = "outbox_events") -> dict[str, Any] | None:
    """Bloat sample for the outbox table. The extension is created and then **rolled back**, exactly
    like :func:`leakproof_report`: ``CREATE EXTENSION`` is transactional, so it exists for the SELECT
    and is gone afterwards, and ``IF NOT EXISTS`` leaves a pre-existing installation untouched.
    Committing it instead leaked an extension into whatever database the study ran against, which then
    made ``test_round_trip_and_schema_dump_equality`` fail for every later run in the same database
    (``docs/db/schema.sql`` has no pgstattuple) — and ``make schema-dump`` would have written it into
    the contract, breaking the CI ``migrations`` job in turn."""
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
        row = _one(
            conn.execute(
                sql.SQL(
                    "SELECT table_len, tuple_count, dead_tuple_count, dead_tuple_percent, free_percent "
                    "FROM pgstattuple({})"
                ).format(sql.Literal(table))
            )
        )
        conn.rollback()  # drop the extension again; the schema dump is a contract (see docstring)
    except psycopg.Error as exc:
        conn.rollback()
        return {"table": table, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"}
    return {
        "table": table,
        "table_len_bytes": int(row[0]),
        "tuple_count": int(row[1]),
        "dead_tuple_count": int(row[2]),
        "dead_tuple_percent": float(row[3]),
        "free_percent": float(row[4]),
    }


# ------------------------------------------------------------------ the study


@dataclass
class Study:
    state: str
    revision: str | None
    settings: dict[str, str]
    params: Params
    results: list[QueryResult]
    pgstattuple: dict[str, Any] | None
    leakproof: str

    def rls_overhead(self) -> dict[str, float | None]:
        by_id = {r.id: r.median_wall_ms for r in self.results}

        def pct(app: str, su: str) -> float | None:
            a, s = by_id.get(app), by_id.get(su)
            return round((a - s) / s * 100, 1) if a is not None and s else None

        return {"q1_pct": pct("Q5_q1_app", "Q5_q1_su"), "q3_pct": pct("Q5_q3_app", "Q5_q3_su")}


def run_study(
    conn: psycopg.Connection[Any],
    queries: list[PerfQuery],
    *,
    state: str,
    runs: int = 5,
    tenant_slug: str | None = None,
    log: Any = print,
) -> Study:
    revision = current_revision(conn)
    params = pick_params(conn, tenant_slug)
    log(f"state={state} revision={revision} tenant={params.tenant_slug} runs={runs}")
    results: list[QueryResult] = []
    for query in queries:
        if query.requires_after and state == "before":
            results.append(
                QueryResult(query.id, query.group, query.label, query.role, skipped="0007 이전에는 없는 객체")
            )
            log(f"{query.id:<18} skipped (requires 0007)")
            continue
        result = run_query(conn, query, params, runs=runs)
        results.append(result)
        if result.error:
            log(f"{query.id:<18} ERROR {result.error}")
        else:
            summary = result.plan_summary
            log(
                f"{query.id:<18} {result.median_wall_ms:>10.3f} ms  rows={result.rows}  "
                f"{summary.get('top_node', '-')} {summary.get('indexes', [])}"
            )
    return Study(
        state=state,
        revision=revision,
        settings=settings_snapshot(conn),
        params=params,
        results=results,
        pgstattuple=pgstattuple(conn) if any(r.group == "Q4" for r in results) else None,
        leakproof=leakproof_report(conn),
    )


# ------------------------------------------------------------------ outputs


def write_outputs(study: Study, out: Path, *, seed: int) -> Path:
    """Plans, leakproof.txt and the merged summary.json (one file across ``before``/``after``)."""
    plans = out / PLANS_DIR
    plans.mkdir(parents=True, exist_ok=True)
    for r in study.results:
        if r.plan is not None:
            (plans / f"{r.id.lower()}_{study.state}.json").write_text(
                json.dumps(r.plan, indent=1) + "\n", encoding="utf-8"
            )
    (out / LEAKPROOF_FILE).write_text(study.leakproof, encoding="utf-8")
    summary_path = out / SUMMARY_FILE
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    header = build_report(seed, {}, pg_version=study.settings.get("server_version"))
    summary.update(header)
    states = summary.setdefault("states", {})
    states[study.state] = {
        "revision": study.revision,
        "generated_at": header["generated_at"],
        "settings": study.settings,
        "tenant": study.params.tenant_slug,
        "runs": len(study.results[0].wall_ms) if study.results and study.results[0].wall_ms else None,
    }
    by_id: dict[str, dict[str, Any]] = {q["id"]: q for q in summary.get("queries", [])}
    for r in study.results:
        entry = by_id.setdefault(r.id, {"id": r.id, "group": r.group, "label": r.label, "role": r.role})
        entry[f"{study.state}_ms"] = r.median_wall_ms if not (r.error or r.skipped) else None
        entry[f"{study.state}_exec_ms"] = r.median_exec_ms if not (r.error or r.skipped) else None
        entry[f"{study.state}_rows"] = r.rows
        entry[f"{study.state}_plan"] = r.plan_summary.get("top_node") if r.plan_summary else None
        entry[f"{study.state}_indexes"] = r.plan_summary.get("indexes") if r.plan_summary else None
        entry[f"{study.state}_node_types"] = r.plan_summary.get("node_types") if r.plan_summary else None
        entry[f"{study.state}_samples_ms"] = r.wall_ms
        entry[f"{study.state}_note"] = r.error or r.skipped
    summary["queries"] = [by_id[i] for i in sorted(by_id, key=_order_key)]
    summary.setdefault("rls_overhead", {})[study.state] = study.rls_overhead()
    if study.pgstattuple is not None:
        summary.setdefault("pgstattuple", {})[study.state] = study.pgstattuple
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary_path


def _order_key(query_id: str) -> tuple[int, str]:
    from chartwire.perf.queries import QUERIES

    ids = [q.id for q in QUERIES]
    return (ids.index(query_id) if query_id in ids else len(ids), query_id)
