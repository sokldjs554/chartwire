"""outbox_events, processed_events, dead_letters, audit_events (append-only), purge_jobs

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import grant, grant_sequence

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE outbox_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL, aggregate_type text NOT NULL, aggregate_id uuid NOT NULL,
  event_type text NOT NULL, payload jsonb NOT NULL, idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_flight','done','dead')),
  attempts int NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(),
  locked_by text, locked_at timestamptz, lease_until timestamptz, last_error text,
  created_at timestamptz NOT NULL DEFAULT now(), done_at timestamptz)""")
    op.execute("CREATE INDEX ix_outbox_aggregate ON outbox_events (aggregate_id)")
    op.execute("""
CREATE TABLE processed_events (handler text NOT NULL, event_id bigint NOT NULL, tenant_id uuid NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (handler, event_id))""")
    op.execute("""
CREATE TABLE dead_letters (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL, outbox_event_id bigint NOT NULL,
  event_type text NOT NULL, payload jsonb NOT NULL, attempts int NOT NULL, last_error text, died_at timestamptz NOT NULL DEFAULT now(),
  replayed_at timestamptz)""")
    op.execute("""
CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL, at timestamptz NOT NULL DEFAULT now(),
  actor_id uuid, actor_role text, action text NOT NULL, resource_type text NOT NULL, resource_id text, request_id text,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    op.execute("CREATE INDEX ix_audit_tenant_time ON audit_events (tenant_id, at DESC)")
    op.execute("""
CREATE FUNCTION trg_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$""")
    op.execute(
        "CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION trg_audit_immutable()"
    )
    op.execute("""
CREATE TABLE purge_jobs (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL, subject_type text NOT NULL CHECK (subject_type IN ('session','patient')),
  subject_id uuid NOT NULL, reason text NOT NULL CHECK (reason IN ('consent_revoked','admin','retention')), requested_by uuid,
  requested_at timestamptz NOT NULL DEFAULT now(),
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','completed','verified','failed')),
  steps jsonb NOT NULL DEFAULT '[]'::jsonb, counts jsonb NOT NULL DEFAULT '{}'::jsonb, dek_fingerprints jsonb NOT NULL DEFAULT '[]'::jsonb,
  sample_ciphertext bytea, receipt_hash bytea, completed_at timestamptz, verified_at timestamptz, verify_result jsonb)""")
    for table in ("outbox_events", "processed_events", "dead_letters", "purge_jobs"):
        grant(table, "SELECT, INSERT, UPDATE, DELETE")
    grant("audit_events", "SELECT, INSERT")
    grant_sequence(
        "audit_events_id_seq"
    )  # currval() after INSERT: RETURNING is blocked by the read gate (0006)


def downgrade() -> None:
    for table in ("purge_jobs", "audit_events", "dead_letters", "processed_events", "outbox_events"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION trg_audit_immutable()")
