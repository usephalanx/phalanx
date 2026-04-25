"""Tier-2 fixtures — real Postgres, schema-validated provider mocks.

Mirrors tests/integration/test_db_constraints.py's db_engine + db_session
pattern so v2 and v3 integration tests share infra.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope="module")
async def db_engine():
    """Module-scoped async engine. Skips the entire module if Postgres
    isn't reachable so dev workflow doesn't get blocked."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://forge:forge@localhost:5432/forge_test",
    )
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    except Exception as exc:
        pytest.skip(f"Postgres not reachable at {db_url}: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Per-test session with rollback. Tests can assert on flushed-but-
    not-committed state — clean for every test."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session, session.begin():
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def cifix_project(db_session):
    """A v3-style Project (slug 'cifix_*') for ci_fix work orders."""
    from phalanx.db.models import Project

    project = Project(
        slug=f"cifix_t2_{uuid.uuid4().hex[:8]}",
        name="Tier-2 v3 Test Project",
        repo_url="https://github.com/example/repo",
        repo_provider="github",
        domain="ci_fix",
        onboarding_status="active",
    )
    db_session.add(project)
    await db_session.flush()
    return project


@pytest_asyncio.fixture
async def cifix_work_order(db_session, cifix_project):
    """A WorkOrder(work_order_type='ci_fix') with realistic raw_command
    JSON so commander can parse ci_context out of it.
    """
    import json as _json

    from phalanx.db.models import WorkOrder

    ci_context = {
        "repo": "owner/repo",
        "branch": "fix/foo",
        "sha": "abc123def456",
        "pr_number": 7,
        "failing_job_id": "job-100",
        "failing_job_name": "lint",
        "ci_provider": "github_actions",
    }
    wo = WorkOrder(
        project_id=cifix_project.id,
        channel_id=None,
        title="Fix CI: owner/repo#7 — lint",
        description="Tier-2 fixture — synthetic ci_fix workorder.",
        raw_command=_json.dumps(ci_context),
        requested_by="ci_webhook",
        priority=60,
        status="OPEN",
        work_order_type="ci_fix",
    )
    db_session.add(wo)
    await db_session.flush()
    return wo
