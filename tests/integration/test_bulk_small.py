"""``chartwire synth bulk --segments 20000``: the perf dataset at 1 % scale (spec §4.6).

Checks the shape the perf study relies on — every segment in a monthly partition (default
partition empty), search rows for ≈60 % of sessions with the controlled keyword rates and
``terms[]`` tags, risk/outbox proportions, FORCE-RLS respected by the owner-role loader, and
that a repeated seed refuses to load twice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.config import Settings
from chartwire.synth import bulk
from tests.conftest import DbUrls

pytestmark = pytest.mark.integration

SEGMENTS = 20_000


@pytest.fixture
async def loaded(clean_db: AsyncEngine, migrated_db: DbUrls, settings: Settings) -> bulk.BulkReport:
    plan = bulk.BulkPlan.build(seed=7, segments=SEGMENTS, tenants=8)
    assert (plan.sessions, plan.segments, plan.patients) == (200, SEGMENTS, 500)
    lines: list[str] = []
    return await bulk.load(migrated_db.owner, plan, kek_master=settings.kek_master_bytes, log=lines.append)


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def test_shape_partitions_and_proportions(loaded: bulk.BulkReport, su_engine: AsyncEngine) -> None:
    counts = loaded.counts
    assert counts["segments"] == SEGMENTS and counts["sessions"] == 200 and counts["patients"] == 500
    assert counts["outbox"] == SEGMENTS
    assert len(loaded.tenant_ids) == 8 and len(loaded.partitions) == 25  # 24 months + 1 ahead
    assert loaded.total_s < 120 and {"partitions", "pool", "tenants", "tenant_01", "vacuum_analyze"} <= set(
        loaded.timings_s
    )

    # superuser sees everything (bypasses RLS): totals and partition placement
    assert await _scalar(su_engine, "SELECT count(*) FROM transcript_segments") == SEGMENTS
    assert await _scalar(su_engine, "SELECT count(*) FROM transcript_segments_default") == 0
    partitions_used = await _scalar(
        su_engine,
        "SELECT count(DISTINCT tableoid) FROM transcript_segments",
    )
    assert partitions_used >= 20
    oldest = datetime.now(tz=UTC) - timedelta(days=31 * 24)
    async with su_engine.connect() as conn:
        lo, hi = (
            await conn.execute(text("SELECT min(created_at), max(created_at) FROM transcript_segments"))
        ).one()
    assert lo > oldest and hi < datetime.now(tz=UTC)
    # created_at = started_at + t_start_ms, id block contiguous and the sequence moved past it
    mismatched = await _scalar(
        su_engine,
        "SELECT count(*) FROM transcript_segments t JOIN sessions s ON s.id = t.session_id "
        "WHERE t.created_at <> s.started_at + make_interval(secs => t.t_start_ms / 1000.0)",
    )
    assert mismatched == 0
    lo_id, hi_id = loaded.segment_id_range
    assert hi_id - lo_id + 1 == SEGMENTS
    assert await _scalar(su_engine, "SELECT nextval('transcript_segments_id_seq')") > hi_id

    # search: ≈60 % of sessions, keyword rates, terms tagged
    search_sessions = await _scalar(su_engine, "SELECT count(DISTINCT session_id) FROM segment_search")
    assert 0.45 * 200 <= search_sessions <= 0.75 * 200
    rows = counts["search"]
    assert rows == search_sessions * bulk.SEGMENTS_PER_SESSION
    for keyword, expected in (("불면", 0.020), ("불면증", 0.005), ("에스시탈로프람", 0.003), ("자해", 0.002)):
        n = await _scalar(
            su_engine, "SELECT count(*) FROM segment_search WHERE text LIKE :p", p=f"%{keyword}%"
        )
        assert abs(n / rows - expected) <= max(0.004, expected * 0.6), (keyword, n, rows)
    tagged = await _scalar(
        su_engine, "SELECT count(*) FROM segment_search WHERE text LIKE '%불면%' AND terms @> ARRAY['불면']"
    )
    assert tagged == await _scalar(su_engine, "SELECT count(*) FROM segment_search WHERE text LIKE '%불면%'")
    assert (
        await _scalar(su_engine, "SELECT count(*) FROM segment_search WHERE cardinality(terms) > 0")
        > rows * 0.5
    )

    # risk 2 % (1 % of those open), outbox 0.1 % pending
    assert abs(counts["risk"] / SEGMENTS - 0.02) < 0.006
    assert 0 <= counts["risk_open"] <= 12 and counts["risk_open"] == await _scalar(
        su_engine, "SELECT count(*) FROM risk_events WHERE acknowledged_at IS NULL"
    )
    assert 5 <= counts["outbox_pending"] <= 45 and counts["outbox_pending"] == await _scalar(
        su_engine, "SELECT count(*) FROM outbox_events WHERE status = 'pending'"
    )
    assert await _scalar(su_engine, "SELECT count(*) FROM outbox_events WHERE status = 'done'") == (
        SEGMENTS - counts["outbox_pending"]
    )


async def test_rls_holds_for_bulk_rows_and_search_function_works(
    loaded: bulk.BulkReport, app_engine: AsyncEngine, su_engine: AsyncEngine
) -> None:
    tenant_id = loaded.tenant_ids[0]
    per_tenant = await _scalar(
        su_engine, "SELECT count(*) FROM transcript_segments WHERE tenant_id = :t", t=tenant_id
    )
    assert per_tenant == 25 * bulk.SEGMENTS_PER_SESSION  # 200 sessions / 8 tenants
    async with app_engine.connect() as conn, conn.begin():
        assert (await conn.execute(text("SELECT count(*) FROM transcript_segments"))).scalar_one() == 0
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        assert (
            await conn.execute(text("SELECT count(*) FROM transcript_segments"))
        ).scalar_one() == per_tenant
        hits = (await conn.execute(text("SELECT * FROM search_segments('불면증', 'text')"))).all()
        term_hits = (await conn.execute(text("SELECT * FROM search_segments('불면', 'term')"))).all()
    assert hits and all("불면증" in h.snippet for h in hits)  # rows returned under the app role
    assert len(term_hits) >= len(hits)  # canonical term tag covers more phrasings than the substring


async def test_same_seed_refuses_to_load_twice(
    loaded: bulk.BulkReport, migrated_db: DbUrls, settings: Settings
) -> None:
    plan = bulk.BulkPlan.build(seed=7, segments=SEGMENTS, tenants=8)
    with pytest.raises(bulk.BulkAlreadyLoaded):
        await bulk.load(migrated_db.owner, plan, kek_master=settings.kek_master_bytes, log=lambda _: None)
    with pytest.raises(ValueError, match="최소"):
        bulk.BulkPlan.build(seed=7, segments=100, tenants=8)


def test_pool_is_keyword_free_and_deterministic() -> None:
    base, keyed = bulk.build_pool(7)
    assert len(base) >= 100 and all(not bulk._controlled(s.text) for s in base)
    assert base == bulk.build_pool(7)[0]
    assert all(keyword in s.text for keyword, sents in keyed.items() for s in sents)
    assert all("불면" in s.terms for s in keyed["불면"] + keyed["불면증"])


async def test_perf_study_runs_on_the_small_dataset(
    loaded: bulk.BulkReport, migrated_db: DbUrls, tmp_path: Path
) -> None:
    """`chartwire perf study` end to end at head (state ``after``): plans, leakproof.txt, summary.json."""
    from chartwire.db.cli import libpq_url
    from chartwire.perf import queries as catalogue
    from chartwire.perf import study as study_mod

    assert migrated_db.superuser is not None
    conn = study_mod.connect(libpq_url(migrated_db.superuser))
    try:
        assert study_mod.detect_state(study_mod.current_revision(conn)) == "after"
        study = study_mod.run_study(conn, catalogue.select(None), state="after", runs=2, log=lambda _: None)
    finally:
        conn.close()
    by_id = {r.id: r for r in study.results}
    assert not [r.id for r in study.results if r.error], [(r.id, r.error) for r in study.results if r.error]
    scanned = {
        k: by_id[k].plan_summary["relations_scanned"] for k in ("Q1a_without", "Q1a_with", "Q1a_bounded")
    }
    assert scanned["Q1a_bounded"] == 1 < scanned["Q1a_with"] < scanned["Q1a_without"], scanned
    assert "ix_segments_patient_time" in " ".join(by_id["Q1"].plan_summary["indexes"])
    assert by_id["Q2d_text"].rows and by_id["Q2d_text"].plan is None  # wall time only
    assert by_id["Q6"].rows == 1 and by_id["Q4"].rows <= 100
    assert "texticlike" in study.leakproof and "false" in study.leakproof
    assert study.pgstattuple and study.pgstattuple["tuple_count"] == SEGMENTS

    summary_path = study_mod.write_outputs(study, tmp_path, seed=7)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["seed"] == 7 and summary["pg_version"] and summary["states"]["after"]["revision"]
    q1 = next(q for q in summary["queries"] if q["id"] == "Q1")
    assert q1["after_ms"] > 0 and q1["before_ms"] is None if "before_ms" in q1 else True
    assert (tmp_path / "plans" / "q1_after.json").is_file() and (tmp_path / "leakproof.txt").is_file()
    assert not (tmp_path / "plans" / "q2d_text_after.json").exists()
    assert summary["rls_overhead"]["after"]["q1_pct"] is not None
    # a second run for the other state merges instead of overwriting
    study.state = "before"
    study_mod.write_outputs(study, tmp_path, seed=7)
    merged = json.loads(summary_path.read_text(encoding="utf-8"))
    q1 = next(q for q in merged["queries"] if q["id"] == "Q1")
    assert q1["before_ms"] and q1["after_ms"] and set(merged["states"]) == {"before", "after"}
