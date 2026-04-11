"""Add ci_integrations and ci_fix_runs tables — CI Fixer feature.

Revision ID: 20260408_0001
Revises: 20260407_0001
Create Date: 2026-04-08

Adds:
  - ci_integrations: one row per repo connected to Phalanx CI Fixer
  - ci_fix_runs: one row per CI failure Phalanx attempted to fix

Zero downtime — pure addition. Existing tables untouched.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, UUID

revision = "20260408_0001"
down_revision = "20260407_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ci_integrations ───────────────────────────────────────────────────────
    op.create_table(
        "ci_integrations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("ci_provider", sa.String(50), nullable=False),
        sa.Column("ci_api_key_enc", sa.Text, nullable=True),
        sa.Column("github_token", sa.Text, nullable=True),
        sa.Column("github_installation_id", sa.BigInteger, nullable=True),
        sa.Column("auto_commit", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="2"),
        sa.Column("allowed_authors", ARRAY(TEXT), nullable=True, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("repo_full_name", name="uq_ci_integrations_repo"),
    )
    op.create_index("idx_ci_integrations_repo", "ci_integrations", ["repo_full_name"])

    # ── ci_fix_runs ───────────────────────────────────────────────────────────
    op.create_table(
        "ci_fix_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "integration_id",
            UUID(as_uuid=False),
            sa.ForeignKey("ci_integrations.id"),
            nullable=False,
        ),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=True),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("ci_provider", sa.String(50), nullable=False),
        sa.Column("ci_build_id", sa.String(255), nullable=False),
        sa.Column("build_url", sa.Text, nullable=True),
        sa.Column("failed_jobs", ARRAY(TEXT), nullable=True),
        sa.Column("failure_summary", sa.Text, nullable=True),
        sa.Column("failure_category", sa.String(30), nullable=True),
        sa.Column("fix_commit_sha", sa.String(40), nullable=True),
        sa.Column("fix_pr_number", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_ci_fix_runs_repo_branch", "ci_fix_runs", ["repo_full_name", "branch"]
    )
    op.create_index("idx_ci_fix_runs_status", "ci_fix_runs", ["status"])
    op.create_index("idx_ci_fix_runs_integration", "ci_fix_runs", ["integration_id"])


def downgrade() -> None:
    op.drop_table("ci_fix_runs")
    op.drop_table("ci_integrations")
