"""Add agent_traces table — soul/observability layer for agent reasoning.

Revision ID: 20260407_0001
Revises: 20260406_0001
Create Date: 2026-04-07

Adds:
  - agent_traces: structured reasoning traces per agent per task
    (reflections, decisions, uncertainties, disagreements, self-checks)

Zero downtime — pure addition. Existing tables untouched.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260407_0001"
down_revision = "20260406_0001"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("agent_traces"):
        return

    op.create_table(
        "agent_traces",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=False),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_role", sa.String(50), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        # reflection | decision | uncertainty | disagreement | self_check
        sa.Column("trace_type", sa.String(30), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.Column("tokens_used", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index("ix_agent_traces_run_id", "agent_traces", ["run_id"])
    op.create_index("ix_agent_traces_run_task", "agent_traces", ["run_id", "task_id"])
    op.create_index("ix_agent_traces_run_type", "agent_traces", ["run_id", "trace_type"])


def downgrade() -> None:
    op.drop_index("ix_agent_traces_run_type", table_name="agent_traces")
    op.drop_index("ix_agent_traces_run_task", table_name="agent_traces")
    op.drop_index("ix_agent_traces_run_id", table_name="agent_traces")
    op.drop_table("agent_traces")
