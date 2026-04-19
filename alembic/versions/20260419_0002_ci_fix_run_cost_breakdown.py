"""ci_fix_runs: add cost_breakdown_json for CI Fixer v2 per-provider cost tracking.

CI Fixer v2 uses a hybrid model setup (GPT-5.4 main agent + Claude Sonnet 4.6
coder subagent) plus sandbox runtime. We need per-run cost visibility to tune
reasoning_effort and model choice.

JSON shape (written by the v2 agent at run end):
  {
    "gpt_reasoning":   {"input_tokens": int, "output_tokens": int,
                        "reasoning_tokens": int, "cost_usd": float},
    "sonnet_coder":    {"input_tokens": int, "output_tokens": int,
                        "thinking_tokens": int, "cost_usd": float},
    "sandbox_runtime_seconds": float,
    "total_cost_usd": float
  }

Revision ID: 20260419_0002
Revises: 20260419_0001
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260419_0002"
down_revision = "20260419_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ci_fix_runs",
        sa.Column(
            "cost_breakdown_json",
            sa.Text(),
            nullable=True,
            comment=(
                "v2 cost breakdown JSON: gpt_reasoning + sonnet_coder "
                "token/usd splits + sandbox_runtime_seconds + total_cost_usd."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_fix_runs", "cost_breakdown_json")
