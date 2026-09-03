"""A partition queried *by name* as ``chartwire_app`` must be isolated exactly like the parent (§4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from chartwire.db.cli import month_start
from chartwire.db.repo import segments as segments_repo
from chartwire.db.tenant import tenant_tx

pytestmark = pytest.mark.integration


def _partition_for(at: datetime) -> str:
    return f"transcript_segments_y{at:%Y}m{at:%m}"


async def _insert_segment(engine, ctx, session_row, *, created_at: datetime, seq: int = 1) -> None:
    async with tenant_tx(engine, ctx) as session:
        await segments_repo.insert_final(
            session,
            tenant_id=session_row.tenant_id,
            session_id=session_row.id,
            patient_id=session_row.patient_id,
            seq=seq,
            speaker="patient",
            t_start_ms=0,
            t_end_ms=900,
            text_enc=b"\x00" * 24,
            text_len=8,
            confidence=0.9,
            provider="simulator",
            created_at=created_at,
        )


async def _count_by_name(engine, ctx, table: str) -> int:
    if ctx is None:
        async with engine.connect() as conn:
            return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
    async with tenant_tx(engine, ctx) as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def test_monthly_partition_by_name_is_isolated(app_engine, session_a, session_b, ctx_a, ctx_b):
    now = datetime.now(tz=UTC)
    await _insert_segment(app_engine, ctx_a, session_a, created_at=now)
    await _insert_segment(app_engine, ctx_b, session_b, created_at=now)
    partition = _partition_for(now)
    assert await _count_by_name(app_engine, None, partition) == 0
    assert await _count_by_name(app_engine, ctx_a, partition) == 1
    assert await _count_by_name(app_engine, ctx_b, partition) == 1
    assert await _count_by_name(app_engine, ctx_a, "transcript_segments") == 1


async def test_default_partition_by_name_is_isolated(app_engine, session_a, ctx_a, ctx_b):
    far_future = datetime(2099, 1, 1, tzinfo=UTC)  # no monthly partition → default partition
    await _insert_segment(app_engine, ctx_a, session_a, created_at=far_future)
    assert await _count_by_name(app_engine, None, "transcript_segments_default") == 0
    assert await _count_by_name(app_engine, ctx_b, "transcript_segments_default") == 0
    assert await _count_by_name(app_engine, ctx_a, "transcript_segments_default") == 1


async def test_ensure_segment_partition_runs_as_app_and_attaches_index(app_engine, owner_engine, ctx_a):
    month = month_start(6)  # beyond the 0003 initial window
    name = f"transcript_segments_y{month:%Y}m{month:%m}"
    try:
        async with tenant_tx(app_engine, ctx_a) as session:
            created = (
                await session.execute(text("SELECT ensure_segment_partition(:m)"), {"m": month})
            ).scalar_one()
        assert created == name
        async with owner_engine.connect() as conn:
            rls = (
                await conn.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :n"),
                    {"n": name},
                )
            ).one()
            attached = (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                        "WHERE i.inhparent = 'ix_segments_patient_time'::regclass AND c.relname = :i"
                    ),
                    {"i": f"ix_segments_patient_time_y{month:%Y}m{month:%m}"},
                )
            ).scalar_one_or_none()
            valid = (
                await conn.execute(
                    text(
                        "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_segments_patient_time'::regclass"
                    )
                )
            ).scalar_one()
        assert tuple(rls) == (True, True)
        assert attached is not None
        assert valid is True
        assert await _count_by_name(app_engine, None, name) == 0
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


async def test_replay_prunes_to_started_at_partition(app_engine, session_a, ctx_a):
    start = session_a.started_at
    assert start is not None
    for seq in (1, 2, 3):
        await _insert_segment(
            app_engine, ctx_a, session_a, created_at=start + timedelta(seconds=seq), seq=seq
        )
    async with tenant_tx(app_engine, ctx_a) as session:
        rows = await segments_repo.replay(session, session_a.id, after_seq=1, limit=10)
        plan = (
            await session.execute(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT id FROM transcript_segments "
                    "WHERE session_id = :s AND seq > 0 AND created_at >= :st ORDER BY seq"
                ),
                {"s": session_a.id, "st": start},
            )
        ).scalar_one()
    assert [r.seq for r in rows] == [2, 3]
    scanned = _scanned_relations(plan[0]["Plan"])
    assert len(scanned) == 1, scanned  # every other partition pruned at plan time


def _scanned_relations(node: dict) -> set[str]:
    names = {node["Relation Name"]} if "Relation Name" in node else set()
    for child in node.get("Plans", []):
        names |= _scanned_relations(child)
    return names


def test_month_start_arithmetic():
    from datetime import date

    assert month_start(-1, date(2026, 1, 15)) == date(2025, 12, 1)
    assert month_start(2, date(2026, 11, 30)) == date(2027, 1, 1)
    assert month_start(0, date(2026, 9, 2)) == date(2026, 9, 1)
