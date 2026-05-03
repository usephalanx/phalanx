"""Tier-1 unit tests for v1.7 engineer routing — `_extract_v17_engineer_steps`
plus end-to-end exercise of `_execute_via_step_interpreter`.

These tests don't touch the DB / Celery / Sonnet — they verify the
routing decision (v1.6 fallback vs v1.7 step path) and the step path's
end-to-end behavior using a real git tempdir workspace.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from phalanx.agents.cifix_engineer import (
    CIFixEngineerAgent,
    _extract_v17_engineer_steps,
)


# ─── _extract_v17_engineer_steps ──────────────────────────────────────────────


class TestExtractV17EngineerSteps:
    def test_returns_none_for_v16_fix_spec_without_task_plan(self):
        fix_spec = {
            "root_cause": "test",
            "fix_spec": "edit",
            "affected_files": ["src.py"],
            "failing_command": "ruff check src.py",
        }
        assert _extract_v17_engineer_steps(fix_spec) is None

    def test_returns_steps_when_engineer_task_present(self):
        fix_spec = {
            "task_plan": [
                {
                    "task_id": "T2",
                    "agent": "cifix_sre_setup",
                    "steps": [],
                },
                {
                    "task_id": "T3",
                    "agent": "cifix_engineer",
                    "steps": [
                        {"id": 1, "action": "replace", "file": "src.py",
                         "old": "x", "new": "y"},
                        {"id": 2, "action": "commit", "message": "fix"},
                        {"id": 3, "action": "push"},
                    ],
                },
            ],
        }
        steps = _extract_v17_engineer_steps(fix_spec)
        assert steps is not None
        assert len(steps) == 3
        assert steps[0]["action"] == "replace"

    def test_returns_none_when_task_plan_has_no_engineer(self):
        fix_spec = {
            "task_plan": [
                {"task_id": "T2", "agent": "cifix_sre_verify", "steps": [
                    {"id": 1, "action": "run", "command": "ruff check ."},
                ]},
            ],
        }
        assert _extract_v17_engineer_steps(fix_spec) is None

    def test_returns_none_when_engineer_steps_empty(self):
        fix_spec = {
            "task_plan": [
                {"task_id": "T2", "agent": "cifix_engineer", "steps": []},
            ],
        }
        assert _extract_v17_engineer_steps(fix_spec) is None

    def test_returns_none_when_task_plan_is_not_a_list(self):
        for bad in [None, {}, "string", 42]:
            assert _extract_v17_engineer_steps({"task_plan": bad}) is None

    def test_skips_non_dict_entries_in_plan(self):
        fix_spec = {
            "task_plan": [
                "not a dict",
                42,
                None,
                {"task_id": "T3", "agent": "cifix_engineer", "steps": [
                    {"id": 1, "action": "push"},
                ]},
            ],
        }
        steps = _extract_v17_engineer_steps(fix_spec)
        assert steps is not None
        assert steps[0]["action"] == "push"


# ─── _execute_via_step_interpreter end-to-end ─────────────────────────────────


@pytest.fixture
def workspace_with_repo():
    """Real tempdir git repo for end-to-end engineer routing tests."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "src.py").write_text("def hello():\n    return 'world'\n")
        subprocess.run(["git", "init", "--quiet"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@l"], cwd=str(ws), check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"], cwd=str(ws), check=True
        )
        yield ws


def _make_agent() -> CIFixEngineerAgent:
    return CIFixEngineerAgent(
        run_id="test-run-eng-v17",
        agent_id="cifix_engineer",
        task_id="test-task-eng-v17",
    )


class TestExecuteViaStepInterpreter:
    def test_happy_path_returns_committed_with_sha(self, workspace_with_repo):
        agent = _make_agent()
        steps = [
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'galaxy'"},
            {"id": 2, "action": "commit", "message": "fix(test): swap"},
        ]
        fix_spec = {
            "root_cause": "x", "fix_spec": "y",
            "task_plan": [{"task_id": "T3", "agent": "cifix_engineer", "steps": steps}],
        }
        ci_context = {"failing_command": "ruff check src.py", "verify_command": "ruff check src.py"}

        result = asyncio.run(agent._execute_via_step_interpreter(
            steps=steps,
            workspace_path=str(workspace_with_repo),
            fix_spec=fix_spec,
            ci_context=ci_context,
            affected_files=["src.py"],
        ))

        assert result.success
        assert result.output["committed"] is True
        assert result.output["v17_path"] is True
        assert result.output["commit_sha"] is not None
        assert "galaxy" in (workspace_with_repo / "src.py").read_text()

    def test_step_precondition_violated_surfaced(self, workspace_with_repo):
        agent = _make_agent()
        steps = [
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "this text does not exist anywhere", "new": "x"},
            {"id": 2, "action": "commit", "message": "should not run"},
        ]
        result = asyncio.run(agent._execute_via_step_interpreter(
            steps=steps,
            workspace_path=str(workspace_with_repo),
            fix_spec={"root_cause": "x", "fix_spec": "y"},
            ci_context={"failing_command": "x"},
            affected_files=["src.py"],
        ))

        assert not result.success
        assert result.output["v17_path"] is True
        assert result.output["failed_step_id"] == 1
        assert result.output["failed_step_error"] == "step_precondition_violated"
        assert "completed_steps" in result.output
        assert result.output["completed_steps"] == []

    def test_plan_without_commit_is_failure(self, workspace_with_repo):
        """TL emitting steps without a commit step is a malformed plan —
        engineer refuses to claim success. Catches a class where TL
        forgets the commit/push tail."""
        agent = _make_agent()
        steps = [
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'galaxy'"},
            # NO commit step — the bug
        ]
        result = asyncio.run(agent._execute_via_step_interpreter(
            steps=steps,
            workspace_path=str(workspace_with_repo),
            fix_spec={"root_cause": "x", "fix_spec": "y"},
            ci_context={"failing_command": "x"},
            affected_files=["src.py"],
        ))

        assert not result.success
        assert result.output["skipped_reason"] == "tl_plan_missing_commit_step"
        assert "completed_steps" in result.output
        assert 1 in result.output["completed_steps"]

    def test_failure_includes_tl_root_cause_for_replan_signal(self, workspace_with_repo):
        """When step fails, the v1.7 engineer surfaces TL's root_cause so
        the upstream (commander/Challenger) has context for the re-plan."""
        agent = _make_agent()
        steps = [{"id": 1, "action": "replace", "file": "src.py",
                  "old": "nope", "new": "x"}]
        result = asyncio.run(agent._execute_via_step_interpreter(
            steps=steps,
            workspace_path=str(workspace_with_repo),
            fix_spec={
                "root_cause": "tz-aware bug in naturaldate",
                "fix_spec": "swap dt.date.today",
            },
            ci_context={"failing_command": "pytest"},
            affected_files=["src.py"],
        ))

        assert not result.success
        assert result.output["tech_lead_root_cause"] == "tz-aware bug in naturaldate"
