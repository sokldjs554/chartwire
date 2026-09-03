"""row-level security on every tenant table, every segment partition, role gates (perf study "before")

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import TENANT_POLICY, disable_rls, enable_rls, segment_partitions

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

FORCED_TABLES = (
    "users",
    "patients",
    "consents",
    "sessions",
    "audio_chunks",
    "stt_offsets",
    "transcript_segments",
    "risk_events",
    "notes",
    "note_statements",
    "note_assessments",
    "outbox_events",
    "processed_events",
    "dead_letters",
    "purge_jobs",
)
NOTE_TABLES = ("notes", "note_statements", "note_assessments")
ROLE_GATE = "current_setting('app.role', true) IN ('clinician','service','auditor')"
AUDIT_READ_ROLES = "current_setting('app.role', true) IN ('auditor','admin','service')"


def upgrade() -> None:
    for table in FORCED_TABLES:
        enable_rls(table)
    for partition in segment_partitions():  # default partition + every monthly partition existing now
        enable_rls(partition)
    # owner-exempt: the SECURITY DEFINER search function runs as owner and bypasses the policy (ENABLE, not FORCE)
    enable_rls("segment_search", force=False)
    for table in NOTE_TABLES:  # staff/admin get 0 rows even if a router forgets the role check
        op.execute(f"CREATE POLICY role_gate ON {table} AS RESTRICTIVE USING ({ROLE_GATE})")
    # audit_events: tenant_isolation (INSERT/UPDATE/DELETE reach the append-only trigger) plus a
    # RESTRICTIVE read gate — only auditor/admin/service can SELECT (and therefore RETURNING) rows.
    enable_rls("audit_events")
    op.execute(
        f"CREATE POLICY audit_read_gate ON audit_events AS RESTRICTIVE FOR SELECT USING ({AUDIT_READ_ROLES})"
    )


def downgrade() -> None:
    disable_rls("audit_events", "tenant_isolation", "audit_read_gate")
    for table in NOTE_TABLES:
        op.execute(f"DROP POLICY role_gate ON {table}")
    disable_rls("segment_search")
    disable_rls(
        "transcript_segments_default"
    )  # monthly partitions keep the RLS they were created with (0003)
    for table in FORCED_TABLES:
        disable_rls(table)
