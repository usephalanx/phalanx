"""CI Fixer Phase 3 — flaky pattern table + dedup columns.

Revision ID: 20260412_0003
Revises: 20260412_0002
Create Date: 2026-04-12

Changes:
  1. New table: ci_flaky_patterns
     Tracks test/lint errors that have historically been flaky (pass sometimes
     without any fix).  Phase 3 uses this to suppress fix attempts for patterns
     that are more likely to self-heal than to need a code change.

  2. New index on ci_fix_runs: (repo_full_name, commit_sha, created_at) for the
     5-minute commit-window dedup query.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260412_0003"
down_revision = "20260412_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ci_flaky_patterns ──────────────────────────────────────────────────────
    op.create_table(
        "ci_flaky_patterns",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("error_code", sa.String(50), nullable=True),
        # e.g. "F401", "E501", or None for pytest failures
        sa.Column("error_file", sa.String(500), nullable=True),
        # normalised path — stripped of line numbers
        sa.Column("flaky_count", sa.Integer, nullable=False, server_default="1"),
        # how many times this pattern appeared and later self-healed (no fix needed)
        sa.Column("total_count", sa.Integer, nullable=False, server_default="1"),
        # total times seen — flaky_rate = flaky_count / total_count
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_flaky_repo_tool_code",
        "ci_flaky_patterns",
        ["repo_full_name", "tool", "error_code"],
        unique=False,
    )

    # ── Commit-window dedup index ──────────────────────────────────────────────
    # Speeds up the 5-minute window check: WHERE repo=? AND commit_sha=? AND created_at > ?
    op.create_index(
        "idx_ci_fix_runs_commit_window",
        "ci_fix_runs",
        ["repo_full_name", "commit_sha", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_ci_fix_runs_commit_window", table_name="ci_fix_runs")
    op.drop_index("idx_flaky_repo_tool_code", table_name="ci_flaky_patterns")
    op.drop_table("ci_flaky_patterns")
