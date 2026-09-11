"""Migration round-trip (head → base → head) and schema-dump equality with ``docs/db/schema.sql`` (§4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from chartwire.db import cli as dbcli
from chartwire.db.engine import make_sync_engine

pytestmark = pytest.mark.integration

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "docs" / "db" / "schema.sql"


def _tables(url: str) -> set[str]:
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars()
            return set(rows)
    finally:
        engine.dispose()


def test_round_trip_and_schema_dump_equality(migrated_db):
    before = dbcli.schema_dump(migrated_db.owner)
    dbcli.downgrade(migrated_db.owner, "base")
    assert _tables(migrated_db.owner) == {"alembic_version"}
    dbcli.upgrade(migrated_db.owner, "head")
    after = dbcli.schema_dump(migrated_db.owner)
    assert after == before
    assert dbcli.current_revision(migrated_db.owner) == "0008"
    committed = SCHEMA_FILE.read_text()
    assert after == committed, "docs/db/schema.sql is stale: run `make schema-dump`"


def test_each_revision_downgrades_one_step(migrated_db):
    for target in ("0007", "0006", "0005", "0004", "0003", "0002", "0001"):
        dbcli.downgrade(migrated_db.owner, target)
        assert dbcli.current_revision(migrated_db.owner) == target
    dbcli.upgrade(migrated_db.owner, "head")
    assert dbcli.current_revision(migrated_db.owner) == "0008"


def test_normalize_strips_volatile_and_calendar_dependent_lines():
    raw = (
        "-- Dumped from database version 16.13\n-- Dumped by pg_dump version 16.13\n\\restrict abc\n\n"
        "CREATE TABLE public.transcript_segments_y2026m09 (\n  id bigint\n);\n\n"
        "ALTER INDEX public.ix_segments_patient_time ATTACH PARTITION public.ix_segments_patient_time_y2026m09;\n\n"
        "CREATE TABLE public.transcript_segments_default (\n  id bigint\n);\n\\unrestrict abc\n"
    )
    assert (
        dbcli.normalize_schema_dump(raw)
        == "CREATE TABLE public.transcript_segments_default (\n  id bigint\n);\n"
    )
