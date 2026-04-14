"""CI Fixer Phase 1 — safe branch, fingerprint, tool version columns.

Revision ID: 20260412_0001
Revises: 20260411_0001
Create Date: 2026-04-12

Adds 4 columns to ci_fix_runs (all nullable — zero-downtime migration):
  fix_branch              — the phalanx/ci-fix/{run_id} branch we push to
                            (never the author's branch)
  fingerprint_hash        — sha256[:16] of normalized errors; enables V2 history
  validation_tool_version — "ruff 0.4.1" captured at validation time; surfaced
                            in PR body so reviewers can verify env parity
  outcome_checked         — False until OutcomeTracker processes this run (V2)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260412_0001"
down_revision = "20260411_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ci_fix_runs", sa.Column("fix_branch", sa.String(255), nullable=True))
    op.add_column("ci_fix_runs", sa.Column("fingerprint_hash", sa.String(16), nullable=True))
    op.add_column(
        "ci_fix_runs",
        sa.Column("validation_tool_version", sa.String(100), nullable=True),
    )
    op.add_column(
        "ci_fix_runs",
        sa.Column(
            "outcome_checked",
            sa.Boolean,
            nullable=False,
            server_default="false",
        ),
    )
    # Index for OutcomeTracker query (V2): find FIXED runs not yet processed
    op.create_index(
        "idx_ci_fix_runs_outcome_pending",
        "ci_fix_runs",
        ["status", "outcome_checked"],
    )
    # Index for history retrieval (V2): look up past runs by fingerprint
    op.create_index(
        "idx_ci_fix_runs_fingerprint",
        "ci_fix_runs",
        ["fingerprint_hash"],
    )


def downgrade() -> None:
    op.drop_index("idx_ci_fix_runs_fingerprint", table_name="ci_fix_runs")
    op.drop_index("idx_ci_fix_runs_outcome_pending", table_name="ci_fix_runs")
    op.drop_column("ci_fix_runs", "outcome_checked")
    op.drop_column("ci_fix_runs", "validation_tool_version")
    op.drop_column("ci_fix_runs", "fingerprint_hash")
    op.drop_column("ci_fix_runs", "fix_branch")
