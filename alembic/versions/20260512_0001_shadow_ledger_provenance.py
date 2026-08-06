"""shadow_ledger: add phalanx_provenance JSONB (P0-5 W1.9 fix)

Captures, at ledger-write time, exactly which task row each ledger field
was derived from. Without this, the ledger is unauditable — there is no
way to prove `phalanx_confidence` came from the task it claims.

Revision ID: 20260512_0001
Revises: 20260506_0002
Create Date: 2026-05-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260512_0001"
down_revision = "20260506_0002"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _column_exists("shadow_ledger", "phalanx_provenance"):
        op.add_column(
            "shadow_ledger",
            sa.Column("phalanx_provenance", JSONB, nullable=True),
        )


def downgrade() -> None:
    if _column_exists("shadow_ledger", "phalanx_provenance"):
        op.drop_column("shadow_ledger", "phalanx_provenance")
