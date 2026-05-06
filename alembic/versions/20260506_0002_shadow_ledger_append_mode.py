"""shadow_ledger append-mode — replace overwrite-by-replace with append.

The runner's prior idempotency-by-replace on (repo, workflow_run_id)
clobbered prior ledger evidence whenever the same workflow_run_id was
shadowed twice. Concretely: the v1.7.3 hardening S4 proof run
overwrote Path A entry 3's SHIPPED_PROPOSED row (latest verdict
SAFE_ESCALATE remained, prior SHIPPED_PROPOSED was lost).

Append-mode preserves every attempt:

  - Drop UNIQUE(repo, workflow_run_id) ('uq_shadow_ledger_repo_wfrun')
  - Add `attempt_number INT NOT NULL DEFAULT 1`
  - New UNIQUE(repo, workflow_run_id, attempt_number)
  - Existing rows get attempt_number=1 via the default

The runner's create_pending will SELECT MAX(attempt_number) for the
(repo, workflow_run_id) tuple and INSERT one above. First run: 1.
Second on same workflow: 2. And so on.

Zero-downtime: column is NOT NULL with DEFAULT, so existing rows
backfill to 1 automatically without a separate UPDATE step.

Revision ID: 20260506_0002
Revises: 20260506_0001
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260506_0002"
down_revision = "20260506_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_ledger",
        sa.Column(
            "attempt_number",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    op.drop_constraint(
        "uq_shadow_ledger_repo_wfrun",
        "shadow_ledger",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_shadow_ledger_repo_wfrun_attempt",
        "shadow_ledger",
        ["repo", "workflow_run_id", "attempt_number"],
    )

    # New helper index — list-all-attempts-for-this-workflow query path.
    # The unique constraint above creates an index automatically, but it's
    # composite over three columns; this 2-col index is cheaper for the
    # common query "show me every attempt against this workflow_run_id".
    op.create_index(
        "idx_shadow_ledger_workflow_attempts",
        "shadow_ledger",
        ["repo", "workflow_run_id", "attempt_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_shadow_ledger_workflow_attempts",
        table_name="shadow_ledger",
    )

    # Collapse to first attempt only — drop higher attempts (would
    # otherwise violate the restored constraint).
    op.execute(
        """
        DELETE FROM shadow_ledger
        WHERE attempt_number > 1
        """
    )

    op.drop_constraint(
        "uq_shadow_ledger_repo_wfrun_attempt",
        "shadow_ledger",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_shadow_ledger_repo_wfrun",
        "shadow_ledger",
        ["repo", "workflow_run_id"],
    )

    op.drop_column("shadow_ledger", "attempt_number")
