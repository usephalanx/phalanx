"""sre_setup_cache: memoize successful agentic SRE setup plans.

Bug #11+ aftermath / agentic SRE Phase 2. Repeat fixes on the same repo
shouldn't re-run the LLM loop — they should replay the install plan
deterministically. Cache key = sha256 of relevant setup files (pyproject,
workflow YAMLs, pre-commit, tool-versions).

Zero-downtime migration: new table, no FKs to existing rows. Safe to
deploy ahead of the agentic SRE code (table sits empty until first
READY result writes a row).

24h TTL is enforced at SELECT time (not via DB triggers) so we can
adjust the policy without DDL.

Revision ID: 20260430_0001
Revises: 20260428_0001
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260430_0001"
down_revision = "20260428_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sre_setup_cache",
        sa.Column("cache_key", sa.String(length=64), primary_key=True),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("install_plan", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("final_status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "sre_setup_cache_repo_created",
        "sre_setup_cache",
        ["repo_full_name", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("sre_setup_cache_repo_created", table_name="sre_setup_cache")
    op.drop_table("sre_setup_cache")
