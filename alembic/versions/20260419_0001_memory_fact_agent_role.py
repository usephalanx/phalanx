"""memory_facts: add agent_role column for per-agent memory scoping.

Prep for CI Fixer v2. Without agent_role scoping, CI Fixer memory writes
and reads would cross-contaminate engineering-agent memory (Builder,
Reviewer, QA, etc.) and vice versa. Nullable for backwards compatibility
with legacy pre-v2 facts; new writes set agent_role explicitly.

Revision ID: 20260419_0001
Revises: 20260418_0001
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260419_0001"
down_revision = "20260418_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_facts",
        sa.Column(
            "agent_role",
            sa.String(100),
            nullable=True,
            comment=(
                "Per-agent scope for memory. NULL = legacy shared fact (pre-v2). "
                "CI Fixer v2 writes agent_role='ci_fixer'; engineering agents "
                "write their own role (builder, reviewer, qa, etc.)."
            ),
        ),
    )
    op.create_index(
        "idx_memory_facts_project_agent_role",
        "memory_facts",
        ["project_id", "agent_role"],
    )


def downgrade() -> None:
    op.drop_index("idx_memory_facts_project_agent_role", table_name="memory_facts")
    op.drop_column("memory_facts", "agent_role")
