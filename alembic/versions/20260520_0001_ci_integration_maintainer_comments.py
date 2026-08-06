"""ci_integrations: opt-in flag for maintainer-facing PR comments.

Default false. Maintainer-facing comments are the FIRST GitHub-visible
side effect Phalanx produces, even in shadow mode. They must be opted
in per-repo by the operator before any comment is ever posted.

Revision ID: 20260520_0001
Revises: 20260513_0001
Create Date: 2026-05-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20260520_0001"
down_revision = "20260513_0001"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _column_exists("ci_integrations", "maintainer_comments_enabled"):
        op.add_column(
            "ci_integrations",
            sa.Column(
                "maintainer_comments_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    if _column_exists("ci_integrations", "maintainer_comments_enabled"):
        op.drop_column("ci_integrations", "maintainer_comments_enabled")
