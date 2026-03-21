"""add dag columns: epic_id, branch_name, estimated_minutes, task_dependencies

Revision ID: 20260321_0001
Revises: 0001
Create Date: 2026-03-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20260321_0001"
down_revision = "0001"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return insp.has_table(table)


def upgrade() -> None:
    if not _column_exists("tasks", "epic_id"):
        op.add_column("tasks", sa.Column("epic_id", sa.String(), nullable=True))
    if not _column_exists("tasks", "branch_name"):
        op.add_column("tasks", sa.Column("branch_name", sa.String(255), nullable=True))
    if not _column_exists("tasks", "estimated_minutes"):
        op.add_column("tasks", sa.Column("estimated_minutes", sa.Integer(), nullable=False, server_default="30"))

    if not _table_exists("task_dependencies"):
        op.create_table(
            "task_dependencies",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("depends_on_id", sa.String(), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("dependency_type", sa.String(50), nullable=False, server_default="full"),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("task_dependencies")
    op.drop_column("tasks", "estimated_minutes")
    op.drop_column("tasks", "branch_name")
    op.drop_column("tasks", "epic_id")
