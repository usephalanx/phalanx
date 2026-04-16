"""ci_fix_run: add pipeline_context_json for multi-agent shared state

Revision ID: 20260415_0001
Revises: 20260412_0005
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0001"
down_revision = "20260412_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add pipeline_context_json — stores the full CIFixContext as a JSON blob.
    # NULL for runs created before this migration; populated by new pipeline runs.
    op.add_column(
        "ci_fix_runs",
        sa.Column(
            "pipeline_context_json",
            sa.Text(),
            nullable=True,
            comment="CIFixContext serialized as JSON — full multi-agent pipeline state",
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_fix_runs", "pipeline_context_json")
