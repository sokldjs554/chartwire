"""sessions, DEK guard trigger, audio_chunks ledger, stt_offsets

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import grant

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE sessions (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
  patient_id uuid NOT NULL REFERENCES patients(id), clinician_id uuid NOT NULL REFERENCES users(id),
  state text NOT NULL DEFAULT 'created' CHECK (state IN ('created','recording','paused','ended','transcribed','drafted','signed','purging','purged')),
  script_ref text, codec text NOT NULL DEFAULT 'pcm16le', sample_rate int NOT NULL DEFAULT 16000, chunk_ms int NOT NULL DEFAULT 200,
  stt_provider text NOT NULL DEFAULT 'simulator', scopes_snapshot text[] NOT NULL DEFAULT '{}',
  dek_wrapped bytea, dek_fingerprint bytea, dek_destroyed_at timestamptz,
  ack_seq bigint NOT NULL DEFAULT 0, final_seq bigint, epoch int NOT NULL DEFAULT 0,
  started_at timestamptz, ended_at timestamptz, transcribed_at timestamptz, signed_at timestamptz, purged_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_sessions_clinician ON sessions (tenant_id, clinician_id, created_at DESC)")
    op.execute("CREATE INDEX ix_sessions_patient   ON sessions (tenant_id, patient_id, created_at DESC)")
    op.execute(
        "CREATE INDEX ix_sessions_live      ON sessions (tenant_id) WHERE state IN ('recording','paused')"
    )
    op.execute("""
CREATE FUNCTION trg_dek_no_resurrect() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.dek_destroyed_at IS NOT NULL AND NEW.dek_wrapped IS NOT NULL THEN RAISE EXCEPTION 'DEK destroyed'; END IF;
  RETURN NEW;
END $$""")
    op.execute(
        "CREATE TRIGGER sessions_dek_guard BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION trg_dek_no_resurrect()"
    )
    op.execute(
        "CREATE TRIGGER patients_dek_guard BEFORE UPDATE ON patients FOR EACH ROW EXECUTE FUNCTION trg_dek_no_resurrect()"
    )
    op.execute("""
CREATE TABLE audio_chunks (
  session_id uuid NOT NULL REFERENCES sessions(id), seq bigint NOT NULL, tenant_id uuid NOT NULL,
  byte_len int NOT NULL, sha256 bytea NOT NULL, storage_key text NOT NULL, offset_ms int NOT NULL, flags smallint NOT NULL DEFAULT 0,
  received_at timestamptz NOT NULL, PRIMARY KEY (session_id, seq))""")
    op.execute("""
CREATE TABLE stt_offsets (
  session_id uuid PRIMARY KEY REFERENCES sessions(id), tenant_id uuid NOT NULL,
  last_chunk_seq bigint NOT NULL DEFAULT 0, last_segment_seq int NOT NULL DEFAULT -1, updated_at timestamptz NOT NULL DEFAULT now())""")
    for table in ("sessions", "audio_chunks", "stt_offsets"):
        grant(table, "SELECT, INSERT, UPDATE, DELETE")


def downgrade() -> None:
    op.execute("DROP TABLE stt_offsets")
    op.execute("DROP TABLE audio_chunks")
    op.execute("DROP TRIGGER patients_dek_guard ON patients")
    op.execute("DROP TABLE sessions")
    op.execute("DROP FUNCTION trg_dek_no_resurrect()")
