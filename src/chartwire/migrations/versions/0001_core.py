"""core: extensions, tenants, users, patients, consents

Revision ID: 0001
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import grant

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("""
CREATE TABLE tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE, name text NOT NULL,
  kek_ref text NOT NULL, record_key_wrapped bytea NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  role text NOT NULL CHECK (role IN ('clinician','staff','admin','auditor','recorder')),
  email_hmac bytea NOT NULL, email_enc bytea NOT NULL, display_name text NOT NULL,
  password_hash text NOT NULL, is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (tenant_id, email_hmac))""")
    op.execute("""
CREATE TABLE patients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  pseudonym text NOT NULL, name_enc bytea, name_hmac bytea, birth_year int, sex char(1), phone_enc bytea,
  dek_wrapped bytea, dek_fingerprint bytea, dek_destroyed_at timestamptz,
  consent_state text NOT NULL DEFAULT 'none' CHECK (consent_state IN ('none','granted','revoked','purged')),
  is_synthetic boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), purged_at timestamptz,
  UNIQUE (tenant_id, pseudonym))""")
    op.execute("CREATE INDEX ix_patients_name_hmac ON patients (tenant_id, name_hmac)")
    op.execute("""
CREATE TABLE consents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, patient_id uuid NOT NULL REFERENCES patients(id),
  scopes text[] NOT NULL CHECK (scopes <@ ARRAY['recording','transcription','ai_drafting','search_index']),
  version int NOT NULL, granted_at timestamptz NOT NULL DEFAULT now(), granted_by uuid, channel text NOT NULL DEFAULT 'console',
  policy_hash bytea, revoked_at timestamptz, revoked_by uuid, revoked_reason text,
  UNIQUE (patient_id, version))""")
    op.execute("CREATE INDEX ix_consents_active ON consents (tenant_id, patient_id) WHERE revoked_at IS NULL")
    grant("tenants", "SELECT")
    for table in ("users", "patients", "consents"):
        grant(table, "SELECT, INSERT, UPDATE, DELETE")


def downgrade() -> None:
    # Extensions stay: they are database-level, trusted, and may be owned by the bootstrap superuser.
    for table in ("consents", "patients", "users", "tenants"):
        op.execute(f"DROP TABLE {table}")
