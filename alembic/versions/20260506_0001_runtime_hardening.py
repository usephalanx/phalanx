"""v1.7.3 runtime hardening — heartbeat + TTL + TIMED_OUT + failure_class.

Schema changes:
  - tasks.last_heartbeat_at TIMESTAMPTZ NULL
      Updated by long-running agents. Stuck-task detector compares
      NOW() - last_heartbeat_at against ttl_seconds.
  - tasks.ttl_seconds INTEGER NULL
      Per-task heartbeat-staleness budget. NULL = use per-role default
      from phalanx.runtime.heartbeat._DEFAULT_TTL_BY_ROLE.
  - runs.failure_class VARCHAR(40) NULL
      Top-level infra-vs-architecture classification:
        FAILED_INFRA_TIMEOUT, FAILED_INFRA_WORKER_HANG,
        FAILED_SANDBOX_SETUP, FAILED_TL, FAILED_ENGINEER, etc.
  - tasks status check: add 'TIMED_OUT'
  - runs status check: add 'TIMED_OUT'

Zero-downtime: all new columns are nullable; old rows unaffected.

Revision ID: 20260506_0001
Revises: 20260505_0001
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260506_0001"
down_revision = "20260505_0001"
branch_labels = None
depends_on = None


_NEW_TASK_STATUS_LIST = (
    "'PENDING','IN_PROGRESS','COMPLETED','BLOCKED',"
    "'WAITING_ON_DEP','NEEDS_CLARIFICATION',"
    "'DEFERRED','CANCELLED','FAILED','ESCALATING','TIMED_OUT'"
)
_OLD_TASK_STATUS_LIST = (
    "'PENDING','IN_PROGRESS','COMPLETED','BLOCKED',"
    "'WAITING_ON_DEP','NEEDS_CLARIFICATION',"
    "'DEFERRED','CANCELLED','FAILED','ESCALATING'"
)
_NEW_RUN_STATUS_LIST = (
    "'INTAKE','RESEARCHING','PLANNING','AWAITING_PLAN_APPROVAL',"
    "'EXECUTING','VERIFYING','AWAITING_SHIP_APPROVAL',"
    "'READY_TO_MERGE','MERGED','RELEASE_PREP',"
    "'AWAITING_RELEASE_APPROVAL','SHIPPED',"
    "'FAILED','BLOCKED','PAUSED','CANCELLED','TIMED_OUT'"
)
_OLD_RUN_STATUS_LIST = (
    "'INTAKE','RESEARCHING','PLANNING','AWAITING_PLAN_APPROVAL',"
    "'EXECUTING','VERIFYING','AWAITING_SHIP_APPROVAL',"
    "'READY_TO_MERGE','MERGED','RELEASE_PREP',"
    "'AWAITING_RELEASE_APPROVAL','SHIPPED',"
    "'FAILED','BLOCKED','PAUSED','CANCELLED'"
)


def upgrade() -> None:
    # tasks: heartbeat + ttl
    op.add_column(
        "tasks",
        sa.Column("last_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
    )
    op.create_index(
        "idx_tasks_heartbeat_inflight",
        "tasks",
        ["last_heartbeat_at"],
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )

    # runs: failure_class
    op.add_column(
        "runs",
        sa.Column("failure_class", sa.String(40), nullable=True),
    )

    # tasks status: add TIMED_OUT
    op.drop_constraint("ck_task_valid_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_task_valid_status",
        "tasks",
        f"status IN ({_NEW_TASK_STATUS_LIST})",
    )

    # runs status: add TIMED_OUT
    op.drop_constraint("ck_run_valid_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_run_valid_status",
        "runs",
        f"status IN ({_NEW_RUN_STATUS_LIST})",
    )


def downgrade() -> None:
    # Revert any TIMED_OUT rows to FAILED so the old constraint is satisfiable.
    op.execute("UPDATE tasks SET status='FAILED' WHERE status='TIMED_OUT'")
    op.execute("UPDATE runs SET status='FAILED' WHERE status='TIMED_OUT'")

    op.drop_constraint("ck_run_valid_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_run_valid_status",
        "runs",
        f"status IN ({_OLD_RUN_STATUS_LIST})",
    )
    op.drop_constraint("ck_task_valid_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_task_valid_status",
        "tasks",
        f"status IN ({_OLD_TASK_STATUS_LIST})",
    )

    op.drop_column("runs", "failure_class")
    op.drop_index("idx_tasks_heartbeat_inflight", table_name="tasks")
    op.drop_column("tasks", "ttl_seconds")
    op.drop_column("tasks", "last_heartbeat_at")
