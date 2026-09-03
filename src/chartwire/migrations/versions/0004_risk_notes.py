"""risk_events, notes, note_statements, note_assessments

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import grant

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE risk_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL, session_id uuid NOT NULL REFERENCES sessions(id),
  patient_id uuid NOT NULL, segment_id bigint NOT NULL, segment_created_at timestamptz NOT NULL, segment_seq int NOT NULL,
  category text NOT NULL CHECK (category IN ('suicidal_ideation','self_harm','harm_to_others','substance_acute')),
  severity smallint NOT NULL CHECK (severity BETWEEN 1 AND 3), phrase text NOT NULL, span_start int NOT NULL, span_end int NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb, detector_version text NOT NULL, detected_at timestamptz NOT NULL DEFAULT now(),
  sla_deadline_at timestamptz, acknowledged_at timestamptz, acknowledged_by uuid, escalated_at timestamptz,
  escalation_level smallint NOT NULL DEFAULT 0)""")
    op.execute("CREATE INDEX ix_risk_session ON risk_events (session_id, detected_at)")
    op.execute("CREATE INDEX ix_risk_tenant_time ON risk_events (tenant_id, detected_at DESC)")
    op.execute("""
CREATE TABLE notes (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL, session_id uuid NOT NULL REFERENCES sessions(id), version int NOT NULL,
  status text NOT NULL CHECK (status IN ('drafting','needs_review','verified','abstained','signed','rejected')),
  provider text NOT NULL, model text, prompt_hash bytea, grounding_coverage numeric(5,4), statement_count int NOT NULL DEFAULT 0,
  unsupported_count int NOT NULL DEFAULT 0, abstain_reason text, raw_draft_enc bytea,
  signed_content_enc bytea, legal_hold text CHECK (legal_hold IN ('medical_record')), retention_until timestamptz,
  signed_by uuid, signed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (session_id, version))""")
    op.execute("""
CREATE TABLE note_statements (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL, note_id uuid NOT NULL REFERENCES notes(id),
  section char(1) NOT NULL CHECK (section IN ('S','O','P')), ordinal int NOT NULL, text_enc bytea NOT NULL,
  evidence jsonb NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('supported','unsupported')), verdict_reason text, method text,
  clinician_decision text CHECK (clinician_decision IN ('accept','edit','reject')), edited_text_enc bytea)""")
    op.execute("CREATE INDEX ix_note_statements ON note_statements (note_id, section, ordinal)")
    op.execute("""
CREATE TABLE note_assessments (
  note_id uuid PRIMARY KEY REFERENCES notes(id), tenant_id uuid NOT NULL, text_enc bytea NOT NULL,
  author_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
    for table in ("risk_events", "notes", "note_statements", "note_assessments"):
        grant(table, "SELECT, INSERT, UPDATE, DELETE")


def downgrade() -> None:
    for table in ("note_assessments", "note_statements", "notes", "risk_events"):
        op.execute(f"DROP TABLE {table}")
