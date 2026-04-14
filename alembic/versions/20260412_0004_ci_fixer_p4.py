"""CI Fixer Phase 4 — auto-merge opt-in + tool version parity columns.

Revision ID: 20260412_0004
Revises: 20260412_0003
Create Date: 2026-04-12

Changes:
  1. ci_integrations.auto_merge (BOOLEAN DEFAULT false)
     When true, CIFixerAgent opens a real (non-draft) PR and enables
     GitHub auto-merge if all required status checks pass.
     Default is false — all existing integrations are unchanged.

  2. ci_integrations.min_success_count (INTEGER DEFAULT 3)
     Auto-merge is only enabled after a fingerprint has >= min_success_count
     successful fixes.  Prevents auto-merging untested fix patterns.

  3. ci_fix_runs.tool_version_parity_ok (BOOLEAN)
     Set to True when the tool version at fix time matches the tool version
     at failure time (within minor version).  Surfaced in PR body.
     NULL = parity check not performed (e.g. tool version unavailable).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260412_0004"
down_revision = "20260412_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Auto-merge opt-in on CIIntegration
    op.add_column(
        "ci_integrations",
        sa.Column("auto_merge", sa.Boolean, nullable=False, server_default="false"),
    )
    # Minimum successful fix count before auto-merge is trusted for a fingerprint
    op.add_column(
        "ci_integrations",
        sa.Column("min_success_count", sa.Integer, nullable=False, server_default="3"),
    )
    # Tool version parity flag on CIFixRun
    op.add_column(
        "ci_fix_runs",
        sa.Column("tool_version_parity_ok", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ci_fix_runs", "tool_version_parity_ok")
    op.drop_column("ci_integrations", "min_success_count")
    op.drop_column("ci_integrations", "auto_merge")
