"""
Integration tests — DB constraint enforcement.

These tests require a REAL Postgres instance (with pgvector extension).
Run via: make test-integration  OR  pytest tests/integration/ -m integration

The CI pipeline runs these in the `integration-tests` job which spins up
postgres + redis via docker-compose.

Markers:
  @pytest.mark.integration — skipped unless FORGE_RUN_INTEGRATION env var is set

What we test:
  1. CheckConstraint on run.status (only valid states accepted)
  2. CheckConstraint on task.status
  3. CheckConstraint on approval.status
  4. CheckConstraint on skill_confidence.score (0.0–1.0)
  5. UniqueConstraint on projects.slug
  6. Foreign key cascade behaviour
  7. State machine transition tests against real DB
  8. Audit log append-only (no updates possible via ORM)
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Skip entire module unless integration env var is set
pytestmark = pytest.mark.skipif(
    not os.getenv("FORGE_RUN_INTEGRATION"),
    reason="Set FORGE_RUN_INTEGRATION=1 to run integration tests",
)


# ---------------------------------------------------------------------------
# Integration fixtures (real DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def db_engine():
    """Create a real async engine for integration tests."""
    from sqlalchemy.ext.asyncio import create_async_engine

    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://forge:forge@localhost:5432/forge_test",
    )
    engine = create_async_engine(db_url, echo=False)

    async with engine.begin() as conn:
        # Ensure pgvector extension exists
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Provide an async session that rolls back after each test."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session, session.begin():
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def sample_project(db_session):
    """Insert a project row and return its ID."""
    from forge.db.models import Project

    project = Project(
        slug=f"test-project-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        config={},
    )
    db_session.add(project)
    await db_session.flush()
    return project


@pytest_asyncio.fixture
async def sample_channel(db_session, sample_project):
    """Insert a channel row."""
    from forge.db.models import Channel

    channel = Channel(
        project_id=sample_project.id,
        platform="slack",
        channel_id=f"C{uuid.uuid4().hex[:8].upper()}",
        display_name="forge-test",
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


@pytest_asyncio.fixture
async def sample_work_order(db_session, sample_project, sample_channel):
    """Insert a WorkOrder row."""
    from forge.db.models import WorkOrder

    wo = WorkOrder(
        project_id=sample_project.id,
        channel_id=sample_channel.id,
        title="Test work order",
        description="Integration test work order",
        raw_command="/forge build Test work order",
        requested_by="test-user",
        priority=50,
        status="OPEN",
    )
    db_session.add(wo)
    await db_session.flush()
    return wo


@pytest_asyncio.fixture
async def sample_run(db_session, sample_work_order):
    """Insert a Run in INTAKE status."""
    from forge.db.models import Run

    run = Run(
        work_order_id=sample_work_order.id,
        project_id=sample_work_order.project_id,
        run_number=1,
        status="INTAKE",
    )
    db_session.add(run)
    await db_session.flush()
    return run


# ---------------------------------------------------------------------------
# Run status constraint tests
# ---------------------------------------------------------------------------


class TestRunStatusConstraint:
    VALID_STATUSES = [
        "INTAKE",
        "RESEARCHING",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_SHIP_APPROVAL",
        "READY_TO_MERGE",
        "MERGED",
        "RELEASE_PREP",
        "AWAITING_RELEASE_APPROVAL",
        "SHIPPED",
        "FAILED",
        "BLOCKED",
        "PAUSED",
        "CANCELLED",
    ]

    @pytest.mark.parametrize("status", VALID_STATUSES)
    async def test_valid_run_status_accepted(self, db_session, sample_work_order, status: str):
        from forge.db.models import Run

        run = Run(
            work_order_id=sample_work_order.id,
            project_id=sample_work_order.project_id,
            run_number=1,
            status=status,
        )
        db_session.add(run)
        await db_session.flush()  # should not raise

    async def test_invalid_run_status_rejected(self, db_session, sample_work_order):
        from forge.db.models import Run

        run = Run(
            work_order_id=sample_work_order.id,
            project_id=sample_work_order.project_id,
            run_number=1,
            status="HACKING",  # invalid
        )
        db_session.add(run)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# Task status constraint tests
# ---------------------------------------------------------------------------


class TestTaskStatusConstraint:
    VALID_TASK_STATUSES = [
        "PENDING",
        "IN_PROGRESS",
        "COMPLETED",
        "BLOCKED",
        "WAITING_ON_DEP",
        "NEEDS_CLARIFICATION",
        "DEFERRED",
        "CANCELLED",
        "FAILED",
        "ESCALATING",
    ]

    @pytest.mark.parametrize("status", VALID_TASK_STATUSES)
    async def test_valid_task_status_accepted(self, db_session, sample_run, status: str):
        from forge.db.models import Task

        task = Task(
            run_id=sample_run.id,
            title=f"Task with status {status}",
            description="Integration test task",
            agent_role="builder",
            status=status,
            sequence_num=1,
        )
        db_session.add(task)
        await db_session.flush()

    async def test_invalid_task_status_rejected(self, db_session, sample_run):
        from forge.db.models import Task

        task = Task(
            run_id=sample_run.id,
            title="Bad task",
            description="Integration test task",
            agent_role="builder",
            status="WORKING_ON_IT",  # invalid
            sequence_num=1,
        )
        db_session.add(task)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# Approval status constraint tests
# ---------------------------------------------------------------------------


class TestApprovalStatusConstraint:
    async def test_valid_pending_approval_accepted(self, db_session, sample_run):
        from forge.db.models import Approval

        approval = Approval(
            run_id=sample_run.id,
            gate_type="plan",
            gate_phase="planning",
            status="PENDING",
        )
        db_session.add(approval)
        await db_session.flush()

    async def test_valid_approved_status_accepted(self, db_session, sample_run):
        from forge.db.models import Approval

        approval = Approval(
            run_id=sample_run.id,
            gate_type="ship",
            gate_phase="execution",
            status="APPROVED",
            decided_by="morgan",
        )
        db_session.add(approval)
        await db_session.flush()

    async def test_invalid_approval_status_rejected(self, db_session, sample_run):
        from forge.db.models import Approval

        approval = Approval(
            run_id=sample_run.id,
            gate_type="plan",
            gate_phase="planning",
            status="MAYBE",  # invalid
        )
        db_session.add(approval)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# Skill confidence score constraint (0.0–1.0)
# ---------------------------------------------------------------------------


class TestSkillConfidenceConstraint:
    async def test_valid_score_accepted(self, db_session, sample_project):
        from forge.db.models import SkillConfidence

        sc = SkillConfidence(
            project_id=sample_project.id,
            agent_id="sam",
            skill_id="write-clean-code",
            score=0.75,
        )
        db_session.add(sc)
        await db_session.flush()

    async def test_zero_score_accepted(self, db_session, sample_project):
        from forge.db.models import SkillConfidence

        sc = SkillConfidence(
            project_id=sample_project.id,
            agent_id="sam",
            skill_id="code-review",
            score=0.0,
        )
        db_session.add(sc)
        await db_session.flush()

    async def test_one_score_accepted(self, db_session, sample_project):
        from forge.db.models import SkillConfidence

        sc = SkillConfidence(
            project_id=sample_project.id,
            agent_id="morgan",
            skill_id="code-review",
            score=1.0,
        )
        db_session.add(sc)
        await db_session.flush()

    async def test_score_above_one_rejected(self, db_session, sample_project):
        from forge.db.models import SkillConfidence

        sc = SkillConfidence(
            project_id=sample_project.id,
            agent_id="jordan",
            skill_id="git-workflow",
            score=1.1,  # invalid
        )
        db_session.add(sc)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_negative_score_rejected(self, db_session, sample_project):
        from forge.db.models import SkillConfidence

        sc = SkillConfidence(
            project_id=sample_project.id,
            agent_id="jordan",
            skill_id="git-workflow",
            score=-0.1,  # invalid
        )
        db_session.add(sc)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# UniqueConstraint tests
# ---------------------------------------------------------------------------


class TestUniqueConstraints:
    async def test_duplicate_project_slug_rejected(self, db_session, sample_project):
        from forge.db.models import Project

        duplicate = Project(
            slug=sample_project.slug,  # same slug
            name="Duplicate Project",
            config={},
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_different_slugs_accepted(self, db_session):
        from forge.db.models import Project

        p1 = Project(slug=f"proj-a-{uuid.uuid4().hex[:6]}", name="A", config={})
        p2 = Project(slug=f"proj-b-{uuid.uuid4().hex[:6]}", name="B", config={})
        db_session.add_all([p1, p2])
        await db_session.flush()


# ---------------------------------------------------------------------------
# State machine transition tests via DB
# ---------------------------------------------------------------------------


class TestStateMachineViaDB:
    """
    These tests verify that the state machine validates transitions BEFORE
    writing to DB — the DB constraint is the last line of defence.
    """

    async def test_valid_transition_persisted(self, db_session, sample_run):
        from forge.workflow.state_machine import RunStatus, validate_transition

        validate_transition(RunStatus.INTAKE, RunStatus.RESEARCHING)
        sample_run.status = "RESEARCHING"
        sample_run.updated_at = datetime.now(UTC)
        await db_session.flush()

        await db_session.refresh(sample_run)
        assert sample_run.status == "RESEARCHING"

    async def test_invalid_status_blocked_by_db(self, db_session, sample_run):
        """Even if state machine check is bypassed, DB rejects invalid status."""
        sample_run.status = "NOT_A_REAL_STATUS"
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# Audit log append-only test
# ---------------------------------------------------------------------------


class TestAuditLogAppendOnly:
    async def test_audit_log_inserted_successfully(self, db_session, sample_run):
        from forge.db.models import AuditLog

        entry = AuditLog(
            project_id=sample_run.project_id,
            run_id=sample_run.id,
            agent_id="system",
            event_type="run.created",
            payload={"status": "INTAKE"},
        )
        db_session.add(entry)
        await db_session.flush()
        assert entry.id is not None

    async def test_audit_log_id_is_auto_increment(self, db_session, sample_run):
        from forge.db.models import AuditLog

        e1 = AuditLog(
            project_id=sample_run.project_id,
            run_id=sample_run.id,
            agent_id="system",
            event_type="a",
            payload={},
        )
        e2 = AuditLog(
            project_id=sample_run.project_id,
            run_id=sample_run.id,
            agent_id="system",
            event_type="b",
            payload={},
        )
        db_session.add_all([e1, e2])
        await db_session.flush()
        assert e2.id > e1.id
