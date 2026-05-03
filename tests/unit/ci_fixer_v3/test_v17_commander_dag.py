"""Tier-1 unit tests for v1.7 commander DAG persistence.

Validates that `_persist_initial_dag` lays down the correct 5-task DAG
with Challenger between TL and Engineer, and that agent_roles +
sequence_nums match the v1.7 spec.

These tests use a mock session so they run fast and don't require Postgres.
A separate integration test (in v3_harness_t2/) exercises the full
Celery dispatch loop end-to-end.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.agents.cifix_commander import CIFixCommanderAgent


@pytest.fixture
def fake_ci_context() -> dict:
    return {
        "repo": "acme/widget",
        "pr_number": 42,
        "branch": "fix/test",
        "sha": "0" * 40,
        "failing_job_id": "job-1",
        "failing_job_name": "test",
        "failing_command": "pytest tests/",
    }


def _make_commander() -> CIFixCommanderAgent:
    return CIFixCommanderAgent(
        run_id="run-test-v17",
        work_order_id="wo-test-v17",
        project_id="proj-test-v17",
    )


@pytest.mark.asyncio
async def test_v17_initial_dag_persists_five_tasks(fake_ci_context):
    """v1.7 — 5 tasks: SRE setup + TL + Challenger + Engineer + SRE verify."""
    commander = _make_commander()
    added_tasks: list = []
    mock_session = MagicMock()
    mock_session.add = lambda task: added_tasks.append(task)
    mock_session.commit = AsyncMock()

    await commander._persist_initial_dag(mock_session, fake_ci_context)

    assert len(added_tasks) == 5, f"expected 5 tasks; got {len(added_tasks)}"


@pytest.mark.asyncio
async def test_v17_initial_dag_correct_sequence_and_roles(fake_ci_context):
    """Sequence: 1=sre_setup, 2=techlead, 3=challenger, 4=engineer, 5=sre_verify."""
    commander = _make_commander()
    added_tasks: list = []
    mock_session = MagicMock()
    mock_session.add = lambda task: added_tasks.append(task)
    mock_session.commit = AsyncMock()

    await commander._persist_initial_dag(mock_session, fake_ci_context)

    # Sort by sequence_num to be order-insensitive
    by_seq = sorted(added_tasks, key=lambda t: t.sequence_num)
    expected = [
        (1, "cifix_sre_setup", "setup"),
        (2, "cifix_techlead", None),
        (3, "cifix_challenger", None),
        (4, "cifix_engineer", None),
        (5, "cifix_sre_verify", "verify"),
    ]
    for i, (exp_seq, exp_role, exp_mode) in enumerate(expected):
        task = by_seq[i]
        assert task.sequence_num == exp_seq, f"task[{i}] seq mismatch"
        assert task.agent_role == exp_role, (
            f"task[{i}] role mismatch: {task.agent_role!r} vs {exp_role!r}"
        )
        if exp_mode is not None:
            ctx = json.loads(task.description)
            assert ctx.get("sre_mode") == exp_mode, (
                f"task[{i}] sre_mode mismatch: {ctx.get('sre_mode')!r} vs {exp_mode!r}"
            )


@pytest.mark.asyncio
async def test_v17_challenger_task_has_shadow_mode_flag(fake_ci_context):
    """Challenger task description must carry shadow_mode=True so the
    agent knows not to enforce its verdict (yet)."""
    commander = _make_commander()
    added_tasks: list = []
    mock_session = MagicMock()
    mock_session.add = lambda task: added_tasks.append(task)
    mock_session.commit = AsyncMock()

    await commander._persist_initial_dag(mock_session, fake_ci_context)

    challenger = next(t for t in added_tasks if t.agent_role == "cifix_challenger")
    ctx = json.loads(challenger.description)
    assert ctx.get("shadow_mode") is True, (
        f"Challenger task must carry shadow_mode=True; got {ctx}"
    )


@pytest.mark.asyncio
async def test_v17_challenger_task_at_seq_3_between_tl_and_engineer(fake_ci_context):
    """Sequence ordering matters — advance_run dispatches by sequence_num,
    so Challenger MUST be between TL (seq=2) and Engineer (seq=4)."""
    commander = _make_commander()
    added_tasks: list = []
    mock_session = MagicMock()
    mock_session.add = lambda task: added_tasks.append(task)
    mock_session.commit = AsyncMock()

    await commander._persist_initial_dag(mock_session, fake_ci_context)

    by_role: dict[str, int] = {t.agent_role: t.sequence_num for t in added_tasks}

    assert by_role["cifix_techlead"] < by_role["cifix_challenger"], (
        f"TL must come before Challenger; got TL={by_role['cifix_techlead']}, "
        f"Challenger={by_role['cifix_challenger']}"
    )
    assert by_role["cifix_challenger"] < by_role["cifix_engineer"], (
        f"Challenger must come before Engineer; got "
        f"Challenger={by_role['cifix_challenger']}, "
        f"Engineer={by_role['cifix_engineer']}"
    )
    assert by_role["cifix_engineer"] < by_role["cifix_sre_verify"], (
        f"Engineer must come before SRE verify; got "
        f"Engineer={by_role['cifix_engineer']}, "
        f"SRE_verify={by_role['cifix_sre_verify']}"
    )


@pytest.mark.asyncio
async def test_v17_run_cost_cap_bumped_to_30(fake_ci_context):
    """v1.6 cap was $1; v1.7 needs $30 to accommodate Challenger ($5)
    plus iteration headroom. Per spec docs/v17-architecture-gaps.md."""
    from phalanx.agents.cifix_commander import _MAX_RUN_COST_USD
    assert _MAX_RUN_COST_USD == 30.0, (
        f"v1.7 commander run cap must be $30; got ${_MAX_RUN_COST_USD}"
    )
