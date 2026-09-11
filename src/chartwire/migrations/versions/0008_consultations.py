"""consultation_requests: platform-level lead inbox for the demo homepage form

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

from chartwire.migrations.helpers import grant

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tenant_id 없음: 신청자는 아직 테넌트가 아니다. tenants 처럼 RLS 밖이며 app 역할은 INSERT + SELECT 만
    # (UPDATE/DELETE 없음 — 접수함은 런타임에서 고치거나 지우지 않는다). IP·User-Agent 컬럼을 두지 않는다.
    op.execute("""
CREATE TABLE consultation_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_name text NOT NULL, contact_name text NOT NULL, phone text NOT NULL, email text NOT NULL,
  role text CHECK (role IN ('director','manager','staff','other')),
  message text, source text NOT NULL DEFAULT 'console-home',
  created_at timestamptz NOT NULL DEFAULT now())""")
    grant("consultation_requests", "SELECT, INSERT")


def downgrade() -> None:
    op.execute("DROP TABLE consultation_requests")
