"""performance indexes, zero-downtime partitioned index, search_segments() (perf study "after")

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import (
    ENSURE_PARTITION_V1,
    ENSURE_PARTITION_V2,
    grant_function,
    segment_partitions,
)

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

PARTITIONED_INDEX = "ix_segments_patient_time"
INDEX_COLUMNS = "(tenant_id, patient_id, created_at DESC, id DESC)"

SEARCH_SEGMENTS = """
CREATE FUNCTION search_segments(p_query text, p_mode text, p_patient uuid DEFAULT NULL, p_limit int DEFAULT 50)
RETURNS TABLE (segment_id bigint, session_id uuid, patient_id uuid, speaker text, snippet text, segment_created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE v_tenant uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
BEGIN
  IF v_tenant IS NULL THEN RAISE EXCEPTION 'tenant context missing' USING ERRCODE = '42501'; END IF;
  IF p_mode = 'term' THEN
    RETURN QUERY SELECT s.segment_id, s.session_id, s.patient_id, s.speaker, left(s.text, 120), s.segment_created_at
      FROM segment_search s WHERE s.tenant_id = v_tenant AND s.terms @> ARRAY[p_query] AND (p_patient IS NULL OR s.patient_id = p_patient)
      ORDER BY s.segment_created_at DESC LIMIT p_limit;
  ELSE
    IF length(p_query) < 3 THEN RAISE EXCEPTION 'free-text search needs >= 3 characters' USING ERRCODE = '22023'; END IF;
    RETURN QUERY SELECT s.segment_id, s.session_id, s.patient_id, s.speaker, left(s.text, 120), s.segment_created_at
      FROM segment_search s WHERE s.tenant_id = v_tenant AND s.text ILIKE '%' || p_query || '%' AND (p_patient IS NULL OR s.patient_id = p_patient)
      ORDER BY s.segment_created_at DESC LIMIT p_limit;
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("CREATE INDEX ix_search_text_trgm ON segment_search USING gin (text gin_trgm_ops)")
    op.execute("CREATE INDEX ix_search_terms     ON segment_search USING gin (terms)")
    op.execute(
        "CREATE INDEX ix_risk_open_sla    ON risk_events (tenant_id, sla_deadline_at) WHERE acknowledged_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_outbox_pending   ON outbox_events (next_attempt_at, id) WHERE status = 'pending'"
    )

    # Zero-downtime partitioned index: an invalid parent shell (ON ONLY), each partition built
    # CONCURRENTLY outside the migration transaction, then attached. The parent flips to valid
    # once the last partition index is attached; no partition is ever locked against writes.
    op.execute(f"CREATE INDEX {PARTITIONED_INDEX} ON ONLY transcript_segments {INDEX_COLUMNS}")
    partitions = segment_partitions()
    with op.get_context().autocommit_block():
        for partition in partitions:
            op.execute(
                f"CREATE INDEX CONCURRENTLY {PARTITIONED_INDEX}_{_suffix(partition)} ON {partition} {INDEX_COLUMNS}"
            )
    for partition in partitions:
        op.execute(
            f"ALTER INDEX {PARTITIONED_INDEX} ATTACH PARTITION {PARTITIONED_INDEX}_{_suffix(partition)}"
        )

    op.execute(SEARCH_SEGMENTS)
    grant_function("search_segments(text, text, uuid, int)")
    op.execute(ENSURE_PARTITION_V2)  # future partitions get their index leg automatically


def downgrade() -> None:
    op.execute(ENSURE_PARTITION_V1)
    op.execute("DROP FUNCTION search_segments(text, text, uuid, int)")
    op.execute(f"DROP INDEX {PARTITIONED_INDEX}")  # cascades to every attached partition index
    for index in ("ix_outbox_pending", "ix_risk_open_sla", "ix_search_terms", "ix_search_text_trgm"):
        op.execute(f"DROP INDEX {index}")


def _suffix(partition: str) -> str:
    return partition.removeprefix("transcript_segments_")
