"""tasks: add tokens_used column for v1.6 cost cap aggregation.

Phase 2 of the v1.6 sprint. Each agent's AgentResult already carries
`tokens_used` and is wired through persist_task_completion. Today that
field lands in Task.output JSONB only; commander's cost cap needs a
top-level integer column to SUM efficiently.

Zero-downtime: nullable column with default 0; pre-existing tasks
unaffected.

Revision ID: 20260502_0001
Revises: 20260430_0001
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0001"
down_revision = "20260430_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "tokens_used")
