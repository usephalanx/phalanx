"""Add display_name and avatar_url to user table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add display_name and avatar_url columns to user table."""
    op.add_column(
        "user",
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
    )
    op.add_column(
        "user",
        sa.Column("avatar_url", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    """Remove display_name and avatar_url columns from user table."""
    op.drop_column("user", "avatar_url")
    op.drop_column("user", "display_name")
