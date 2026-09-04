"""row-level security on every tenant table, every segment partition, role gates (perf study "before")

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import (
    TENANT_POLICY,
    app_role_exists,
    disable_rls,
    enable_rls,
    segment_partitions,
)

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
    # ``segment_search`` is the only table holding *plaintext* transcript. It is deliberately
    # ENABLE-but-not-FORCE so the SECURITY DEFINER ``search_segments()`` — which runs as the table
    # owner and supplies the tenant qual itself — can use ``ix_search_text_trgm`` (ADR-0005, §4.6
    # Q2c/Q2d). That exemption is only narrow while the *runtime* role is not the owner, which is what
    # ADR-0001 §0.7 guarantees for the two-role deployment. In the managed-PG single-role fallback
    # there is no ``chartwire_app`` role and the runtime connects as the owner, so ENABLE alone would
    # be no isolation at all for the plaintext table — exactly the case SPEC §4.1 and ADR-0005 already
    # describe as "the owner exemption is lost and search falls back to the slower RLS path". FORCE it
    # there so the documented behaviour is the actual behaviour (docs/limitations.md §단일 역할).
    # ``search_segments()`` keeps working under FORCE: it runs in the caller's session, so the
    # transaction-local ``app.tenant_id`` is still visible inside the function body.
    enable_rls("segment_search", force=not app_role_exists())
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
