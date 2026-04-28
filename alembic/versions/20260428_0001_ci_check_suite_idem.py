"""ci_fix_runs: add ci_check_suite_id idempotency key (bug #11 A3).

Adds a nullable BIGINT column for GitHub's check_suite.id with a partial
unique index `(repo_full_name, ci_check_suite_id) WHERE ci_check_suite_id IS NOT NULL`.
This lets the webhook handler use the GitHub-supplied identifier as the
deterministic dedup key instead of the time-window heuristic that has
edge cases (see bug #11 deep analysis 2026-04-28).

Why partial index: legacy rows have NULL ci_check_suite_id; we don't want
NULL == NULL to collide. The partial WHERE clause excludes those.

Zero-downtime: the column is nullable with no default; v2 path stays on
the time-window dedup. v3 webhook handler will populate this column on
new dispatches and gain a fast O(1) lookup at the index level.

Revision ID: 20260428_0001
Revises: 20260423_0001
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_0001"
down_revision = "20260423_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ci_fix_runs",
        sa.Column("ci_check_suite_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ci_fix_runs_repo_check_suite_idem",
        "ci_fix_runs",
        ["repo_full_name", "ci_check_suite_id"],
        unique=True,
        postgresql_where=sa.text("ci_check_suite_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ci_fix_runs_repo_check_suite_idem", table_name="ci_fix_runs")
    op.drop_column("ci_fix_runs", "ci_check_suite_id")
