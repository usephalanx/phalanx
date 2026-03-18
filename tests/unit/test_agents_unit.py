"""
Unit tests for all FORGE agents.
Tests the business logic by mocking Anthropic API, DB session, and file I/O.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Shared helpers ─────────────────────────────────────────────────────────────

def make_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


def make_db_context(session):
    @asynccontextmanager
    async def _get_db():
        yield session
    return _get_db


def make_task(
    task_id="t-1",
    run_id="r-1",
    title="Implement auth",
    description="Add JWT auth",
    agent_role="builder",
    sequence_num=1,
    status="PENDING",
    output=None,
    assigned_agent_id="builder",
):
    t = MagicMock()
    t.id = task_id
    t.run_id = run_id
    t.title = title
    t.description = description
    t.agent_role = agent_role
    t.sequence_num = sequence_num
    t.status = status
    t.output = output or {}
    t.assigned_agent_id = assigned_agent_id
    return t


def make_run(
    run_id="r-1",
    project_id="proj-1",
    active_branch="feature/auth",
    work_order_id="wo-1",
):
    r = MagicMock()
    r.id = run_id
    r.project_id = project_id
    r.active_branch = active_branch
    r.work_order_id = work_order_id
    return r


def make_work_order(wo_id="wo-1", title="Add auth", description="Add JWT authentication"):
    wo = MagicMock()
    wo.id = wo_id
    wo.title = title
    wo.description = description
    return wo


def mock_claude_response(text: str):
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.model = "claude-sonnet-4-6"
    return response


# ══════════════════════════════════════════════════════════════════════════════
# PlannerAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerAgent:
    def _make_agent(self):
        from forge.agents.planner import PlannerAgent
        return PlannerAgent(run_id="r-1", task_id="t-1", agent_id="planner")

    async def test_execute_returns_success(self):
        agent = self._make_agent()
        task = make_task()
        run = make_run()

        plan = {
            "task_title": "Implement auth",
            "approach": "Use JWT tokens",
            "files": ["forge/auth.py"],
            "implementation_steps": ["Create JWT handler"],
            "test_strategy": "Unit tests",
            "acceptance_criteria": ["Auth works"],
            "edge_cases": ["Expired token"],
            "estimated_complexity": 3,
        }

        session = make_session()
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        session.execute.side_effect = [task_result, run_result, MagicMock(), MagicMock(), MagicMock()]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(plan))

        with (
            patch("forge.agents.planner.get_db", make_db_context(session)),
            patch("forge.agents.base.get_anthropic_client", return_value=mock_client),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        assert result.success is True
        assert result.output["approach"] == "Use JWT tokens"

    async def test_execute_task_not_found_returns_failure(self):
        agent = self._make_agent()
        session = make_session()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        with patch("forge.agents.planner.get_db", make_db_context(session)):
            result = await agent.execute()

        assert result.success is False
        assert "not found" in result.error

    async def test_execute_handles_bad_json_gracefully(self):
        agent = self._make_agent()
        task = make_task()
        run = make_run()
        session = make_session()

        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        session.execute.side_effect = [task_result, run_result, MagicMock(), MagicMock(), MagicMock()]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not json at all")

        with (
            patch("forge.agents.planner.get_db", make_db_context(session)),
            patch("forge.agents.base.get_anthropic_client", return_value=mock_client),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        # Fallback plan should be returned
        assert result.success is True
        assert "task_title" in result.output


# ══════════════════════════════════════════════════════════════════════════════
# ReviewerAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestReviewerAgent:
    def _make_agent(self):
        from forge.agents.reviewer import ReviewerAgent
        return ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

    async def test_execute_approved_verdict(self):
        agent = self._make_agent()
        task = make_task(agent_role="reviewer", sequence_num=2)
        run = make_run()

        review = {
            "verdict": "APPROVED",
            "summary": "Code looks good",
            "blocking_reason": None,
            "issues": [],
            "positives": ["Clean implementation"],
            "test_coverage_ok": True,
            "security_ok": True,
        }

        session = make_session()
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        builder_result = MagicMock()
        builder_task = MagicMock()
        builder_task.output = {"summary": "Added JWT", "files_written": []}
        builder_result.scalar_one_or_none.return_value = builder_task

        session.execute.side_effect = [task_result, run_result, builder_result, run_result, MagicMock(), MagicMock()]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(review))

        with (
            patch("forge.agents.reviewer.get_db", make_db_context(session)),
            patch("forge.agents.base.get_anthropic_client", return_value=mock_client),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        assert result.success is True
        assert result.output["verdict"] == "APPROVED"

    def test_read_changed_files_with_existing_files(self, tmp_path):
        from forge.agents.reviewer import ReviewerAgent
        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        (tmp_path / "auth.py").write_text("def authenticate(): pass")
        builder_output = {"files_written": ["auth.py"]}

        context = agent._read_changed_files(tmp_path, builder_output)
        assert "auth.py" in context
        assert "authenticate" in context

    def test_read_changed_files_missing_workspace(self, tmp_path):
        from forge.agents.reviewer import ReviewerAgent
        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        nonexistent = tmp_path / "does_not_exist"
        context = agent._read_changed_files(nonexistent, {"files_written": ["a.py"]})
        assert context == ""

    def test_read_changed_files_handles_deleted(self, tmp_path):
        from forge.agents.reviewer import ReviewerAgent
        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        builder_output = {"files_written": ["DELETE:old_file.py"]}
        context = agent._read_changed_files(tmp_path, builder_output)
        assert "DELETED" in context

    def test_read_changed_files_respects_max_bytes(self, tmp_path):
        from forge.agents.reviewer import ReviewerAgent, _MAX_CODE_BYTES
        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        # Create files totalling more than max
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("x" * 8_000)

        builder_output = {"files_written": [f"file{i}.py" for i in range(5)]}
        context = agent._read_changed_files(tmp_path, builder_output)
        assert len(context.encode()) < _MAX_CODE_BYTES * 2  # should be capped, with some slack for headers


# ══════════════════════════════════════════════════════════════════════════════
# SecurityAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityAgent:
    def _make_agent(self):
        from forge.agents.security import SecurityAgent
        return SecurityAgent(run_id="r-1", task_id="t-1", agent_id="security")

    async def test_execute_passed_scan(self):
        agent = self._make_agent()
        task = make_task(agent_role="security")
        run = make_run()
        session = make_session()

        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        session.execute.side_effect = [task_result, run_result, MagicMock()]

        scan_result = {
            "overall_passed": True,
            "max_severity": "none",
            "blocking_reason": None,
            "scanned_at": "2026-01-01T00:00:00",
            "scans": [],
        }

        with (
            patch("forge.agents.security.get_db", make_db_context(session)),
            patch.object(agent, "_run_security_pipeline", AsyncMock(return_value=scan_result)),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        assert result.success is True
        assert result.output["overall_passed"] is True

    async def test_execute_failed_scan_still_completes(self):
        """Even failed security scan marks task COMPLETED — gate decision is at ship."""
        agent = self._make_agent()
        task = make_task(agent_role="security")
        run = make_run()
        session = make_session()

        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        session.execute.side_effect = [task_result, run_result, MagicMock()]

        scan_result = {
            "overall_passed": False,
            "max_severity": "high",
            "blocking_reason": "Hardcoded secrets found",
            "scanned_at": "2026-01-01T00:00:00",
            "scans": [{"tool": "detect-secrets", "passed": False, "max_severity": "high",
                        "findings_count": 2, "error": None}],
        }

        with (
            patch("forge.agents.security.get_db", make_db_context(session)),
            patch.object(agent, "_run_security_pipeline", AsyncMock(return_value=scan_result)),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        assert result.success is True  # task-level success even on failed scan
        assert result.output["overall_passed"] is False
        assert result.output["max_severity"] == "high"

    async def test_pipeline_exception_returns_degraded_result(self, tmp_path):
        agent = self._make_agent()
        run = make_run()

        with patch("forge.guardrails.security_pipeline.SecurityPipeline", side_effect=ImportError("not installed")):
            result = await agent._run_security_pipeline(tmp_path, run)

        assert result["overall_passed"] is False
        assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# ReleaseAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestReleaseAgent:
    def _make_agent(self):
        from forge.agents.release import ReleaseAgent
        return ReleaseAgent(run_id="r-1", task_id="t-1", agent_id="release")

    async def test_execute_without_github_token(self):
        agent = self._make_agent()
        task = make_task(agent_role="release")
        run = make_run()
        wo = make_work_order()
        session = make_session()

        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        run_result = MagicMock()
        run_result.scalar_one.return_value = run
        wo_result = MagicMock()
        wo_result.scalar_one_or_none.return_value = wo
        tasks_result = MagicMock()
        tasks_result.scalars.return_value.all.return_value = []
        project_result = MagicMock()
        project_result.scalar_one.return_value = run

        session.execute.side_effect = [
            task_result, run_result, wo_result, tasks_result,
            run_result, MagicMock(), MagicMock(), MagicMock(),
        ]

        notes = {
            "title": "Release Notes: Add auth",
            "summary": "Added JWT auth",
            "changes": [{"type": "feat", "description": "JWT authentication"}],
            "testing": "Unit tests pass",
            "rollback": "Revert PR",
            "breaking_changes": [],
        }

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(notes))

        with (
            patch("forge.agents.release.get_db", make_db_context(session)),
            patch("forge.agents.base.get_anthropic_client", return_value=mock_client),
            patch("forge.agents.release.settings") as mock_settings,
            patch.object(agent, "_audit", AsyncMock()),
            patch.object(agent, "_persist_artifact", AsyncMock()),
        ):
            mock_settings.github_token = None  # no GitHub token
            result = await agent.execute()

        assert result.success is True
        assert result.output["release_notes"]["title"] == "Release Notes: Add auth"
        assert result.output["pr_url"] is None  # no PR created

    async def test_generate_release_notes_valid_json(self):
        agent = self._make_agent()
        run = make_run()
        wo = make_work_order()

        notes = {
            "title": "Release Notes: Feature X",
            "summary": "Implemented feature X",
            "changes": [{"type": "feat", "description": "Feature X"}],
            "testing": "All tests pass",
            "rollback": "Revert the PR",
            "breaking_changes": [],
        }

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(notes))

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            result = await agent._generate_release_notes(run, wo, [])

        assert result["title"] == "Release Notes: Feature X"
        assert result["changes"][0]["type"] == "feat"

    async def test_generate_release_notes_bad_json_falls_back(self):
        agent = self._make_agent()
        run = make_run()
        wo = make_work_order(title="My Feature")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not valid json")

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            result = await agent._generate_release_notes(run, wo, [])

        assert "title" in result
        assert "My Feature" in result["title"]

    async def test_create_github_pr_skips_without_token(self):
        agent = self._make_agent()
        run = make_run(active_branch="feature/auth")
        wo = make_work_order()

        with patch("forge.agents.release.settings") as mock_settings:
            mock_settings.github_token = None
            result = await agent._create_github_pr(run, wo, {})

        assert result == {}

    async def test_create_github_pr_skips_without_branch(self):
        agent = self._make_agent()
        run = make_run(active_branch=None)
        wo = make_work_order()

        with patch("forge.agents.release.settings") as mock_settings:
            mock_settings.github_token = "ghp_token"
            result = await agent._create_github_pr(run, wo, {})

        assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# CommanderAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestCommanderAgent:
    def _make_agent(self):
        from forge.agents.commander import CommanderAgent
        return CommanderAgent(
            run_id="r-1",
            work_order_id="wo-1",
            project_id="proj-1",
            agent_id="commander",
        )

    async def test_execute_returns_failure_when_work_order_not_found(self):
        agent = self._make_agent()
        session = make_session()

        wo_result = MagicMock()
        wo_result.scalar_one_or_none.return_value = None
        session.execute.return_value = wo_result

        with patch("forge.db.session.get_db", make_db_context(session)):
            result = await agent.execute()

        assert result.success is False
        assert "not found" in result.error

    async def test_generate_task_plan_valid_json(self):
        agent = self._make_agent()
        wo = make_work_order()

        plan = {
            "tasks": [
                {
                    "sequence_num": 1,
                    "title": "Implement auth",
                    "description": "Add JWT",
                    "agent_role": "builder",
                    "depends_on": [],
                    "files_likely_touched": ["auth.py"],
                    "estimated_complexity": 3,
                }
            ]
        }

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(plan))

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            result = await agent._generate_task_plan(wo, "memory context")

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["agent_role"] == "builder"

    async def test_generate_task_plan_bad_json_returns_fallback(self):
        agent = self._make_agent()
        wo = make_work_order(title="Build feature")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not json")

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            result = await agent._generate_task_plan(wo, "")

        # Fallback: one builder task
        assert "tasks" in result
        assert result["tasks"][0]["agent_role"] == "builder"

    async def test_persist_task_plan_adds_tasks(self):
        agent = self._make_agent()
        session = make_session()

        plan = {
            "tasks": [
                {"sequence_num": 1, "title": "Task 1", "description": "D1",
                 "agent_role": "builder", "depends_on": [], "files_likely_touched": []},
                {"sequence_num": 2, "title": "Task 2", "description": "D2",
                 "agent_role": "reviewer", "depends_on": [], "files_likely_touched": []},
            ]
        }

        await agent._persist_task_plan(session, plan)

        assert session.add.call_count == 2
        session.commit.assert_awaited_once()
