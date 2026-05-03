"""Catches DAG-persistence shape bugs in cifix_commander.

The v1.7 5-task DAG (sre_setup → techlead → challenger → engineer →
sre_verify) is the core architectural commitment. If a future change
accidentally drops sre_verify, swaps order, misses sre_mode on either
SRE task, or skips the Challenger task, the canary won't ship. These
tests assert the shape on a mocked DB session — no real Postgres needed.

Note: Challenger runs in shadow mode in v1.7.0 — its verdict is logged
but does NOT gate downstream dispatch. The DAG shape is unconditional;
shadow-mode is enforced by the Challenger agent's own logic returning
success regardless of verdict.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.agents.cifix_commander import CIFixCommanderAgent
from phalanx.db.models import Task


@pytest.mark.asyncio
async def test_persist_initial_dag_creates_5_tasks_in_order():
    """v1.7 — 5 tasks with sequence_num 1..5 in the order:
        sre_setup → techlead → challenger → engineer → sre_verify.
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

    # 5 tasks, in this exact order
    assert len(captured) == 5, [t.agent_role for t in captured]
    roles_in_order = [t.agent_role for t in captured]
    assert roles_in_order == [
        "cifix_sre_setup",  # v1.7 — split from "cifix_sre"
        "cifix_techlead",
        "cifix_challenger",
        "cifix_engineer",
        "cifix_sre_verify",  # v1.7 — split from "cifix_sre"
    ], roles_in_order

    # sequence_num strictly 1, 2, 3, 4, 5
    assert [t.sequence_num for t in captured] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_initial_dag_sre_modes_correct():
    """seq=1 must carry sre_mode='setup'; seq=5 must carry sre_mode='verify'.
    Tech Lead, Challenger, and Engineer descriptions must NOT contain sre_mode.
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
    techlead_ctx = json.loads(captured[1].description)
    challenger_ctx = json.loads(captured[2].description)
    engineer_ctx = json.loads(captured[3].description)
    verify_ctx = json.loads(captured[4].description)

    assert setup_ctx.get("sre_mode") == "setup"
    assert verify_ctx.get("sre_mode") == "verify"
    # Tech Lead, Challenger, and Engineer don't take a sre_mode — that
    # field is only meaningful for SRE tasks.
    assert "sre_mode" not in techlead_ctx
    assert "sre_mode" not in challenger_ctx
    assert "sre_mode" not in engineer_ctx
    # Challenger task carries shadow_mode=True flag
    assert challenger_ctx.get("shadow_mode") is True


@pytest.mark.asyncio
async def test_initial_dag_pending_status():
    """All 5 tasks must start as PENDING — advance_run only dispatches
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
