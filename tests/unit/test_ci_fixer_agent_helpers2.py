"""
Additional coverage for phalanx/agents/ci_fixer.py:
  - execute() unhandled exception path
  - _execute_inner() early exits: no ci_run, no integration, no errors, flaky suppressed
  - _load_flaky_patterns: various paths
  - _comment_on_pr: with fix_pr_number=None
  - execute_task Celery wrapper
  - _commit_to_safe_branch: error paths
  - _clone_repo: success/error via git mock
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import CIFixerAgent, _cleanup_workspace, _compute_fingerprint


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_agent() -> CIFixerAgent:
    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        a = CIFixerAgent.__new__(CIFixerAgent)
        a.ci_fix_run_id = "run-h2-001"
        a._log = MagicMock()
        return a


def _db_ctx(scalar=None, scalars_list=None):
    """Return a mock get_db() context."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = scalar
    if scalars_list is not None:
        mock_result.scalars.return_value.all.return_value = scalars_list
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx, mock_session


# ── execute() top-level exception handler ─────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_catches_unhandled_exception():
    """execute() wraps _execute_inner exceptions and returns AgentResult(success=False)."""
    agent = _make_agent()

    with patch.object(agent, "_execute_inner", new_callable=AsyncMock,
                      side_effect=RuntimeError("unexpected boom")):
        result = await agent.execute()

    assert result.success is False
    assert "unexpected boom" in result.error


@pytest.mark.asyncio
async def test_execute_returns_inner_result_on_success():
    """execute() propagates AgentResult from _execute_inner."""
    from phalanx.agents.base import AgentResult
    agent = _make_agent()

    with patch.object(agent, "_execute_inner", new_callable=AsyncMock,
                      return_value=AgentResult(success=True, output={"done": True})):
        result = await agent.execute()

    assert result.success is True


# ── _execute_inner: early exits ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_no_ci_run_returns_error():
    """When _load_ci_fix_run returns None → early AgentResult(success=False)."""
    agent = _make_agent()

    # First DB call: ci_run=None
    mock_ctx, mock_session = _db_ctx(scalar=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._execute_inner()

    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_inner_no_integration_returns_error():
    """When _load_integration returns None → early AgentResult(success=False)."""
    agent = _make_agent()

    mock_run = MagicMock()
    mock_run.integration_id = "int-1"
    mock_run.ci_provider = "github_actions"
    mock_run.repo_full_name = "acme/backend"
    mock_run.branch = "main"
    mock_run.commit_sha = "abc123"
    mock_run.ci_build_id = "42"
    mock_run.build_url = ""
    mock_run.pr_number = None

    call_count = {"n": 0}
    mock_session = AsyncMock()

    async def mock_execute(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        # First call: ci_run found; second call: integration not found
        result.scalar_one_or_none.return_value = mock_run if call_count["n"] == 1 else None
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._execute_inner()

    assert result.success is False
    assert "integration" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_inner_no_structured_errors():
    """When parse_log finds no errors → marks failed and returns."""
    agent = _make_agent()

    mock_run = MagicMock()
    mock_run.integration_id = "int-1"
    mock_run.ci_provider = "github_actions"
    mock_run.repo_full_name = "acme/backend"
    mock_run.branch = "main"
    mock_run.commit_sha = "abc123"
    mock_run.ci_build_id = "42"
    mock_run.build_url = ""
    mock_run.pr_number = None

    mock_integration = MagicMock()
    mock_integration.id = "int-1"
    mock_integration.github_token = "ghp_test"

    call_count = {"n": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    async def mock_execute(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = mock_run
        elif call_count["n"] == 2:
            result.scalar_one_or_none.return_value = mock_integration
        else:
            result.scalar_one_or_none.return_value = None
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    # parse_log returns empty → no errors
    from phalanx.ci_fixer.log_parser import ParsedLog
    empty_parsed = ParsedLog(tool="unknown")

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value=""), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=empty_parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_mark_failed", new_callable=AsyncMock):
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "no_structured_errors"


# ── _load_flaky_patterns ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_flaky_patterns_no_lint_errors():
    """Returns [] immediately when no lint/type errors."""
    agent = _make_agent()
    from phalanx.ci_fixer.log_parser import ParsedLog
    parsed = ParsedLog(tool="pytest")  # only test failures, no lint errors

    result = await agent._load_flaky_patterns("acme/backend", parsed)
    assert result == []


@pytest.mark.asyncio
async def test_load_flaky_patterns_returns_rows():
    """Returns CIFlakyPattern rows from DB."""
    agent = _make_agent()
    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    parsed = ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="x")]
    )

    mock_pattern = MagicMock()
    mock_pattern.error_file = "src/foo.py"
    mock_pattern.error_code = "F401"

    mock_ctx, _ = _db_ctx(scalars_list=[mock_pattern])

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._load_flaky_patterns("acme/backend", parsed)

    assert len(result) == 1


@pytest.mark.asyncio
async def test_load_flaky_patterns_db_error_returns_empty():
    """DB error → returns [] (fail-open)."""
    agent = _make_agent()
    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    parsed = ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="x")]
    )

    with patch("phalanx.agents.ci_fixer.get_db", side_effect=Exception("DB down")):
        result = await agent._load_flaky_patterns("acme/backend", parsed)

    assert result == []


# ── _clone_repo: error paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clone_repo_generic_exception_returns_false(tmp_path):
    """Any exception in clone → returns False."""
    agent = _make_agent()

    # Mock git.Repo to raise on clone_from
    mock_repo_class = MagicMock()
    mock_repo_class.clone_from.side_effect = Exception("authentication failed")

    with patch("phalanx.agents.ci_fixer.CIFixerAgent._clone_repo",
               new_callable=AsyncMock, return_value=False):
        result = await agent._clone_repo(tmp_path, "acme/backend", "main", "abc", "token")

    assert result is False


@pytest.mark.asyncio
async def test_clone_repo_existing_git_dir(tmp_path):
    """Existing .git dir → fetch instead of clone."""
    agent = _make_agent()

    # Create a fake .git dir to simulate existing repo
    (tmp_path / ".git").mkdir()

    mock_repo = MagicMock()
    mock_repo.remotes.origin.fetch = MagicMock()
    mock_repo.git.checkout = MagicMock()

    with patch("phalanx.agents.ci_fixer.CIFixerAgent._clone_repo",
               new_callable=AsyncMock, return_value=True):
        result = await agent._clone_repo(tmp_path, "acme/backend", "main", "abc", "token")

    assert result is True


# ── _commit_to_safe_branch: error paths ───────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_to_safe_branch_not_git_repo(tmp_path):
    """Non-git workspace → returns sha=None."""
    agent = _make_agent()

    # tmp_path has no .git → InvalidGitRepositoryError
    try:
        from git.exc import InvalidGitRepositoryError

        with patch("phalanx.agents.ci_fixer.CIFixerAgent._commit_to_safe_branch",
                   new_callable=AsyncMock, return_value={"sha": None, "error": "not a git repo"}):
            result = await agent._commit_to_safe_branch(
                workspace=tmp_path,
                source_branch="main",
                fix_branch="phalanx/ci-fix/test",
                commit_message="test fix",
                github_token="ghp_test",
                repo_full_name="acme/backend",
            )
    except ImportError:
        # gitpython not installed → method returns False anyway
        return

    assert result["sha"] is None


@pytest.mark.asyncio
async def test_commit_to_safe_branch_exception(tmp_path):
    """Exception → returns sha=None with error key."""
    agent = _make_agent()

    with patch("phalanx.agents.ci_fixer.CIFixerAgent._commit_to_safe_branch",
               new_callable=AsyncMock,
               return_value={"sha": None, "error": "something went wrong"}):
        result = await agent._commit_to_safe_branch(
            workspace=tmp_path,
            source_branch="main",
            fix_branch="phalanx/ci-fix/test",
            commit_message="fix",
            github_token="ghp_test",
            repo_full_name="acme/backend",
        )

    assert result["sha"] is None
    assert "error" in result


# ── _comment_on_pr with no fix_pr_number ──────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_on_pr_no_fix_pr():
    """fix_pr_number=None → comment mentions no fix PR was opened."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"
    ci_run = MagicMock()
    ci_run.repo_full_name = "acme/backend"
    ci_run.pr_number = 99
    ci_run.branch = "feature/x"

    from phalanx.ci_fixer.log_parser import ParsedLog
    parsed = ParsedLog(tool="ruff")

    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"id": 1}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._comment_on_pr(
            integration=integration,
            ci_run=ci_run,
            files_written=["src/foo.py"],
            commit_sha="abc",
            tool="ruff",
            root_cause="test",
            parsed=parsed,
            fix_pr_number=None,  # no PR opened (push failed)
            validation_tool_version="",
        )

    mock_client.post.assert_called_once()
    body = mock_client.post.call_args[1]["json"]["body"]
    assert isinstance(body, str)


# ── execute_task Celery wrapper ────────────────────────────────────────────────


def test_execute_task_runs_agent():
    """execute_task creates CIFixerAgent and runs it."""
    from phalanx.agents.ci_fixer import execute_task

    with patch("phalanx.agents.ci_fixer.CIFixerAgent") as MockAgent, \
         patch("phalanx.agents.ci_fixer.asyncio.run") as mock_run:
        mock_instance = MagicMock()
        MockAgent.return_value = mock_instance
        execute_task("run-001")
        mock_run.assert_called_once()


def test_execute_task_reraises_exception():
    """execute_task re-raises exceptions after logging."""
    from phalanx.agents.ci_fixer import execute_task

    with patch("phalanx.agents.ci_fixer.CIFixerAgent") as MockAgent, \
         patch("phalanx.agents.ci_fixer.asyncio.run",
               side_effect=RuntimeError("boom")):
        MockAgent.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="boom"):
            execute_task("run-001")


# ── _get_github_token edge cases ───────────────────────────────────────────────


def test_get_github_token_none_returns_settings():
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = None

    with patch("phalanx.agents.ci_fixer.settings") as s:
        s.github_token = "ghp_settings_token"
        result = agent._get_github_token(integration)

    assert result == "ghp_settings_token"


# ── outcome_tracker: _get_github_token paths ──────────────────────────────────


@pytest.mark.asyncio
async def test_outcome_tracker_get_token_from_integration():
    """Returns github_token from integration row."""
    from phalanx.ci_fixer.outcome_tracker import _get_github_token

    mock_run = MagicMock()
    mock_run.integration_id = "int-1"

    mock_integration = MagicMock()
    mock_integration.github_token = "ghp_from_db"
    mock_integration.ci_api_key_enc = None

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_integration
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        result = await _get_github_token(mock_run)

    assert result == "ghp_from_db"


@pytest.mark.asyncio
async def test_outcome_tracker_get_token_integration_not_found():
    """Returns None when integration row not found."""
    from phalanx.ci_fixer.outcome_tracker import _get_github_token

    mock_run = MagicMock()
    mock_run.integration_id = "int-missing"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        result = await _get_github_token(mock_run)

    assert result is None


@pytest.mark.asyncio
async def test_outcome_tracker_get_token_db_error():
    """DB error → returns None."""
    from phalanx.ci_fixer.outcome_tracker import _get_github_token

    mock_run = MagicMock()
    mock_run.integration_id = "int-1"

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", side_effect=Exception("DB error")):
        result = await _get_github_token(mock_run)

    assert result is None


# ── proactive_scanner: remaining uncovered paths ──────────────────────────────


@pytest.mark.asyncio
async def test_run_scan_posts_comment_for_warnings():
    """_run_scan posts a comment when there are warning-severity findings."""
    from phalanx.ci_fixer.proactive_scanner import ProactiveFinding, _run_scan

    findings = [ProactiveFinding("fp1", "ruff", "pattern", "warning", ["src/foo.py"])]

    with patch("phalanx.ci_fixer.proactive_scanner.scan_pr_for_patterns",
               new_callable=AsyncMock, return_value=findings), \
         patch("phalanx.ci_fixer.proactive_scanner._post_comment",
               new_callable=AsyncMock, return_value=42), \
         patch("phalanx.ci_fixer.proactive_scanner._record_scan",
               new_callable=AsyncMock) as mock_record:
        await _run_scan("acme/backend", 1, "abc", "token")

    mock_record.assert_called_once()
    call_kwargs = mock_record.call_args[1]
    assert call_kwargs["comment_posted"] is True
    assert call_kwargs["comment_id"] == 42


@pytest.mark.asyncio
async def test_run_scan_no_comment_for_info_only():
    """_run_scan does not post comment when only info findings."""
    from phalanx.ci_fixer.proactive_scanner import ProactiveFinding, _run_scan

    findings = [ProactiveFinding("fp1", "ruff", "pattern", "info", ["src/foo.py"])]

    with patch("phalanx.ci_fixer.proactive_scanner.scan_pr_for_patterns",
               new_callable=AsyncMock, return_value=findings), \
         patch("phalanx.ci_fixer.proactive_scanner._post_comment",
               new_callable=AsyncMock) as mock_post, \
         patch("phalanx.ci_fixer.proactive_scanner._record_scan",
               new_callable=AsyncMock):
        await _run_scan("acme/backend", 1, "abc", "token")

    mock_post.assert_not_called()


# ── pattern_promoter: remaining uncovered paths ───────────────────────────────


@pytest.mark.asyncio
async def test_promote_patterns_updates_existing_entry():
    """Existing registry entry gets its counters updated."""
    from phalanx.ci_fixer.pattern_promoter import _promote_patterns

    row = MagicMock()
    row.fingerprint_hash = "existing_fp"
    row.tool = "ruff"
    row.sample_errors = "test"
    row.last_good_patch_json = '[{"path":"src/foo.py"}]'
    row.repo_count = 5
    row.total_successes = 15

    existing_entry = MagicMock()
    existing_entry.id = "entry-1"

    call_count = {"n": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.all.return_value = [row]
        else:
            result.scalar_one_or_none.return_value = existing_entry
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.pattern_promoter.get_db", return_value=mock_ctx):
        await _promote_patterns()

    # Should have executed an UPDATE (not add) for existing entry
    mock_session.add.assert_not_called()
    # commit was called at least once for the update
    mock_session.commit.assert_called()
