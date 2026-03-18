"""Initial schema — all FORGE tables.

Revision ID: 0001
Revises: —
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMPTZ, UUID
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extensions (idempotent) ───────────────────────────────────────────────
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')

    # ── projects ──────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("repo_url", sa.Text),
        sa.Column("repo_provider", sa.String(50), server_default="github"),
        sa.Column("default_branch", sa.String(100), server_default="main"),
        sa.Column("domain", sa.String(50), server_default="web"),
        sa.Column("team_id", sa.String(100)),
        sa.Column("config", JSONB, server_default="{}"),
        sa.Column("onboarding_status", sa.String(50), server_default="pending"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── channels ──────────────────────────────────────────────────────────────
    op.create_table(
        "channels",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id")),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("channel_id", sa.String(100), nullable=False),
        sa.Column("thread_ts", sa.String(50)),
        sa.Column("thread_id", sa.String(100)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("platform", "channel_id", "thread_ts"),
    )

    # ── work_orders ───────────────────────────────────────────────────────────
    op.create_table(
        "work_orders",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("channel_id", UUID(as_uuid=False), sa.ForeignKey("channels.id")),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("raw_command", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), server_default="OPEN"),
        sa.Column("priority", sa.Integer, server_default="50"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("constraints", JSONB, server_default="[]"),
        sa.Column("tags", ARRAY(sa.String), server_default="{}"),
        sa.Column("references", JSONB, server_default="[]"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.CheckConstraint("priority >= 0 AND priority <= 100", name="ck_work_order_priority"),
    )
    op.create_index("idx_work_orders_project_status", "work_orders", ["project_id", "status"])

    # ── runs ──────────────────────────────────────────────────────────────────
    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("work_order_id", UUID(as_uuid=False), sa.ForeignKey("work_orders.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(50), server_default="INTAKE", nullable=False),
        sa.Column("active_branch", sa.String(255)),
        sa.Column("pr_url", sa.Text),
        sa.Column("pr_number", sa.Integer),
        sa.Column("deploy_url", sa.Text),
        sa.Column("error_message", sa.Text),
        sa.Column("error_context", JSONB),
        sa.Column("paused_by_interrupt_id", sa.String(100)),
        sa.Column("token_count", sa.Integer, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float, server_default="0.0"),
        sa.Column("started_at", TIMESTAMPTZ),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("work_order_id", "run_number"),
        sa.CheckConstraint(
            "status IN ("
            "'INTAKE','RESEARCHING','PLANNING','AWAITING_PLAN_APPROVAL',"
            "'EXECUTING','VERIFYING','AWAITING_SHIP_APPROVAL',"
            "'READY_TO_MERGE','MERGED','RELEASE_PREP',"
            "'AWAITING_RELEASE_APPROVAL','SHIPPED',"
            "'FAILED','BLOCKED','PAUSED','CANCELLED'"
            ")",
            name="ck_run_valid_status",
        ),
    )
    op.create_index("idx_runs_project_created", "runs", ["project_id", "created_at"])

    # ── tasks ─────────────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("parent_task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id")),
        sa.Column("sequence_num", sa.Integer, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("agent_role", sa.String(100), nullable=False),
        sa.Column("assigned_agent_id", sa.String(100)),
        sa.Column("status", sa.String(50), server_default="PENDING", nullable=False),
        sa.Column("required_skills", ARRAY(sa.String), server_default="{}"),
        sa.Column("files_likely_touched", ARRAY(sa.String), server_default="{}"),
        sa.Column("depends_on", ARRAY(sa.String), server_default="{}"),
        sa.Column("estimated_complexity", sa.Integer, server_default="3"),
        sa.Column("actual_complexity", sa.Integer),
        sa.Column("output", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("failure_count", sa.Integer, server_default="0"),
        sa.Column("escalation_reason", sa.Text),
        sa.Column("risk_flags", ARRAY(sa.String), server_default="{}"),
        sa.Column("started_at", TIMESTAMPTZ),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "status IN ("
            "'PENDING','IN_PROGRESS','COMPLETED','BLOCKED',"
            "'WAITING_ON_DEP','NEEDS_CLARIFICATION',"
            "'DEFERRED','CANCELLED','FAILED','ESCALATING'"
            ")",
            name="ck_task_valid_status",
        ),
        sa.CheckConstraint("failure_count >= 0", name="ck_task_failures"),
    )
    op.create_index("idx_tasks_run_status", "tasks", ["run_id", "status"])

    # ── artifacts ─────────────────────────────────────────────────────────────
    op.create_table(
        "artifacts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id")),
        sa.Column("task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id")),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("artifact_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("s3_key", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("is_final", sa.Boolean, server_default="false"),
        sa.Column("summary", sa.Text),
        sa.Column("quality_evidence", JSONB),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_artifacts_project_type", "artifacts", ["project_id", "artifact_type"])

    # ── approvals ─────────────────────────────────────────────────────────────
    op.create_table(
        "approvals",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("gate_type", sa.String(50), nullable=False),
        sa.Column("gate_phase", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), server_default="PENDING", nullable=False),
        sa.Column("context_snapshot", JSONB),
        sa.Column("required_evidence", JSONB, server_default="[]"),
        sa.Column("evidence_satisfied", sa.Boolean, server_default="false"),
        sa.Column("requested_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("decided_at", TIMESTAMPTZ),
        sa.Column("decided_by", sa.String(100)),
        sa.Column("decision_note", sa.Text),
        sa.Column("required_approver_level", sa.String(10), server_default="ic6"),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_approval_status"),
    )
    op.create_index(
        "idx_approvals_pending", "approvals", ["run_id"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # ── audit_log (append-only, BigSerial PK) ─────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36)),
        sa.Column("work_order_id", sa.String(36)),
        sa.Column("project_id", sa.String(36)),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("agent_role", sa.String(50)),
        sa.Column("agent_id", sa.String(100)),
        sa.Column("from_state", sa.String(50)),
        sa.Column("to_state", sa.String(50)),
        sa.Column("tool_name", sa.String(100)),
        sa.Column("tokens_used", sa.Integer),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("payload", JSONB, server_default="{}"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_audit_run", "audit_log", ["run_id", "created_at"])
    op.create_index("idx_audit_project", "audit_log", ["project_id", "created_at"])
    op.create_index("idx_audit_event", "audit_log", ["event_type", "created_at"])

    # ── escalations ───────────────────────────────────────────────────────────
    op.create_table(
        "escalations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("from_agent_id", sa.String(100), nullable=False),
        sa.Column("to_agent_id", sa.String(100), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("context_snapshot", JSONB, server_default="{}"),
        sa.Column("urgency", sa.String(20), server_default="normal"),
        sa.Column("status", sa.String(20), server_default="PENDING"),
        sa.Column("resolution", sa.Text),
        sa.Column("resolved_by", sa.String(100)),
        sa.Column("resolved_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── handoffs ──────────────────────────────────────────────────────────────
    op.create_table(
        "handoffs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("from_task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("to_task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("from_agent_id", sa.String(100), nullable=False),
        sa.Column("to_agent_id", sa.String(100), nullable=False),
        sa.Column("files_modified", ARRAY(sa.String), server_default="{}"),
        sa.Column("files_created", ARRAY(sa.String), server_default="{}"),
        sa.Column("decisions_made", JSONB, server_default="[]"),
        sa.Column("open_questions", ARRAY(sa.String), server_default="{}"),
        sa.Column("tests_needed", ARRAY(sa.String), server_default="{}"),
        sa.Column("flags", ARRAY(sa.String), server_default="{}"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── interrupts ────────────────────────────────────────────────────────────
    op.create_table(
        "interrupts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("team_id", sa.String(100), nullable=False),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id")),
        sa.Column("priority", sa.String(5), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("paused_run_ids", ARRAY(sa.String), server_default="{}"),
        sa.Column("interrupt_run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id")),
        sa.Column("status", sa.String(20), server_default="ACTIVE"),
        sa.Column("resolved_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── memory_facts (with pgvector) ──────────────────────────────────────────
    op.create_table(
        "memory_facts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("fact_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, server_default="1.0"),
        sa.Column("status", sa.String(20), server_default="confirmed"),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("is_standing", sa.Boolean, server_default="false"),
        sa.Column("superseded_by", UUID(as_uuid=False), sa.ForeignKey("memory_facts.id")),
        sa.Column("conflicts_with", UUID(as_uuid=False), sa.ForeignKey("memory_facts.id")),
        sa.Column("source_run_id", sa.String(36)),
        sa.Column("source_artifact_id", sa.String(36)),
        sa.Column("relevance_score", sa.Float, server_default="1.0"),
        sa.Column("embedding", Vector(1536)),
        sa.Column("tags", ARRAY(sa.String), server_default="{}"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_memory_facts_project_status", "memory_facts", ["project_id", "status", "fact_type"])
    op.create_index(
        "idx_memory_facts_standing", "memory_facts", ["project_id", "is_standing"],
        postgresql_where=sa.text("is_standing = true"),
    )
    op.create_index(
        "idx_memory_facts_embedding", "memory_facts", ["embedding"],
        postgresql_using="ivfflat",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # ── memory_decisions ──────────────────────────────────────────────────────
    op.create_table(
        "memory_decisions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text),
        sa.Column("rejected_alternatives", ARRAY(sa.String), server_default="{}"),
        sa.Column("approval_id", UUID(as_uuid=False), sa.ForeignKey("approvals.id")),
        sa.Column("decided_by", sa.String(100)),
        sa.Column("is_standing", sa.Boolean, server_default="false"),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── memory_role ───────────────────────────────────────────────────────────
    op.create_table(
        "memory_role",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("agent_role", sa.String(100), nullable=False),
        sa.Column("memory_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("last_used_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("use_count", sa.Integer, server_default="0"),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("project_id", "agent_role", "memory_type", "title"),
    )

    # ── skills ────────────────────────────────────────────────────────────────
    op.create_table(
        "skills",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("domain", sa.String(50), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("stability", sa.String(20), server_default="stable"),
        sa.Column("applicable_roles", ARRAY(sa.String), server_default="{}"),
        sa.Column("min_level", sa.String(10), nullable=False),
        sa.Column("token_cost_estimate", sa.Integer, server_default="2000"),
        sa.Column("spec", JSONB, server_default="{}"),
        sa.Column("deprecated_at", TIMESTAMPTZ),
        sa.Column("superseded_by", sa.String(100), sa.ForeignKey("skills.id")),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── skill_overrides ───────────────────────────────────────────────────────
    op.create_table(
        "skill_overrides",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("overrides", JSONB, server_default="{}"),
        sa.Column("generated_by", sa.String(50), nullable=False),
        sa.Column("source_run_id", sa.String(36)),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── skill_confidence ──────────────────────────────────────────────────────
    op.create_table(
        "skill_confidence",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("score", sa.Float, server_default="0.70"),
        sa.Column("peak_score", sa.Float, server_default="0.70"),
        sa.Column("proficiency_level", sa.String(20), server_default="learning"),
        sa.Column("level_entered_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.Column("execution_count", sa.Integer, server_default="0"),
        sa.Column("last_execution", TIMESTAMPTZ),
        sa.Column("flagged_for_review", sa.Boolean, server_default="false"),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("agent_id", "skill_id", "project_id"),
        sa.CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_confidence_score"),
    )

    # ── skill_executions ──────────────────────────────────────────────────────
    op.create_table(
        "skill_executions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=False), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=False), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("skill_version", sa.String(20), nullable=False),
        sa.Column("load_strategy", sa.String(20), nullable=False),
        sa.Column("proficiency_at_execution", sa.String(20), nullable=False),
        sa.Column("token_cost", sa.Integer),
        sa.Column("outcome_score", sa.Float),
        sa.Column("review_changes_requested", sa.Integer, server_default="0"),
        sa.Column("qa_rounds_needed", sa.Integer, server_default="1"),
        sa.Column("security_flags_raised", sa.Boolean, server_default="false"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── skill_patch_proposals ─────────────────────────────────────────────────
    op.create_table(
        "skill_patch_proposals",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_ref", sa.String(100)),
        sa.Column("update_type", sa.String(50), nullable=False),
        sa.Column("knowledge_section", sa.String(50), nullable=False),
        sa.Column("proposed_content", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("severity", sa.String(20)),
        sa.Column("fast_track", sa.Boolean, server_default="false"),
        sa.Column("notify_all_teams", sa.Boolean, server_default="false"),
        sa.Column("status", sa.String(20), server_default="PROPOSED"),
        sa.Column("reviewed_by", sa.String(100)),
        sa.Column("review_note", sa.Text),
        sa.Column("applied_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── proficiency_history ───────────────────────────────────────────────────
    op.create_table(
        "proficiency_history",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("from_level", sa.String(20), nullable=False),
        sa.Column("to_level", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(50), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── skill_feeds ───────────────────────────────────────────────────────────
    op.create_table(
        "skill_feeds",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("feed_id", sa.String(100), nullable=False, unique=True),
        sa.Column("feed_type", sa.String(50), nullable=False),
        sa.Column("config", JSONB, server_default="{}"),
        sa.Column("affects_skills", ARRAY(sa.String), server_default="{}"),
        sa.Column("last_checked_at", TIMESTAMPTZ),
        sa.Column("is_active", sa.Boolean, server_default="true"),
    )

    # ── skill_feed_items ──────────────────────────────────────────────────────
    op.create_table(
        "skill_feed_items",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("feed_id", UUID(as_uuid=False), sa.ForeignKey("skill_feeds.id"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("proposals_count", sa.Integer, server_default="0"),
        sa.Column("processed_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("feed_id", "external_id"),
    )

    # ── skill_drills ──────────────────────────────────────────────────────────
    op.create_table(
        "skill_drills",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("skill_id", sa.String(100), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("difficulty", sa.String(10), nullable=False),
        sa.Column("task_spec", JSONB, server_default="{}"),
        sa.Column("expected_output", JSONB, server_default="{}"),
        sa.Column("scoring_rubric", JSONB, server_default="{}"),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── skill_drill_results ───────────────────────────────────────────────────
    op.create_table(
        "skill_drill_results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("drill_id", UUID(as_uuid=False), sa.ForeignKey("skill_drills.id"), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("dimension_scores", JSONB, server_default="{}"),
        sa.Column("gaps_identified", ARRAY(sa.String), server_default="{}"),
        sa.Column("triggered_by", sa.String(50)),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )

    # ── onboarding_runs ───────────────────────────────────────────────────────
    op.create_table(
        "onboarding_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=False), sa.ForeignKey("projects.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="PENDING"),
        sa.Column("repo_structure", JSONB),
        sa.Column("patterns_found", JSONB),
        sa.Column("conventions_extracted", JSONB),
        sa.Column("constraints_registered", sa.Integer, server_default="0"),
        sa.Column("skill_overrides_created", sa.Integer, server_default="0"),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    tables = [
        "onboarding_runs", "skill_drill_results", "skill_drills",
        "skill_feed_items", "skill_feeds", "proficiency_history",
        "skill_patch_proposals", "skill_executions", "skill_confidence",
        "skill_overrides", "skills", "memory_role", "memory_decisions",
        "memory_facts", "interrupts", "handoffs", "escalations",
        "audit_log", "approvals", "artifacts", "tasks",
        "runs", "work_orders", "channels", "projects",
    ]
    for table in tables:
        op.drop_table(table)
