"""shadow_ledger MVP — runs.shadow_mode flag + shadow_ledger table.

v1.7.3-ledger MVP. Captures Phalanx's verdict, proposed patch, confidence,
root cause, affected files, cost, and time per shadow run. Used to compare
against maintainer's actual fix MANUALLY for the first 10 entries; future
work adds an automated matcher + ground-truth scraper.

Zero-downtime: runs.shadow_mode defaults False; existing rows unaffected.

Revision ID: 20260505_0001
Revises: 20260502_0001
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260505_0001"
down_revision = "20260502_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "shadow_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "shadow_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("failing_commit_sha", sa.CHAR(40), nullable=True),
        sa.Column("failure_class", sa.String(40), nullable=True),
        sa.Column(
            "phalanx_run_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "phalanx_verdict",
            sa.String(40),
            nullable=True,
            comment="SHIPPED_PROPOSED / SAFE_ESCALATE / FAILED / PENDING",
        ),
        sa.Column("phalanx_confidence", sa.Float(), nullable=True),
        sa.Column("phalanx_proposed_patch", sa.Text(), nullable=True),
        sa.Column("phalanx_root_cause", sa.Text(), nullable=True),
        sa.Column("phalanx_affected_files", postgresql.JSONB(), nullable=True),
        sa.Column("phalanx_iterations", sa.Integer(), nullable=True),
        sa.Column("phalanx_tool_calls", sa.Integer(), nullable=True),
        sa.Column("phalanx_cost_usd", sa.Float(), nullable=True),
        sa.Column("phalanx_run_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "ground_truth_status",
            sa.String(20),
            nullable=False,
            server_default="pending",
            comment="pending / fixed / abandoned / still_red — manual for MVP",
        ),
        sa.Column("maintainer_fix_commit_sha", sa.CHAR(40), nullable=True),
        sa.Column("maintainer_actual_patch", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("repo", "workflow_run_id", name="uq_shadow_ledger_repo_wfrun"),
    )

    op.create_index(
        "idx_shadow_ledger_repo_created",
        "shadow_ledger",
        ["repo", "created_at"],
    )
    op.create_index(
        "idx_shadow_ledger_pending_gt",
        "shadow_ledger",
        ["created_at"],
        postgresql_where=sa.text("ground_truth_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("idx_shadow_ledger_pending_gt", table_name="shadow_ledger")
    op.drop_index("idx_shadow_ledger_repo_created", table_name="shadow_ledger")
    op.drop_table("shadow_ledger")
    op.drop_column("runs", "shadow_mode")
