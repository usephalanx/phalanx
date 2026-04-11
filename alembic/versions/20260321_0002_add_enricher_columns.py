"""add prompt enricher columns: work_orders.intent, enriched_spec, current_phase + tasks.phase_id, phase_name, role_context

Revision ID: 20260321_0002
Revises: 20260321_0001
Create Date: 2026-03-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260321_0002"
down_revision = "20260321_0001"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # work_orders: intent document (immutable, set once by enricher)
    if not _column_exists("work_orders", "intent"):
        op.add_column("work_orders", sa.Column("intent", sa.dialects.postgresql.JSONB(), nullable=True))

    # work_orders: enriched phase spec (all phases + prompts, set once by enricher)
    if not _column_exists("work_orders", "enriched_spec"):
        op.add_column("work_orders", sa.Column("enriched_spec", sa.dialects.postgresql.JSONB(), nullable=True))

    # work_orders: which phase is currently executing (1-indexed, 0 = not started)
    if not _column_exists("work_orders", "current_phase"):
        op.add_column(
            "work_orders",
            sa.Column("current_phase", sa.Integer(), nullable=False, server_default="0"),
        )

    # tasks: which phase this task belongs to
    if not _column_exists("tasks", "phase_id"):
        op.add_column("tasks", sa.Column("phase_id", sa.Integer(), nullable=True))

    # tasks: human-readable phase name (e.g. "UX Research & IA")
    if not _column_exists("tasks", "phase_name"):
        op.add_column("tasks", sa.Column("phase_name", sa.String(255), nullable=True))

    # tasks: full role context block prepended to builder system prompt
    if not _column_exists("tasks", "role_context"):
        op.add_column("tasks", sa.Column("role_context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "role_context")
    op.drop_column("tasks", "phase_name")
    op.drop_column("tasks", "phase_id")
    op.drop_column("work_orders", "current_phase")
    op.drop_column("work_orders", "enriched_spec")
    op.drop_column("work_orders", "intent")
