"""Add demos table and runs.demo_slug for live demo deployments.

Revision ID: 20260406_0001
Revises: 20260322_0001
Create Date: 2026-04-06

Adds:
  - demos table: tracks Dockerized demo deployments per Run
  - runs.demo_slug: URL-safe slug derived from WorkOrder title

All new — zero downtime, fully backwards-compatible.
Existing runs get NULL demo_slug; existing tables untouched.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

revision = "20260406_0001"
down_revision = "20260322_0001"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return table in insp.get_table_names()


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # ── runs.demo_slug ────────────────────────────────────────────────────────
    if not _column_exists("runs", "demo_slug"):
        op.add_column(
            "runs",
            sa.Column("demo_slug", sa.String(100), nullable=True),
        )

    # ── demos table ───────────────────────────────────────────────────────────
    if not _table_exists("demos"):
        op.create_table(
            "demos",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False, unique=True),
            sa.Column("slug", sa.String(100), nullable=False, unique=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("app_type", sa.String(50), nullable=True),
            sa.Column("image_name", sa.String(255), nullable=True),
            sa.Column("container_id", sa.String(100), nullable=True),
            sa.Column("container_name", sa.String(150), nullable=True),
            sa.Column("internal_port", sa.Integer, nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="BUILDING"),
            sa.Column("demo_url", sa.Text, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("last_accessed_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("idx_demos_status", "demos", ["status"])
        op.create_index("idx_demos_run_id", "demos", ["run_id"])


def downgrade() -> None:
    op.drop_table("demos")
    op.drop_column("runs", "demo_slug")
