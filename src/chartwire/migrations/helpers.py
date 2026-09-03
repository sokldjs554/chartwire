"""Shared pieces for the hand-written revisions: role-aware GRANTs, RLS policy DDL,
partition discovery and both versions of ``ensure_segment_partition``."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

APP_ROLE = "chartwire_app"
TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def app_role_exists() -> bool:
    """False in the managed-PG single-role fallback (``CHARTWIRE_DB_SINGLE_ROLE=1``)."""
    bind = op.get_bind()
    return (
        bind.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).scalar() is not None
    )


def grant(table: str, privileges: str) -> None:
    if app_role_exists():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")


def grant_sequence(sequence: str) -> None:
    if app_role_exists():
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {APP_ROLE}")


def grant_function(signature: str) -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    if app_role_exists():
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")


def enable_rls(table: str, *, force: bool = True) -> None:
    """ENABLE (+FORCE) RLS and (re)create the ``tenant_isolation`` policy on ``table``."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    if force:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
    )


def disable_rls(table: str, *policies: str) -> None:
    for policy in policies or ("tenant_isolation",):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def segment_partitions() -> list[str]:
    """Every partition of ``transcript_segments`` (default included), sorted by name."""
    rows = op.get_bind().execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'transcript_segments'::regclass ORDER BY 1"
        )
    )
    return [r[0] for r in rows]


_PARTITION_HEAD = """
CREATE OR REPLACE FUNCTION ensure_segment_partition(p_month date) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_name text := format('transcript_segments_y%sm%s', to_char(p_month, 'YYYY'), to_char(p_month, 'MM'));
  v_from date := date_trunc('month', p_month);
  v_idx  text := 'ix_segments_patient_time_' || format('y%sm%s', to_char(p_month, 'YYYY'), to_char(p_month, 'MM'));
  v_auto text;
BEGIN
  IF to_regclass(v_name) IS NULL THEN
    EXECUTE format('CREATE TABLE %I PARTITION OF transcript_segments FOR VALUES FROM (%L) TO (%L)',
                   v_name, v_from, v_from + interval '1 month');
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', v_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', v_name);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', v_name);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chartwire_app') THEN
      EXECUTE format('GRANT SELECT, INSERT, DELETE ON %I TO chartwire_app', v_name);
    END IF;
  END IF;
"""

ENSURE_PARTITION_V1 = (
    _PARTITION_HEAD
    + """
  RETURN v_name;
END $$;
"""
)
"""Revision 0003: partition + RLS + grants."""

ENSURE_PARTITION_V2 = (
    _PARTITION_HEAD
    + """
  -- 0007: keep the partitioned index ix_segments_patient_time complete. CREATE TABLE ... PARTITION OF
  -- auto-creates and attaches a matching index; normalize its name, or create + attach it ourselves.
  IF to_regclass('ix_segments_patient_time') IS NOT NULL THEN
    SELECT c.relname INTO v_auto
      FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_index x ON x.indexrelid = c.oid
     WHERE i.inhparent = 'ix_segments_patient_time'::regclass AND x.indrelid = v_name::regclass;
    IF v_auto IS NULL THEN
      EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id, patient_id, created_at DESC, id DESC)', v_idx, v_name);
      EXECUTE format('ALTER INDEX ix_segments_patient_time ATTACH PARTITION %I', v_idx);
    ELSIF v_auto <> v_idx THEN
      EXECUTE format('ALTER INDEX %I RENAME TO %I', v_auto, v_idx);
    END IF;
  END IF;
  RETURN v_name;
END $$;
"""
)
"""Revision 0007: additionally creates/attaches (or renames) the per-partition index."""
