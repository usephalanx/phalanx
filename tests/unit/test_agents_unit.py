"""
Unit tests for all FORGE agents.
Tests the business logic by mocking Anthropic API, DB session, and file I/O.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
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
        from phalanx.agents.planner import PlannerAgent

        return PlannerAgent(run_id="r-1", task_id="t-1", agent_id="planner")

    async def test_execute_returns_success(self):
        agent = self._make_agent()
        task = make_task()
        run = make_run()

        plan = {
            "task_title": "Implement auth",
            "approach": "Use JWT tokens",
            "files": ["phalanx/auth.py"],
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
        session.execute.side_effect = [
            task_result,
            run_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(plan))

        with (
            patch("phalanx.agents.planner.get_db", make_db_context(session)),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
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

        with patch("phalanx.agents.planner.get_db", make_db_context(session)):
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
        session.execute.side_effect = [
            task_result,
            run_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not json at all")

        with (
            patch("phalanx.agents.planner.get_db", make_db_context(session)),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
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
        from phalanx.agents.reviewer import ReviewerAgent

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

        session.execute.side_effect = [
            task_result,
            run_result,
            builder_result,
            run_result,
            MagicMock(),
            MagicMock(),
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response(json.dumps(review))

        with (
            patch("phalanx.agents.reviewer.get_db", make_db_context(session)),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            result = await agent.execute()

        assert result.success is True
        assert result.output["verdict"] == "APPROVED"

    def test_read_changed_files_with_existing_files(self, tmp_path):
        from phalanx.agents.reviewer import ReviewerAgent

        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        (tmp_path / "auth.py").write_text("def authenticate(): pass")
        builder_output = {"files_written": ["auth.py"]}

        context = agent._read_changed_files(tmp_path, builder_output)
        assert "auth.py" in context
        assert "authenticate" in context

    def test_read_changed_files_missing_workspace(self, tmp_path):
        from phalanx.agents.reviewer import ReviewerAgent

        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        nonexistent = tmp_path / "does_not_exist"
        context = agent._read_changed_files(nonexistent, {"files_written": ["a.py"]})
        assert context == ""

    def test_read_changed_files_handles_deleted(self, tmp_path):
        from phalanx.agents.reviewer import ReviewerAgent

        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        builder_output = {"files_written": ["DELETE:old_file.py"]}
        context = agent._read_changed_files(tmp_path, builder_output)
        assert "DELETED" in context

    def test_read_changed_files_respects_max_bytes(self, tmp_path):
        from phalanx.agents.reviewer import _MAX_CODE_BYTES, ReviewerAgent

        agent = ReviewerAgent(run_id="r-1", task_id="t-1", agent_id="reviewer")

        # Create files totalling more than max
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("x" * 8_000)

        builder_output = {"files_written": [f"file{i}.py" for i in range(5)]}
        context = agent._read_changed_files(tmp_path, builder_output)
        assert (
            len(context.encode()) < _MAX_CODE_BYTES * 2
        )  # should be capped, with some slack for headers


# ══════════════════════════════════════════════════════════════════════════════
# SecurityAgent
# ══════════════════════════════════════════════════════════════════════════════


class TestSecurityAgent:
    def _make_agent(self):
        from phalanx.agents.security import SecurityAgent

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
            patch("phalanx.agents.security.get_db", make_db_context(session)),
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
            "scans": [
                {
                    "tool": "detect-secrets",
                    "passed": False,
                    "max_severity": "high",
                    "findings_count": 2,
                    "error": None,
                }
            ],
        }

        with (
            patch("phalanx.agents.security.get_db", make_db_context(session)),
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

        with patch(
            "phalanx.guardrails.security_pipeline.SecurityPipeline",
            side_effect=ImportError("not installed"),
        ):
            result = await agent._run_security_pipeline(tmp_path, run)

        assert result["overall_passed"] is False
        assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# ReleaseAgent
# ══════════════════════════════════════════════════════════════════════════════


class TestReleaseAgent:
    def _make_agent(self):
        from phalanx.agents.release import ReleaseAgent

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
            task_result,
            run_result,
            wo_result,
            tasks_result,
            run_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
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
            patch("phalanx.agents.release.get_db", make_db_context(session)),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.release.settings") as mock_settings,
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

        with (
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
        ):
            result = await agent._generate_release_notes(run, wo, [])

        assert result["title"] == "Release Notes: Feature X"
        assert result["changes"][0]["type"] == "feat"

    async def test_generate_release_notes_bad_json_falls_back(self):
        agent = self._make_agent()
        run = make_run()
        wo = make_work_order(title="My Feature")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not valid json")

        with (
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
        ):
            result = await agent._generate_release_notes(run, wo, [])

        assert "title" in result
        assert "My Feature" in result["title"]

    async def test_create_github_pr_skips_without_token(self):
        agent = self._make_agent()
        run = make_run(active_branch="feature/auth")
        wo = make_work_order()

        with patch("phalanx.agents.release.settings") as mock_settings:
            mock_settings.github_token = None
            result = await agent._create_github_pr(run, wo, {})

        assert result == {}

    async def test_create_github_pr_skips_without_branch(self):
        agent = self._make_agent()
        run = make_run(active_branch=None)
        wo = make_work_order()

        with patch("phalanx.agents.release.settings") as mock_settings:
            mock_settings.github_token = "ghp_token"
            result = await agent._create_github_pr(run, wo, {})

        assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# CommanderAgent
# ══════════════════════════════════════════════════════════════════════════════


class TestCommanderAgent:
    def _make_agent(self):
        from phalanx.agents.commander import CommanderAgent

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

        with patch("phalanx.db.session.get_db", make_db_context(session)):
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

        with (
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
        ):
            result = await agent._generate_task_plan(wo, "memory context")

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["agent_role"] == "builder"

    async def test_generate_task_plan_bad_json_returns_fallback(self):
        agent = self._make_agent()
        wo = make_work_order(title="Build feature")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_claude_response("not json")

        with (
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("phalanx.agents.base._claude_cli_path", None),
        ):
            result = await agent._generate_task_plan(wo, "")

        # Fallback: one builder task
        assert "tasks" in result
        assert result["tasks"][0]["agent_role"] == "builder"

    async def test_persist_task_plan_adds_tasks(self):
        agent = self._make_agent()
        session = make_session()

        plan = {
            "tasks": [
                {
                    "sequence_num": 1,
                    "title": "Task 1",
                    "description": "D1",
                    "agent_role": "builder",
                    "depends_on": [],
                    "files_likely_touched": [],
                },
                {
                    "sequence_num": 2,
                    "title": "Task 2",
                    "description": "D2",
                    "agent_role": "reviewer",
                    "depends_on": [],
                    "files_likely_touched": [],
                },
            ]
        }

        await agent._persist_task_plan(session, plan)

        assert session.add.call_count == 2  # 2 tasks, 0 dependencies (depends_on=[])
        session.flush.assert_awaited_once()
        session.commit.assert_awaited_once()

    async def test_execute_builds_slack_notifier_from_run_id(self):
        """
        SlackNotifier.from_run is called with self.run_id early in execute().
        We stop the run deliberately after the notifier is built by making
        _generate_task_plan raise — the notifier post must have already fired.
        """
        agent = self._make_agent()
        session = make_session()

        wo = make_work_order()
        wo_result = MagicMock()
        wo_result.scalar_one_or_none.return_value = wo

        # _create_or_load_run needs a run-count scalar
        run_count_result = MagicMock()
        run_count_result.scalar_one.return_value = 0

        session.execute.side_effect = [wo_result, run_count_result]

        mock_notifier = AsyncMock()
        mock_notifier.post = AsyncMock()

        with (
            patch("phalanx.db.session.get_db", make_db_context(session)),
            patch("phalanx.agents.commander.SlackNotifier") as mock_notifier_cls,
            patch.object(agent, "_transition_run", AsyncMock()),
            patch.object(agent, "_audit", AsyncMock()),
            patch.object(
                agent, "_generate_task_plan", AsyncMock(side_effect=Exception("stop-sentinel"))
            ),
            patch("phalanx.agents.commander.MemoryReader") as mock_reader_cls,
            patch("phalanx.agents.commander.MemoryAssembler") as mock_assembler_cls,
        ):
            mock_notifier_cls.from_run = AsyncMock(return_value=mock_notifier)
            mock_reader_cls.return_value.get_standing_facts = AsyncMock(return_value=[])
            mock_reader_cls.return_value.get_standing_decisions = AsyncMock(return_value=[])
            mock_assembler_cls.return_value.build = MagicMock(return_value="")

            with pytest.raises(Exception, match="stop-sentinel"):
                await agent.execute()

        # from_run called with the correct run_id
        mock_notifier_cls.from_run.assert_awaited_once_with(agent.run_id, session)

        # Planning message posted to the thread
        mock_notifier.post.assert_awaited_once()
        planning_text = mock_notifier.post.call_args[0][0]
        assert "🧠" in planning_text
        assert wo.title in planning_text

    async def test_execute_posts_run_planned_after_plan_approval(self):
        """
        notifier.run_planned() is called after the plan gate is approved,
        before EXECUTING starts. Tasks loaded from DB are passed to it.
        """
        agent = self._make_agent()
        session = make_session()

        wo = make_work_order()
        wo_result = MagicMock()
        wo_result.scalar_one_or_none.return_value = wo

        run_count_result = MagicMock()
        run_count_result.scalar_one.return_value = 0

        task_plan = {
            "tasks": [
                {
                    "sequence_num": 1,
                    "title": "T1",
                    "description": "d",
                    "agent_role": "builder",
                    "depends_on": [],
                    "files_likely_touched": [],
                },
            ]
        }

        # Tasks loaded after approval
        mock_task = make_task(agent_role="builder")
        tasks_result = MagicMock()
        tasks_result.scalars.return_value = [mock_task]

        # execute calls: wo load, run count, then tasks-after-approval SELECT
        session.execute.side_effect = [wo_result, run_count_result, tasks_result]

        mock_notifier = AsyncMock()
        mock_notifier.post = AsyncMock()
        mock_notifier.run_planned = AsyncMock()

        # Stop execute() after run_planned by making _transition_run("AWAITING_PLAN_APPROVAL", "EXECUTING") raise
        transition_calls = []

        async def _fake_transition(from_s, to_s, **kw):
            transition_calls.append((from_s, to_s))
            if from_s == "AWAITING_PLAN_APPROVAL" and to_s == "EXECUTING":
                raise Exception("stop-sentinel")

        with (
            patch("phalanx.db.session.get_db", make_db_context(session)),
            patch("phalanx.agents.commander.SlackNotifier") as mock_notifier_cls,
            patch.object(agent, "_transition_run", side_effect=_fake_transition),
            patch.object(agent, "_audit", AsyncMock()),
            patch.object(agent, "_generate_task_plan", AsyncMock(return_value=task_plan)),
            patch.object(agent, "_persist_task_plan", AsyncMock()),
            patch("phalanx.agents.commander.ApprovalGate") as mock_gate_cls,
            patch("phalanx.agents.commander.MemoryReader") as mock_reader_cls,
            patch("phalanx.agents.commander.MemoryAssembler") as mock_assembler_cls,
        ):
            mock_notifier_cls.from_run = AsyncMock(return_value=mock_notifier)
            mock_gate_cls.return_value.request_and_wait = AsyncMock()  # approved
            mock_reader_cls.return_value.get_standing_facts = AsyncMock(return_value=[])
            mock_reader_cls.return_value.get_standing_decisions = AsyncMock(return_value=[])
            mock_assembler_cls.return_value.build = MagicMock(return_value="")

            with pytest.raises(Exception, match="stop-sentinel"):
                await agent.execute()

        # run_planned called after approval with the loaded tasks
        mock_notifier.run_planned.assert_awaited_once()
        tasks_arg = mock_notifier.run_planned.call_args[0][0]
        assert mock_task in tasks_arg


class TestBuilderAgentJsonParsing:
    """Tests for BuilderAgent._parse_json_response — the robust JSON extractor."""

    def _make_agent(self):
        from phalanx.agents.builder import BuilderAgent

        return BuilderAgent(run_id="r-1", task_id="t-1", agent_id="builder")

    _VALID_PAYLOAD = {
        "summary": "Added auth module",
        "commit_message": "feat: add JWT auth",
        "files": [{"path": "auth.py", "action": "create", "content": "# auth\n"}],
    }

    def test_plain_json(self):
        agent = self._make_agent()
        raw = json.dumps(self._VALID_PAYLOAD)
        result = agent._parse_json_response(raw)
        assert result == self._VALID_PAYLOAD

    def test_json_with_markdown_fence(self):
        agent = self._make_agent()
        raw = "```json\n" + json.dumps(self._VALID_PAYLOAD) + "\n```"
        result = agent._parse_json_response(raw)
        assert result == self._VALID_PAYLOAD

    def test_json_with_plain_fence(self):
        agent = self._make_agent()
        raw = "```\n" + json.dumps(self._VALID_PAYLOAD) + "\n```"
        result = agent._parse_json_response(raw)
        assert result == self._VALID_PAYLOAD

    def test_json_with_prose_before_and_after(self):
        agent = self._make_agent()
        raw = "Sure! Here is the output:\n" + json.dumps(self._VALID_PAYLOAD) + "\nHope that helps."
        result = agent._parse_json_response(raw)
        assert result == self._VALID_PAYLOAD

    def test_json_with_braces_in_content(self):
        """File content containing { } should not confuse the brace scanner."""
        agent = self._make_agent()
        payload = {
            "summary": "Added component",
            "commit_message": "feat: component",
            "files": [
                {
                    "path": "App.tsx",
                    "action": "create",
                    "content": "function App() { return <div />; }",
                }
            ],
        }
        raw = json.dumps(payload)
        result = agent._parse_json_response(raw)
        assert result is not None
        assert result["files"][0]["content"] == "function App() { return <div />; }"

    def test_returns_none_for_empty_string(self):
        agent = self._make_agent()
        assert agent._parse_json_response("") is None

    def test_returns_none_for_no_json(self):
        agent = self._make_agent()
        assert agent._parse_json_response("No JSON here at all.") is None
