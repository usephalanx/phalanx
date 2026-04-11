"""add workspace_path to runs

Revision ID: 20260411_0001
Revises: 20260408_0001
Create Date: 2026-04-11
"""
from alembic import op
import sqlalchemy as sa

revision = "20260411_0001"
down_revision = "20260408_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("workspace_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "workspace_path")
