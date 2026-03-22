"""add work_orders.slack_thread_ts for Slack thread anchoring

Revision ID: 20260322_0001
Revises: 20260321_0002
Create Date: 2026-03-22

Adds a single nullable VARCHAR(50) column to work_orders.
All existing rows receive NULL — zero-downtime, fully backwards-compatible.
Old Docker images access this via getattr(wo, 'slack_thread_ts', None).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260322_0001"
down_revision = "20260321_0002"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # Slack thread ts — set by gateway after posting the acknowledgment message.
    # NULL means this WorkOrder was created before threading was enabled, or via
    # a non-Slack path (simulator, API). SlackNotifier checks for None before posting.
    if not _column_exists("work_orders", "slack_thread_ts"):
        op.add_column(
            "work_orders",
            sa.Column("slack_thread_ts", sa.String(50), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("work_orders", "slack_thread_ts")
