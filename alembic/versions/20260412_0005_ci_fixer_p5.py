"""CI Fixer Phase 5 — cross-repo pattern registry + proactive PR scan table.

Revision ID: 20260412_0005
Revises: 20260412_0004
Create Date: 2026-04-12

New tables:
  ci_pattern_registry
    Cross-repo fix pattern store.  A pattern is promoted here when it has been
    validated in >= 2 different repos.  Other repos can query this table to
    find proven fixes for their fingerprints.

  ci_proactive_scans
    Tracks proactive PR scans.  When a PR is opened, Phalanx can scan for
    known patterns that would fail and comment proactively before CI runs.
    One row per PR scan, contains the findings as JSON.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260412_0005"
down_revision = "20260412_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ci_pattern_registry ────────────────────────────────────────────────────
    op.create_table(
        "ci_pattern_registry",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("fingerprint_hash", sa.String(16), nullable=False, unique=True),
        # The canonical fingerprint — shared across repos
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("error_codes", sa.ARRAY(sa.String(50)), nullable=True),
        # Array of error codes seen in this pattern (e.g. ["F401", "F811"])
        sa.Column("description", sa.Text, nullable=True),
        # Human-readable description of what this pattern fixes
        sa.Column("patch_template_json", sa.Text, nullable=True),
        # JSON template of the fix — relative to error location, not absolute lines
        sa.Column("repo_count", sa.Integer, nullable=False, server_default="1"),
        # Number of distinct repos where this fix has succeeded
        sa.Column("total_success_count", sa.Integer, nullable=False, server_default="1"),
        # Total successful applications across all repos
        sa.Column("promoted_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_pattern_registry_hash",
        "ci_pattern_registry",
        ["fingerprint_hash"],
        unique=True,
    )
    op.create_index(
        "idx_pattern_registry_tool",
        "ci_pattern_registry",
        ["tool"],
        unique=False,
    )

    # ── ci_proactive_scans ─────────────────────────────────────────────────────
    op.create_table(
        "ci_proactive_scans",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("findings_json", sa.Text, nullable=True),
        # JSON list of {fingerprint_hash, description, severity} findings
        sa.Column("comment_posted", sa.Boolean, nullable=False, server_default="false"),
        # Whether a GitHub comment was posted for this scan
        sa.Column("comment_id", sa.BigInteger, nullable=True),
        # GitHub comment ID if posted
        sa.Column("scan_duration_ms", sa.Integer, nullable=True),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_proactive_scans_pr",
        "ci_proactive_scans",
        ["repo_full_name", "pr_number"],
        unique=False,
    )
    op.create_index(
        "idx_proactive_scans_commit",
        "ci_proactive_scans",
        ["commit_sha"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_proactive_scans_commit", table_name="ci_proactive_scans")
    op.drop_index("idx_proactive_scans_pr", table_name="ci_proactive_scans")
    op.drop_table("ci_proactive_scans")

    op.drop_index("idx_pattern_registry_tool", table_name="ci_pattern_registry")
    op.drop_index("idx_pattern_registry_hash", table_name="ci_pattern_registry")
    op.drop_table("ci_pattern_registry")
