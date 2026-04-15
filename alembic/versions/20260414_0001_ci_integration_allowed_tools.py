"""ci_integrations: add allowed_tools column for per-repo tool allowlist.

Revision ID: 20260414_0001
Revises: 20260412_0005
Create Date: 2026-04-14

Adds allowed_tools (ARRAY of VARCHAR) to ci_integrations.
Default: the full set of supported CI tools so existing integrations are unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "20260414_0001"
down_revision = "20260412_0005"
branch_labels = None
depends_on = None

_DEFAULT_TOOLS = [
    "ruff", "cargo", "npm", "mvn", "pytest",
    "go", "tsc", "eslint", "mypy", "gradle",
]


def upgrade() -> None:
    op.add_column(
        "ci_integrations",
        sa.Column(
            "allowed_tools",
            ARRAY(sa.String()),
            nullable=False,
            server_default="{" + ",".join(_DEFAULT_TOOLS) + "}",
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_integrations", "allowed_tools")
