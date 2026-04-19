"""ci_fix_runs: add fix_strategy and fix_branch_ci_status for closed-loop Tier 1 fixes.

Revision ID: 20260418_0001
Revises: 20260415_0001
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260418_0001"
down_revision = "20260415_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ci_fix_runs",
        sa.Column(
            "fix_strategy",
            sa.String(20),
            nullable=True,
            comment="'author_branch' | 'fix_branch' — how the fix was committed",
        ),
    )
    op.add_column(
        "ci_fix_runs",
        sa.Column(
            "fix_branch_ci_status",
            sa.String(20),
            nullable=True,
            comment="CI re-run result on author branch: pending | passed | failed | preexisting",
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_fix_runs", "fix_branch_ci_status")
    op.drop_column("ci_fix_runs", "fix_strategy")
