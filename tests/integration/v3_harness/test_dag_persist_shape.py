"""Catches DAG-persistence shape bugs in cifix_commander.

The 4-task DAG (sre_setup → techlead → engineer → sre_verify) is the
core architectural commitment of v3. If a future change accidentally
drops sre_verify, swaps order, or misses the sre_mode field on either
SRE task, the canary won't ship. These tests assert the shape on a
mocked DB session — no real Postgres needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.agents.cifix_commander import CIFixCommanderAgent
from phalanx.db.models import Task


@pytest.mark.asyncio
async def test_persist_initial_dag_creates_4_tasks_in_order():
    """4 tasks with sequence_num 1..4 in the order:
        sre_setup → techlead → engineer → sre_verify.
    """
    agent = CIFixCommanderAgent(
        run_id="test-run-1",
        work_order_id="test-wo-1",
        project_id="test-project-1",
    )
    captured: list[Task] = []

    session = MagicMock()
    session.add = MagicMock(side_effect=lambda t: captured.append(t))
    session.commit = AsyncMock()

    ci_context = {
        "repo": "owner/repo",
        "pr_number": 42,
        "branch": "fix/foo",
        "failing_job_name": "lint",
        "failing_job_id": "12345",
    }

    await agent._persist_initial_dag(session, ci_context)

    # 4 tasks, in this exact order
    assert len(captured) == 4, [t.agent_role for t in captured]
    roles_in_order = [t.agent_role for t in captured]
    assert roles_in_order == [
        "cifix_sre",
        "cifix_techlead",
        "cifix_engineer",
        "cifix_sre",
    ], roles_in_order

    # sequence_num strictly 1, 2, 3, 4
    assert [t.sequence_num for t in captured] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_initial_dag_sre_modes_correct():
    """seq=1 must carry sre_mode='setup'; seq=4 must carry sre_mode='verify'.
    Engineer + Tech Lead descriptions must NOT contain sre_mode.
    """
    agent = CIFixCommanderAgent(
        run_id="test-run-2",
        work_order_id="test-wo-2",
        project_id="test-project-2",
    )
    captured: list[Task] = []

    session = MagicMock()
    session.add = MagicMock(side_effect=lambda t: captured.append(t))
    session.commit = AsyncMock()

    ci_context = {
        "repo": "owner/repo",
        "pr_number": 1,
        "branch": "main",
        "failing_job_name": "test",
    }

    await agent._persist_initial_dag(session, ci_context)

    setup_ctx = json.loads(captured[0].description)
    verify_ctx = json.loads(captured[3].description)
    techlead_ctx = json.loads(captured[1].description)
    engineer_ctx = json.loads(captured[2].description)

    assert setup_ctx.get("sre_mode") == "setup"
    assert verify_ctx.get("sre_mode") == "verify"
    # Tech Lead and Engineer don't take a sre_mode — that field is
    # only meaningful for SRE tasks.
    assert "sre_mode" not in techlead_ctx
    assert "sre_mode" not in engineer_ctx


@pytest.mark.asyncio
async def test_initial_dag_pending_status():
    """All 4 tasks must start as PENDING — advance_run only dispatches
    PENDING tasks. Any other initial status would block the run.
    """
    agent = CIFixCommanderAgent(
        run_id="test-run-3",
        work_order_id="test-wo-3",
        project_id="test-project-3",
    )
    captured: list[Task] = []

    session = MagicMock()
    session.add = MagicMock(side_effect=lambda t: captured.append(t))
    session.commit = AsyncMock()

    await agent._persist_initial_dag(
        session, {"repo": "owner/repo", "pr_number": 1, "branch": "main"}
    )

    assert all(t.status == "PENDING" for t in captured), [t.status for t in captured]


@pytest.mark.asyncio
async def test_initial_dag_carries_ci_context_in_descriptions():
    """Each Task.description is a JSON-encoded ci_context. Downstream
    agents (techlead, engineer) parse it. If we accidentally serialized
    something other than ci_context, they'd fail at startup.
    """
    agent = CIFixCommanderAgent(
        run_id="test-run-4",
        work_order_id="test-wo-4",
        project_id="test-project-4",
    )
    captured: list[Task] = []

    session = MagicMock()
    session.add = MagicMock(side_effect=lambda t: captured.append(t))
    session.commit = AsyncMock()

    ci_context = {
        "repo": "raj/example-repo",
        "pr_number": 7,
        "branch": "fix/foo",
        "failing_job_name": "lint",
        "failing_command": "ruff check .",
        "sha": "abcdef0",
    }

    await agent._persist_initial_dag(session, ci_context)

    for t in captured:
        ctx = json.loads(t.description)
        assert ctx["repo"] == "raj/example-repo"
        assert ctx["pr_number"] == 7
        assert ctx["branch"] == "fix/foo"
        # failing_command flows to all four; engineer uses it for
        # sandbox verify, sre uses it for verify-mode CI mimicry.
        assert ctx["failing_command"] == "ruff check ."
