"""CI Fixer Phase 2 — outcome tracking + fingerprint history tables.

Revision ID: 20260412_0002
Revises: 20260412_0001
Create Date: 2026-04-12

New tables:
  ci_failure_fingerprints
    Stable identity for a failure class (sha256[:16] of normalised errors).
    Tracks how many times we've seen this class and whether fixes succeeded.
    Phase 3 will use success_count/failure_count to weight fix suggestions.

  ci_fix_outcomes
    One row per outcome poll for a fix PR.
    OutcomeTracker polls at 4h, 24h, and 72h after fix PR creation.
    outcome: 'merged' | 'closed_unmerged' | 'open'
    Lets us learn which fix strategies work over time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260412_0002"
down_revision = "20260412_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ci_failure_fingerprints ────────────────────────────────────────────────
    op.create_table(
        "ci_failure_fingerprints",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("fingerprint_hash", sa.String(16), nullable=False),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("sample_errors", sa.Text, nullable=True),
        # Rolling counters — updated whenever a fix run completes
        sa.Column("seen_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        # Most-recent successful fix patch, stored as JSON for Phase 3 reuse
        sa.Column("last_good_patch_json", sa.Text, nullable=True),
        sa.Column("last_good_tool_version", sa.String(100), nullable=True),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_fp_hash_repo",
        "ci_failure_fingerprints",
        ["fingerprint_hash", "repo_full_name"],
        unique=False,
    )
    op.create_index(
        "idx_fp_hash_unique",
        "ci_failure_fingerprints",
        ["fingerprint_hash"],
        unique=False,
    )

    # ── ci_fix_outcomes ────────────────────────────────────────────────────────
    op.create_table(
        "ci_fix_outcomes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "ci_fix_run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("ci_fix_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("poll_number", sa.Integer, nullable=False),
        # 1 = 4h check, 2 = 24h check, 3 = 72h check
        sa.Column("outcome", sa.String(30), nullable=False),
        # 'merged' | 'closed_unmerged' | 'open' | 'not_found'
        sa.Column("pr_state", sa.String(20), nullable=True),
        # GitHub PR state at poll time: 'open' | 'closed' | 'merged'
        sa.Column("merged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("polled_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_fix_outcomes_run",
        "ci_fix_outcomes",
        ["ci_fix_run_id"],
    )
    op.create_index(
        "idx_fix_outcomes_outcome",
        "ci_fix_outcomes",
        ["outcome"],
    )


def downgrade() -> None:
    op.drop_index("idx_fix_outcomes_outcome", table_name="ci_fix_outcomes")
    op.drop_index("idx_fix_outcomes_run", table_name="ci_fix_outcomes")
    op.drop_table("ci_fix_outcomes")

    op.drop_index("idx_fp_hash_unique", table_name="ci_failure_fingerprints")
    op.drop_index("idx_fp_hash_repo", table_name="ci_failure_fingerprints")
    op.drop_table("ci_failure_fingerprints")
