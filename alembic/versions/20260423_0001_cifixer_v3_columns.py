"""cifixer_v3: add work_orders.work_order_type + ci_integrations.cifixer_version.

Foundational schema for CI Fixer v3 multi-agent DAG. Both columns are nullable
with non-null defaults so this is a zero-downtime migration — no locks, no
backfill required. v2 code path is fully unaffected.

- work_orders.work_order_type: distinguishes 'build' (from Slack /phalanx build,
  default) vs 'ci_fix' (created by CI webhook ingest for v3). Build flow code
  neither reads nor writes this column, so existing runs stay on the default.

- ci_integrations.cifixer_version: 'v2' (default — existing CI Fixer pipeline)
  vs 'v3' (new multi-agent DAG). Webhook ingest reads this to decide which
  pipeline to dispatch. v2 stays on until v3 is proven per-repo.

Revision ID: 20260423_0001
Revises: 20260419_0002
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260423_0001"
down_revision = "20260419_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_orders",
        sa.Column(
            "work_order_type",
            sa.String(length=20),
            nullable=False,
            server_default="build",
        ),
    )
    op.create_index(
        "idx_work_orders_type",
        "work_orders",
        ["work_order_type"],
        unique=False,
    )

    op.add_column(
        "ci_integrations",
        sa.Column(
            "cifixer_version",
            sa.String(length=8),
            nullable=False,
            server_default="v2",
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_integrations", "cifixer_version")
    op.drop_index("idx_work_orders_type", table_name="work_orders")
    op.drop_column("work_orders", "work_order_type")
