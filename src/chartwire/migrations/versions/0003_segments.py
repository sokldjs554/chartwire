"""transcript_segments (range-partitioned), ensure_segment_partition(), segment_search

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02
"""

from __future__ import annotations

from datetime import date

from alembic import op
from sqlalchemy import text

from chartwire.migrations.helpers import ENSURE_PARTITION_V1, grant, grant_function, grant_sequence

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

INITIAL_PARTITION_MONTHS = range(-1, 3)  # current month -1 .. +2


def _month(offset: int, today: date | None = None) -> date:
    today = today or date.today()
    index = today.year * 12 + (today.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def upgrade() -> None:
    op.execute("CREATE SEQUENCE transcript_segments_id_seq")  # PG16: no identity on partitioned tables
    op.execute("""
CREATE TABLE transcript_segments (
  id bigint NOT NULL DEFAULT nextval('transcript_segments_id_seq'),
  tenant_id uuid NOT NULL, session_id uuid NOT NULL REFERENCES sessions(id), patient_id uuid NOT NULL,
  seq int NOT NULL, speaker text NOT NULL CHECK (speaker IN ('clinician','patient','unknown')),
  t_start_ms int NOT NULL, t_end_ms int NOT NULL, text_enc bytea NOT NULL, text_len int NOT NULL,
  confidence real, provider text NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (created_at, id), UNIQUE (session_id, seq, created_at)
) PARTITION BY RANGE (created_at)""")
    op.execute("ALTER SEQUENCE transcript_segments_id_seq OWNED BY transcript_segments.id")
    op.execute("CREATE TABLE transcript_segments_default PARTITION OF transcript_segments DEFAULT")
    op.execute(ENSURE_PARTITION_V1)
    grant_function("ensure_segment_partition(date)")
    grant("transcript_segments", "SELECT, INSERT, DELETE")
    grant("transcript_segments_default", "SELECT, INSERT, DELETE")
    grant_sequence("transcript_segments_id_seq")
    bind = op.get_bind()
    for offset in INITIAL_PARTITION_MONTHS:
        bind.execute(text("SELECT ensure_segment_partition(:m)"), {"m": _month(offset)})
    op.execute("""
CREATE TABLE segment_search (
  segment_id bigint PRIMARY KEY, segment_created_at timestamptz NOT NULL, tenant_id uuid NOT NULL,
  session_id uuid NOT NULL, patient_id uuid NOT NULL, speaker text NOT NULL,
  text text NOT NULL, terms text[] NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_search_session ON segment_search (tenant_id, session_id)")
    op.execute(
        "CREATE INDEX ix_search_patient ON segment_search (tenant_id, patient_id, segment_created_at DESC)"
    )
    grant("segment_search", "SELECT, INSERT, UPDATE, DELETE")


def downgrade() -> None:
    op.execute("DROP TABLE segment_search")
    op.execute("DROP FUNCTION ensure_segment_partition(date)")
    op.execute("DROP TABLE transcript_segments")  # drops every partition and the owned sequence
