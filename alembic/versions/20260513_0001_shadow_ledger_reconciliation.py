"""shadow_ledger: add reconciliation columns (P0-6 W2 Batch 1 fix)

Closes the evidence-corruption hole from 2026-05-12 W2 Batch 1: CLI
snapshots taken at 15-min poll timeout wrote SAFE_ESCALATE while the
underlying runs were still EXECUTING and later transitioned to
FAILED_INFRA_TIMEOUT. The ledger row never got updated.

Adds 4 nullable columns. The reconciler task uses them to track when
a row was healed and what its previous (incorrect) state was.

Revision ID: 20260513_0001
Revises: 20260512_0001
Create Date: 2026-05-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20260513_0001"
down_revision = "20260512_0001"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _column_exists("shadow_ledger", "reconciled_at"):
        op.add_column(
            "shadow_ledger",
            sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
    if not _column_exists("shadow_ledger", "reconciled_reason"):
        op.add_column(
            "shadow_ledger",
            sa.Column("reconciled_reason", sa.String(80), nullable=True),
        )
    if not _column_exists("shadow_ledger", "previous_verdict"):
        op.add_column(
            "shadow_ledger",
            sa.Column("previous_verdict", sa.String(40), nullable=True),
        )
    if not _column_exists("shadow_ledger", "previous_failure_class"):
        op.add_column(
            "shadow_ledger",
            sa.Column("previous_failure_class", sa.String(40), nullable=True),
        )


def downgrade() -> None:
    for col in (
        "previous_failure_class",
        "previous_verdict",
        "reconciled_reason",
        "reconciled_at",
    ):
        if _column_exists("shadow_ledger", col):
            op.drop_column("shadow_ledger", col)
