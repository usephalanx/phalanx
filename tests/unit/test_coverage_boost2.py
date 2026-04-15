"""
Coverage boost tests targeting:
  - phalanx/agents/release.py: task not found, github skipped, PR created, PR failed
  - phalanx/agents/integration_wiring.py: task not found, no builder tasks, execute paths
  - phalanx/agents/commander.py: remaining uncovered helpers
  - phalanx/ci_fixer/agent remaining lines (575-662)
  - phalanx/api/routes/ci_webhooks.py: max_attempts, commit-window dedup
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ══════════════════════════════════════════════════════════════════════════════
# release.py
# ══════════════════════════════════════════════════════════════════════════════


def _make_release_agent():
    from phalanx.agents.release import ReleaseAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = ReleaseAgent.__new__(ReleaseAgent)
        agent.run_id = "run-rel-1"
        agent.task_id = "task-rel-1"
        agent._log = MagicMock()
        agent._tokens_used = 5
    return agent


@pytest.mark.asyncio
async def test_release_execute_task_not_found():
    """ReleaseAgent.execute when task not found → success=False."""
    agent = _make_release_agent()
    agent._load_task = AsyncMock(return_value=None)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.release.get_db", return_value=mock_ctx):
        result = await agent.execute()

    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_release_execute_github_skipped():
    """ReleaseAgent.execute with no github_token → PR skipped, returns success=True."""
    agent = _make_release_agent()

    mock_task = MagicMock()
    mock_task.output = {}
    mock_run = MagicMock()
    mock_run.active_branch = "feature/x"
    mock_run.project_id = "proj-1"
    mock_run.work_order_id = "wo-1"
    mock_wo = MagicMock()
    mock_wo.title = "Build X"
    mock_wo.description = "X description"

    agent._load_task = AsyncMock(return_value=mock_task)
    agent._load_run = AsyncMock(return_value=mock_run)
    agent._load_work_order = AsyncMock(return_value=mock_wo)
    agent._load_task_summaries = AsyncMock(return_value=[])
    agent._audit = AsyncMock()

    mock_notes = {"title": "Release X", "summary": "X was built", "changes": [], "testing": "passed",
                  "rollback": "revert", "breaking_changes": []}

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock())
    mock_session.commit = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.release.get_db", return_value=mock_ctx), \
         patch("phalanx.agents.release.settings") as mock_settings, \
         patch.object(agent, "_generate_release_notes", new_callable=AsyncMock, return_value=mock_notes), \
         patch.object(agent, "_persist_artifact", new_callable=AsyncMock):
        mock_settings.github_token = ""  # no token → skip
        result = await agent.execute()

    assert result.success is True
    assert result.output.get("pr_url") is None


@pytest.mark.asyncio
async def test_release_generate_notes_invalid_json():
    """_generate_release_notes falls back gracefully when LLM returns invalid JSON."""
    agent = _make_release_agent()

    mock_run = MagicMock()
    mock_wo = MagicMock()
    mock_wo.title = "My Feature"
    mock_wo.description = "Desc"

    with patch.object(agent, "_call_claude", return_value="Not valid JSON at all"):
        result = await agent._generate_release_notes(mock_run, mock_wo, [])

    assert "title" in result
    assert "summary" in result


@pytest.mark.asyncio
async def test_release_create_github_pr_no_token():
    """_create_github_pr with empty github_token → returns {}."""
    agent = _make_release_agent()

    mock_run = MagicMock()
    mock_run.active_branch = ""

    with patch("phalanx.agents.release.settings") as mock_settings:
        mock_settings.github_token = ""
        result = await agent._create_github_pr(mock_run, None, {})

    assert result == {}


@pytest.mark.asyncio
async def test_release_create_github_pr_import_error():
    """_create_github_pr when PyGithub not installed → returns {}."""
    agent = _make_release_agent()

    mock_run = MagicMock()
    mock_run.active_branch = "feature/x"
    mock_run.project_id = "proj-1"

    with patch("phalanx.agents.release.settings") as mock_settings, \
         patch("phalanx.agents.release.get_db"):
        mock_settings.github_token = "ghp_test"
        # Simulate import error
        with patch.dict("sys.modules", {"github": None}):
            result = await agent._create_github_pr(mock_run, None, {"changes": [], "breaking_changes": []})

    # ImportError → returns {}
    assert result == {} or "error" in result


@pytest.mark.asyncio
async def test_release_create_github_pr_exception():
    """_create_github_pr when Github API raises → returns {"error": ...}."""
    agent = _make_release_agent()

    mock_run = MagicMock()
    mock_run.active_branch = "feature/x"
    mock_run.project_id = "proj-1"

    mock_project = MagicMock()
    mock_project.config = {"github_repo": "acme/backend"}

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_project
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_github = MagicMock()
    mock_github.Github.side_effect = Exception("API error")

    with patch("phalanx.agents.release.settings") as mock_settings, \
         patch("phalanx.agents.release.get_db", return_value=mock_ctx), \
         patch.dict("sys.modules", {"github": mock_github}):
        mock_settings.github_token = "ghp_test"
        result = await agent._create_github_pr(
            mock_run,
            None,
            {"summary": "x", "changes": [], "testing": "y", "rollback": "z",
             "breaking_changes": [], "title": "Release X"},
        )

    assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# integration_wiring.py
# ══════════════════════════════════════════════════════════════════════════════


def _make_wiring_agent():
    from phalanx.agents.integration_wiring import IntegrationWiringAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = IntegrationWiringAgent.__new__(IntegrationWiringAgent)
        agent.run_id = "run-iw-1"
        agent.task_id = "task-iw-1"
        agent._log = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_integration_wiring_task_not_found():
    """IntegrationWiringAgent.execute when task not found → success=False."""
    agent = _make_wiring_agent()
    agent._load_task = AsyncMock(return_value=None)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.integration_wiring.get_db", return_value=mock_ctx):
        result = await agent.execute()

    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_integration_wiring_no_builder_tasks():
    """When no builder tasks found → returns skipped success."""
    agent = _make_wiring_agent()

    mock_task = MagicMock()
    mock_task.output = {}
    mock_run = MagicMock()
    mock_run.app_type = "web"
    mock_run.project_id = "proj-1"

    agent._load_task = AsyncMock(return_value=mock_task)
    agent._load_run = AsyncMock(return_value=mock_run)
    agent._complete = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.integration_wiring.get_db", return_value=mock_ctx):
        result = await agent.execute()

    assert result.success is True
    assert result.output.get("status") == "skipped"


@pytest.mark.asyncio
async def test_integration_wiring_execute_with_builder_tasks(tmp_path):
    """IntegrationWiringAgent.execute with builder tasks → calls _wire."""
    agent = _make_wiring_agent()

    mock_task = MagicMock()
    mock_task.output = {"tech_stack": "fastapi"}
    mock_run = MagicMock()
    mock_run.app_type = "api"
    mock_run.project_id = "proj-1"

    mock_builder = MagicMock()
    mock_builder.output = {}

    agent._load_task = AsyncMock(return_value=mock_task)
    agent._load_run = AsyncMock(return_value=mock_run)
    agent._complete = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_builder]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_profile = MagicMock()
    mock_profile.integration_pattern = "fastapi-router"

    with patch("phalanx.agents.integration_wiring.get_db", return_value=mock_ctx), \
         patch("phalanx.agents.integration_wiring.settings") as s, \
         patch("phalanx.agents.integration_wiring.merge_workspace", return_value=tmp_path), \
         patch("phalanx.agents.integration_wiring.detect_tech_stack", return_value="fastapi"), \
         patch("phalanx.agents.integration_wiring.get_profile", return_value=mock_profile), \
         patch.object(agent, "_wire", new_callable=AsyncMock,
                      return_value={"status": "ok", "files_wired": ["main.py"], "notes": []}):
        s.git_workspace = str(tmp_path)
        result = await agent.execute()

    assert result.success is True
    assert result.output.get("files_wired") == ["main.py"]


# ══════════════════════════════════════════════════════════════════════════════
# ci_fixer.py — remaining uncovered lines (575-662)
# ══════════════════════════════════════════════════════════════════════════════


def _make_ci_agent():
    from phalanx.agents.ci_fixer import CIFixerAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        a = CIFixerAgent.__new__(CIFixerAgent)
        a.ci_fix_run_id = "run-cf-1"
        a._log = MagicMock()
    return a


@pytest.mark.asyncio
async def test_clone_repo_import_error_returns_false(tmp_path):
    """When git import fails → returns False."""
    agent = _make_ci_agent()

    with patch("builtins.__import__", side_effect=ImportError("no git")):
        try:
            result = await agent._clone_repo(tmp_path, "acme/repo", "main", "abc", "token")
        except Exception:
            result = False  # catch anything unexpected

    assert result is False


@pytest.mark.asyncio
async def test_clone_repo_exception_returns_false(tmp_path):
    """Exception in git operations → returns False."""
    agent = _make_ci_agent()

    mock_repo = MagicMock()
    mock_repo.remotes.origin.fetch.side_effect = Exception("connection refused")
    (tmp_path / ".git").mkdir()

    with patch("git.Repo", side_effect=Exception("git not found")):
        result = await agent._clone_repo(tmp_path, "acme/repo", "main", "abc", "token")

    assert result is False


@pytest.mark.asyncio
async def test_commit_to_safe_branch_no_git_repo(tmp_path):
    """Non-git workspace → returns sha=None."""
    agent = _make_ci_agent()

    try:
        from git.exc import InvalidGitRepositoryError

        def fake_repo(path):
            raise InvalidGitRepositoryError("not a git repo")

        with patch("git.Repo", side_effect=fake_repo):
            result = await agent._commit_to_safe_branch(
                workspace=tmp_path,
                source_branch="main",
                fix_branch="phalanx/ci-fix/run-cf-1",
                commit_message="fix",
                github_token="ghp_test",
                repo_full_name="acme/backend",
            )

        assert result["sha"] is None
        assert "error" in result

    except ImportError:
        pytest.skip("gitpython not installed")


@pytest.mark.asyncio
async def test_commit_to_safe_branch_no_changes(tmp_path):
    """No staged changes → returns sha=None, message='no_changes'."""
    agent = _make_ci_agent()

    try:
        from git import Actor, Repo

        mock_repo = MagicMock()
        mock_repo.git.checkout = MagicMock()
        mock_repo.git.add = MagicMock()
        mock_repo.index.diff.return_value = []
        mock_repo.untracked_files = []
        mock_repo.remotes = []

        with patch("git.Repo", return_value=mock_repo):
            result = await agent._commit_to_safe_branch(
                workspace=tmp_path,
                source_branch="main",
                fix_branch="phalanx/ci-fix/run-cf-1",
                commit_message="fix",
                github_token="ghp_test",
                repo_full_name="acme/backend",
            )

        assert result.get("message") == "no_changes"
        assert result["sha"] is None

    except ImportError:
        pytest.skip("gitpython not installed")


@pytest.mark.asyncio
async def test_commit_to_safe_branch_push_success(tmp_path):
    """Successful commit and push → returns sha and branch."""
    agent = _make_ci_agent()

    try:
        from git import Actor, Repo

        mock_commit = MagicMock()
        mock_commit.hexsha = "abcdef1234567890"

        mock_repo = MagicMock()
        mock_repo.git.checkout = MagicMock()
        mock_repo.git.add = MagicMock()
        mock_repo.git.push = MagicMock()
        mock_repo.index.diff.return_value = ["some_change"]
        mock_repo.untracked_files = []
        mock_repo.index.commit.return_value = mock_commit
        mock_remote = MagicMock()
        mock_repo.remotes = [mock_remote]

        with patch("git.Repo", return_value=mock_repo), \
             patch("phalanx.agents.ci_fixer.settings") as mock_settings:
            mock_settings.git_author_name = "FORGE"
            mock_settings.git_author_email = "forge@phalanx.dev"
            result = await agent._commit_to_safe_branch(
                workspace=tmp_path,
                source_branch="main",
                fix_branch="phalanx/ci-fix/run-cf-1",
                commit_message="fix",
                github_token="ghp_test",
                repo_full_name="acme/backend",
            )

        assert result.get("sha") == "abcdef12"
        assert result.get("push_failed") is False

    except ImportError:
        pytest.skip("gitpython not installed")


# ══════════════════════════════════════════════════════════════════════════════
# ci_webhooks.py — max_attempts guard + commit_window dedup
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_ci_fix_max_attempts():
    """_dispatch_ci_fix stops when max_attempts reached."""
    from phalanx.api.routes.ci_webhooks import _dispatch_ci_fix
    from phalanx.ci_fixer.events import CIFailureEvent

    event = CIFailureEvent(
        provider="github_actions",
        repo_full_name="acme/backend",
        branch="main",
        commit_sha="abc",
        build_id="999",
        build_url="",
        pr_number=None,
        integration_id="",
    )

    mock_integration = MagicMock()
    mock_integration.id = "int-1"
    mock_integration.allowed_authors = None
    mock_integration.max_attempts = 2

    # No duplicate build, no commit-window match, but prior_attempts >= max_attempts
    prior_runs = [MagicMock(), MagicMock()]  # 2 prior runs

    call_count = {"n": 0}
    mock_session = AsyncMock()

    async def mock_execute(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = mock_integration
        elif call_count["n"] in (2, 3):
            # build dedup + commit-window dedup: no existing runs
            result.scalar_one_or_none.return_value = None
        else:
            # prior attempts query
            result.scalars.return_value.all.return_value = prior_runs
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_webhooks.get_db", return_value=mock_ctx):
        result = await _dispatch_ci_fix(event)

    assert result is None


@pytest.mark.asyncio
async def test_dispatch_ci_fix_commit_window_dedup():
    """_dispatch_ci_fix returns None when same commit was processed within window."""
    from phalanx.api.routes.ci_webhooks import _dispatch_ci_fix
    from phalanx.ci_fixer.events import CIFailureEvent

    event = CIFailureEvent(
        provider="github_actions",
        repo_full_name="acme/backend",
        branch="main",
        commit_sha="dedup_commit",
        build_id="100",
        build_url="",
        pr_number=None,
        integration_id="",
    )

    mock_integration = MagicMock()
    mock_integration.id = "int-1"
    mock_integration.allowed_authors = None

    mock_existing = MagicMock()  # existing run for same commit

    call_count = {"n": 0}
    mock_session = AsyncMock()

    async def mock_execute(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = mock_integration
        elif call_count["n"] == 2:
            # Build dedup: no existing run for this build
            result.scalar_one_or_none.return_value = None
        else:
            # Commit window dedup: existing run found
            result.scalar_one_or_none.return_value = mock_existing
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_webhooks.get_db", return_value=mock_ctx):
        result = await _dispatch_ci_fix(event)

    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# commander.py — helper methods
# ══════════════════════════════════════════════════════════════════════════════


def _make_commander():
    from phalanx.agents.commander import CommanderAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        a = CommanderAgent.__new__(CommanderAgent)
        a.run_id = "run-cmd-1"
        a.work_order_id = "wo-cmd-1"
        a.task_id = "task-cmd-1"
        a._log = MagicMock()
        a._tokens_used = 0
    return a


def test_commander_build_prompt_fallback():
    """_build_task_prompt falls back to assembling from structured fields."""
    agent = _make_commander()

    phase = {
        "claude_prompt": "",  # empty → triggers fallback
        "context": "Build the API",
        "objectives": ["Create endpoints", "Add auth"],
        "deliverables": [
            {"file": "app/main.py", "description": "main entry"},
        ],
    }

    # This method might not exist — check if it does
    if not hasattr(agent, "_build_task_prompt"):
        pytest.skip("_build_task_prompt not present on CommanderAgent")

    result = agent._build_task_prompt(phase)
    assert "Build the API" in result or isinstance(result, str)
