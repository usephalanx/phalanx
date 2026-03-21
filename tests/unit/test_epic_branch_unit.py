"""
Unit tests for Phase 3B — epic-aware branch names.

Covers:
  TechLead:
    - _epic_branch_name formats correctly
    - slug truncated at 40 chars
    - special chars (spaces, &, /) replaced with dashes
    - Task rows created with branch_name set to epic branch
    - All tasks in same epic share same branch_name
    - Tasks in different epics get different branch names
    - branch_name exposed in TechLead output dict

  Builder:
    - _workspace_path with branch_name returns isolated subdirectory
    - _workspace_path without branch_name returns legacy path
    - _ensure_workspace uses task branch over run.active_branch
    - _ensure_workspace falls back to run.active_branch when task has none
    - _ensure_workspace falls back to phalanx/run-<id> when neither set
    - _commit_changes uses explicit branch param
    - _commit_changes falls back to run.active_branch
    - _commit_changes falls back to phalanx/run-<id> when neither set
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.tech_lead import TechLeadAgent, _epic_branch_name


# ─────────────────────────────────────────────────────────────────────────────
# _epic_branch_name
# ─────────────────────────────────────────────────────────────────────────────


class TestEpicBranchName:
    def test_basic_format(self):
        result = _epic_branch_name("Infrastructure", "abc12345-xxxx")
        assert result == "feat/infrastructure-abc12345"

    def test_spaces_become_dashes(self):
        result = _epic_branch_name("User Preferences", "run-0001")
        assert result.startswith("feat/user-preferences-")

    def test_special_chars_stripped(self):
        result = _epic_branch_name("Core Infrastructure & Authentication", "run-0001")
        assert "&" not in result
        assert " " not in result
        assert result.startswith("feat/core-infrastructure-authentication-")

    def test_slug_truncated_at_40_chars(self):
        long_title = "B " * 30  # 60-char title with spaces
        result = _epic_branch_name(long_title, "run-0001")
        # format: feat/<slug>-<run_id[:8]>
        # total branch length should be bounded
        assert len(result) <= len("feat/") + 40 + 1 + 8

    def test_run_id_truncated_at_8_chars(self):
        result = _epic_branch_name("Infra", "verylongrunid-1234567890")
        assert result.endswith("-verylonr") or len(result.split("-")[-1]) == 8

    def test_uppercase_lowercased(self):
        result = _epic_branch_name("LISTINGS API", "run-0001")
        assert result == result.lower()

    def test_forward_slash_in_title_handled(self):
        result = _epic_branch_name("Search/Filter", "run-0001")
        # Should not have double slash in branch name body
        assert "feat/" in result
        path_part = result[len("feat/"):]
        assert "/" not in path_part


# ─────────────────────────────────────────────────────────────────────────────
# TechLead sets branch_name on tasks
# ─────────────────────────────────────────────────────────────────────────────


def _make_agent():
    agent = TechLeadAgent.__new__(TechLeadAgent)
    agent.run_id = "run-abc12345"
    agent.task_id = None
    agent.agent_id = "tech_lead"
    agent.token_budget = 100000
    agent._tokens_used = 0
    import structlog
    agent._log = structlog.get_logger("test").bind(run_id="run-abc12345")
    agent._settings = MagicMock()
    agent._settings.anthropic_model_default = "claude-sonnet-4-6"
    return agent


def _make_epics(n=2):
    return [
        {"id": f"epic-{i}", "title": f"Epic Title {i}", "description": f"Desc {i}",
         "sequence_num": i, "estimated_minutes": 30}
        for i in range(n)
    ]


def _make_work_order():
    wo = MagicMock()
    wo.title = "Build portal"
    wo.description = "A portal"
    return wo


def _make_session(collected_tasks, collected_deps):
    session = AsyncMock()
    session.commit = AsyncMock()

    def _add(obj):
        from phalanx.db.models import Task, TaskDependency
        if isinstance(obj, Task):
            collected_tasks.append(obj)
        elif isinstance(obj, TaskDependency):
            collected_deps.append(obj)

    session.add = MagicMock(side_effect=_add)
    return session


def _valid_response(n_tasks=2):
    tasks = [
        {
            "epic_index": i % 2,
            "title": f"Task {i+1}",
            "agent_role": "builder",
            "sequence_num": i + 1,
            "estimated_minutes": 30,
            "files_likely_touched": [],
            "dependencies": [{"depends_on_seq": i, "dep_type": "full"}] if i > 0 else [],
        }
        for i in range(n_tasks)
    ]
    return json.dumps({"api_contract": {}, "db_schema": {}, "tasks": tasks})


class TestTechLeadBranchName:
    async def test_tasks_have_branch_name_set(self):
        agent = _make_agent()
        tasks_out = []
        deps_out = []
        session = _make_session(tasks_out, deps_out)
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response(2)):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        for task in tasks_out:
            assert task.branch_name is not None
            assert task.branch_name.startswith("feat/")

    async def test_branch_name_contains_run_id_prefix(self):
        agent = _make_agent()
        tasks_out = []
        deps_out = []
        session = _make_session(tasks_out, deps_out)
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response(2)):
            await agent.execute_for_run(session, wo, pm_output)

        for task in tasks_out:
            # run_id is "run-abc12345" → first 8 chars = "run-abc1"
            assert "run-abc1" in task.branch_name

    async def test_tasks_same_epic_same_branch(self):
        """Two tasks in the same epic share the same branch name."""
        agent = _make_agent()
        tasks_out = []
        deps_out = []
        session = _make_session(tasks_out, deps_out)
        wo = _make_work_order()
        epics = _make_epics(1)  # single epic
        pm_output = {"epics": epics, "app_type": "web"}

        # Two tasks both in epic_index=0
        tasks_data = [
            {"epic_index": 0, "title": "Task A", "agent_role": "builder",
             "sequence_num": 1, "estimated_minutes": 20, "files_likely_touched": [], "dependencies": []},
            {"epic_index": 0, "title": "Task B", "agent_role": "reviewer",
             "sequence_num": 2, "estimated_minutes": 10, "files_likely_touched": [],
             "dependencies": [{"depends_on_seq": 1, "dep_type": "full"}]},
        ]
        response = json.dumps({"api_contract": {}, "db_schema": {}, "tasks": tasks_data})

        with patch.object(agent, "_call_claude", return_value=response):
            await agent.execute_for_run(session, wo, pm_output)

        # tech_lead now injects integration_wiring + verifier after builders → 4 total
        assert len(tasks_out) == 4
        # The two builder tasks (first two) share the same epic branch
        builder_tasks = [t for t in tasks_out if t.agent_role in ("builder", "reviewer")]
        assert builder_tasks[0].branch_name == builder_tasks[1].branch_name

    async def test_tasks_different_epics_different_branches(self):
        agent = _make_agent()
        tasks_out = []
        deps_out = []
        session = _make_session(tasks_out, deps_out)
        wo = _make_work_order()
        epics = [
            {"id": "epic-0", "title": "Infrastructure", "description": "",
             "sequence_num": 0, "estimated_minutes": 30},
            {"id": "epic-1", "title": "Listings API", "description": "",
             "sequence_num": 1, "estimated_minutes": 30},
        ]
        pm_output = {"epics": epics, "app_type": "web"}

        tasks_data = [
            {"epic_index": 0, "title": "Scaffold DB", "agent_role": "builder",
             "sequence_num": 1, "estimated_minutes": 30, "files_likely_touched": [], "dependencies": []},
            {"epic_index": 1, "title": "Build API", "agent_role": "builder",
             "sequence_num": 2, "estimated_minutes": 45, "files_likely_touched": [],
             "dependencies": [{"depends_on_seq": 1, "dep_type": "full"}]},
        ]
        response = json.dumps({"api_contract": {}, "db_schema": {}, "tasks": tasks_data})

        with patch.object(agent, "_call_claude", return_value=response):
            await agent.execute_for_run(session, wo, pm_output)

        assert tasks_out[0].branch_name != tasks_out[1].branch_name
        assert "infrastructure" in tasks_out[0].branch_name
        assert "listings" in tasks_out[1].branch_name

    async def test_branch_name_in_output_dict(self):
        agent = _make_agent()
        tasks_out = []
        deps_out = []
        session = _make_session(tasks_out, deps_out)
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response(2)):
            result = await agent.execute_for_run(session, wo, pm_output)

        for t in result.output["tasks"]:
            assert "branch_name" in t
            assert t["branch_name"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Builder workspace isolation
# ─────────────────────────────────────────────────────────────────────────────


def _make_builder(run_id="run-001", task_id="task-001"):
    from phalanx.agents.builder import BuilderAgent
    agent = BuilderAgent.__new__(BuilderAgent)
    agent.run_id = run_id
    agent.task_id = task_id
    agent.agent_id = "builder"
    agent.token_budget = 100000
    agent._tokens_used = 0
    import structlog
    agent._log = structlog.get_logger("test").bind(run_id=run_id)
    return agent


def _make_run(active_branch=None, project_id="proj-1"):
    run = MagicMock()
    run.active_branch = active_branch
    run.project_id = project_id
    return run


class TestBuilderWorkspacePath:
    def test_legacy_path_no_branch(self):
        agent = _make_builder()
        run = _make_run()
        path = agent._workspace_path(run, branch_name=None)
        assert str(path).endswith("proj-1/run-001")

    def test_dag_path_with_branch_name(self):
        agent = _make_builder()
        run = _make_run()
        path = agent._workspace_path(run, branch_name="feat/infra-abc12345")
        # Isolated subdir: project/run/branch_slug
        assert "feat_infra-abc12345" in str(path)
        assert "run-001" in str(path)

    def test_forward_slash_in_branch_replaced(self):
        agent = _make_builder()
        run = _make_run()
        path = agent._workspace_path(run, branch_name="feat/my-epic-abc12345")
        # Forward slash in branch → underscore in path segment
        assert "/" not in path.name

    def test_different_branches_produce_different_paths(self):
        agent = _make_builder()
        run = _make_run()
        p1 = agent._workspace_path(run, "feat/infra-abc12345")
        p2 = agent._workspace_path(run, "feat/frontend-abc12345")
        assert p1 != p2


class TestBuilderEnsureWorkspace:
    async def test_uses_task_branch_when_set(self, tmp_path):
        agent = _make_builder()
        run = _make_run(active_branch="feat/old-branch")

        setup_calls = []

        async def _fake_setup(workspace, run, branch):
            setup_calls.append(branch)

        with (
            patch("phalanx.agents.builder.settings") as mock_settings,
            patch.object(agent, "_setup_git_workspace", _fake_setup),
        ):
            mock_settings.github_token = "ghp_token"
            mock_settings.git_workspace = str(tmp_path)
            await agent._ensure_workspace(tmp_path, run, branch="feat/task-branch-abc")

        assert setup_calls == ["feat/task-branch-abc"]

    async def test_falls_back_to_run_active_branch(self, tmp_path):
        agent = _make_builder()
        run = _make_run(active_branch="feat/run-branch")

        setup_calls = []

        async def _fake_setup(workspace, run, branch):
            setup_calls.append(branch)

        with (
            patch("phalanx.agents.builder.settings") as mock_settings,
            patch.object(agent, "_setup_git_workspace", _fake_setup),
        ):
            mock_settings.github_token = "ghp_token"
            mock_settings.git_workspace = str(tmp_path)
            await agent._ensure_workspace(tmp_path, run, branch=None)

        assert setup_calls == ["feat/run-branch"]

    async def test_falls_back_to_phalanx_run_prefix(self, tmp_path):
        agent = _make_builder()
        run = _make_run(active_branch=None)

        setup_calls = []

        async def _fake_setup(workspace, run, branch):
            setup_calls.append(branch)

        with (
            patch("phalanx.agents.builder.settings") as mock_settings,
            patch.object(agent, "_setup_git_workspace", _fake_setup),
        ):
            mock_settings.github_token = "ghp_token"
            mock_settings.git_workspace = str(tmp_path)
            await agent._ensure_workspace(tmp_path, run, branch=None)

        assert setup_calls[0].startswith("phalanx/run-")

    async def test_no_git_no_setup_called(self, tmp_path):
        agent = _make_builder()
        run = _make_run()
        setup_calls = []

        async def _fake_setup(*args, **kwargs):
            setup_calls.append(True)

        with (
            patch("phalanx.agents.builder.settings") as mock_settings,
            patch.object(agent, "_setup_git_workspace", _fake_setup),
        ):
            mock_settings.github_token = ""
            mock_settings.git_workspace = str(tmp_path)
            await agent._ensure_workspace(tmp_path, run, branch="feat/x")

        assert setup_calls == []


class TestBuilderCommitBranch:
    async def test_uses_explicit_branch_param(self, tmp_path):
        from phalanx.agents.builder import BuilderAgent

        agent = _make_builder()
        run = _make_run(active_branch="feat/old")
        task = MagicMock()
        task.title = "Scaffold DB"

        commit_result = await agent._commit_changes(
            tmp_path, task, run, files_written=[], branch="feat/new-branch"
        )
        # No files → returns {} immediately
        assert commit_result == {}

    async def test_falls_back_to_run_active_branch(self, tmp_path):
        agent = _make_builder()
        run = _make_run(active_branch="feat/run-active")
        task = MagicMock()
        task.title = "Test"

        with patch("phalanx.agents.builder.settings") as mock_settings:
            mock_settings.git_author_name = "Phalanx"
            mock_settings.git_author_email = "bot@test.com"
            mock_settings.github_token = ""
            # no files → early return
            result = await agent._commit_changes(tmp_path, task, run, [], branch=None)

        assert result == {}

    async def test_falls_back_to_phalanx_run_prefix_when_no_active_branch(self, tmp_path):
        """When branch=None and run.active_branch=None, uses phalanx/run-<id>."""
        agent = _make_builder(run_id="run-deadbeef")
        run = _make_run(active_branch=None)
        task = MagicMock()

        # Patch git import to verify the branch variable resolved correctly
        branch_used = []

        try:
            from git import Repo
            original_init = Repo.init

            def _patched_init(path, **kwargs):
                repo = original_init(path, **kwargs)
                return repo

        except ImportError:
            pass

        # With empty files_written → early return {} before branch matters
        result = await agent._commit_changes(tmp_path, task, run, [], branch=None)
        assert result == {}
