"""
Phase 4 unit tests for CIFixerAgent helpers:
  - _check_tool_version_parity (mocked DB)
  - _get_fingerprint_success_count (mocked DB)
  - _enable_github_auto_merge (mocked httpx)
  - _open_draft_pr variants: draft vs auto-merge (mocked httpx)
  - _update_fingerprint_on_success (mocked DB)
  - Additional _apply_patches edge cases
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import CIFixerAgent
from phalanx.ci_fixer.analyst import FilePatch
from phalanx.ci_fixer.version_parity import VersionParityResult


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_agent() -> CIFixerAgent:
    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = CIFixerAgent.__new__(CIFixerAgent)
        agent.ci_fix_run_id = "test-run-p4"
        agent._log = MagicMock()
        return agent


def _write(tmp_path: Path, rel: str, lines: list[str]) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("".join(lines))
    return full


def _mock_db_session(return_value=None):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = return_value
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx, mock_session


# ── _check_tool_version_parity ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_tool_version_parity_no_fingerprint():
    agent = _make_agent()
    result = await agent._check_tool_version_parity(None, "ruff 0.4.1")
    assert result.ok is True
    assert "skipped" in result.reason


@pytest.mark.asyncio
async def test_check_tool_version_parity_no_local_version():
    agent = _make_agent()
    result = await agent._check_tool_version_parity("abc123", "")
    assert result.ok is True


@pytest.mark.asyncio
async def test_check_tool_version_parity_no_history():
    """No fingerprint row in DB → ok=True."""
    agent = _make_agent()
    mock_ctx, _ = _mock_db_session(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._check_tool_version_parity("abc123", "ruff 0.4.1")

    assert result.ok is True
    assert "skipped" in result.reason


@pytest.mark.asyncio
async def test_check_tool_version_parity_match():
    """Versions match → ok=True."""
    agent = _make_agent()
    fp = MagicMock()
    fp.last_good_tool_version = "ruff 0.4.1"
    mock_ctx, _ = _mock_db_session(return_value=fp)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._check_tool_version_parity("abc123", "ruff 0.4.2")

    assert result.ok is True


@pytest.mark.asyncio
async def test_check_tool_version_parity_mismatch():
    """Minor version mismatch → ok=False."""
    agent = _make_agent()
    fp = MagicMock()
    fp.last_good_tool_version = "ruff 0.4.1"
    mock_ctx, _ = _mock_db_session(return_value=fp)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._check_tool_version_parity("abc123", "ruff 0.5.0")

    assert result.ok is False


@pytest.mark.asyncio
async def test_check_tool_version_parity_db_error():
    """DB error → ok=True (safe fail-open)."""
    agent = _make_agent()

    with patch("phalanx.agents.ci_fixer.get_db", side_effect=Exception("DB error")):
        result = await agent._check_tool_version_parity("abc123", "ruff 0.4.1")

    assert result.ok is True


# ── _get_fingerprint_success_count ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_fingerprint_success_count_found():
    agent = _make_agent()
    fp = MagicMock()
    fp.success_count = 5
    mock_ctx, _ = _mock_db_session(return_value=fp)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        count = await agent._get_fingerprint_success_count("abc123")

    assert count == 5


@pytest.mark.asyncio
async def test_get_fingerprint_success_count_not_found():
    agent = _make_agent()
    mock_ctx, _ = _mock_db_session(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        count = await agent._get_fingerprint_success_count("abc123")

    assert count == 0


@pytest.mark.asyncio
async def test_get_fingerprint_success_count_none_hash():
    agent = _make_agent()
    count = await agent._get_fingerprint_success_count(None)
    assert count == 0


@pytest.mark.asyncio
async def test_get_fingerprint_success_count_db_error():
    agent = _make_agent()
    with patch("phalanx.agents.ci_fixer.get_db", side_effect=Exception("DB error")):
        count = await agent._get_fingerprint_success_count("abc123")
    assert count == 0


# ── _enable_github_auto_merge ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enable_github_auto_merge_success():
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"

    # Mock REST response for getting PR node_id
    rest_response = MagicMock()
    rest_response.status_code = 200
    rest_response.json.return_value = {"node_id": "PR_abc123"}

    # Mock GraphQL response
    gql_response = MagicMock()
    gql_response.status_code = 200
    gql_response.json.return_value = {
        "data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"mergeMethod": "SQUASH"}}}}
    }

    call_count = {"get": 0, "post": 0}

    async def mock_get(url, **kwargs):
        call_count["get"] += 1
        return rest_response

    async def mock_post(url, **kwargs):
        call_count["post"] += 1
        return gql_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = mock_get
    mock_client.post = mock_post

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )

    assert call_count["get"] == 1  # REST call for node_id
    assert call_count["post"] == 1  # GraphQL mutation


@pytest.mark.asyncio
async def test_enable_github_auto_merge_pr_not_found():
    """404 on PR fetch → logs warning but doesn't raise."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"

    rest_response = MagicMock()
    rest_response.status_code = 404

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=rest_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )  # should not raise


@pytest.mark.asyncio
async def test_enable_github_auto_merge_network_error():
    """Network error → logs warning but doesn't raise."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"

    with patch("httpx.AsyncClient", side_effect=Exception("network error")):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )  # should not raise


# ── _open_draft_pr ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_draft_pr_creates_draft():
    """Default: draft=True, auto-merge disabled."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"
    ci_run = MagicMock()
    ci_run.repo_full_name = "acme/backend"
    ci_run.branch = "main"
    ci_run.pr_number = None

    from phalanx.ci_fixer.log_parser import ParsedLog
    parsed = ParsedLog(tool="ruff")

    pr_response = MagicMock()
    pr_response.status_code = 201
    pr_response.json.return_value = {"number": 99}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=pr_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        pr_num = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/test",
            files_written=["src/foo.py"],
            commit_sha="abc123",
            tool="ruff",
            root_cause="unused import",
            parsed=parsed,
            validation_tool_version="ruff 0.4.1",
            enable_auto_merge=False,
        )

    assert pr_num == 99
    # Verify draft=True was passed
    call_kwargs = mock_client.post.call_args
    body_json = call_kwargs[1]["json"]
    assert body_json["draft"] is True


@pytest.mark.asyncio
async def test_open_draft_pr_with_auto_merge():
    """enable_auto_merge=True: draft=False, _enable_github_auto_merge called."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"
    ci_run = MagicMock()
    ci_run.repo_full_name = "acme/backend"
    ci_run.branch = "main"
    ci_run.pr_number = 10

    from phalanx.ci_fixer.log_parser import ParsedLog
    parsed = ParsedLog(tool="ruff")

    pr_response = MagicMock()
    pr_response.status_code = 201
    pr_response.json.return_value = {"number": 100}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=pr_response)

    enable_auto_merge_called = {"n": 0}

    async def mock_enable_auto_merge(**kwargs):
        enable_auto_merge_called["n"] += 1

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch.object(agent, "_enable_github_auto_merge", side_effect=mock_enable_auto_merge):
        pr_num = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/test",
            files_written=["src/foo.py"],
            commit_sha="abc123",
            tool="ruff",
            root_cause="unused import",
            parsed=parsed,
            validation_tool_version="ruff 0.4.1",
            enable_auto_merge=True,
        )

    assert pr_num == 100
    # draft=False
    call_kwargs = mock_client.post.call_args
    body_json = call_kwargs[1]["json"]
    assert body_json["draft"] is False
    # auto-merge enable was called
    assert enable_auto_merge_called["n"] == 1


@pytest.mark.asyncio
async def test_open_draft_pr_failure_returns_none():
    """Non-2xx response → returns None."""
    agent = _make_agent()
    integration = MagicMock()
    integration.github_token = "ghp_test"
    ci_run = MagicMock()
    ci_run.repo_full_name = "acme/backend"
    ci_run.branch = "main"
    ci_run.pr_number = None

    from phalanx.ci_fixer.log_parser import ParsedLog
    parsed = ParsedLog(tool="ruff")

    pr_response = MagicMock()
    pr_response.status_code = 422
    pr_response.text = "Unprocessable Entity"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=pr_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        pr_num = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/test",
            files_written=["src/foo.py"],
            commit_sha="abc123",
            tool="ruff",
            root_cause="unused import",
            parsed=parsed,
            validation_tool_version="ruff 0.4.1",
        )

    assert pr_num is None


# ── _update_fingerprint_on_success ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_fingerprint_on_success_creates_new():
    """Creates new fingerprint row when none exists."""
    agent = _make_agent()
    mock_ctx, mock_session = _mock_db_session(return_value=None)

    # First execute returns None (ci_fix_run query)
    run = MagicMock()
    run.repo_full_name = "acme/backend"
    run.id = "run-001"

    call_count = {"n": 0}

    async def mock_execute(stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = run
        else:
            result.scalar_one_or_none.return_value = None  # no existing fingerprint
        return result

    mock_session.execute = mock_execute

    from phalanx.ci_fixer.analyst import FilePatch
    from phalanx.ci_fixer.log_parser import ParsedLog

    patches = [FilePatch(path="src/foo.py", start_line=1, end_line=1, corrected_lines=["x\n"])]
    parsed = ParsedLog(tool="ruff")

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        await agent._update_fingerprint_on_success(
            fingerprint_hash="abc123",
            patches=patches,
            tool_version="ruff 0.4.1",
            parsed_log=parsed,
        )

    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_update_fingerprint_on_success_increments_existing():
    """Increments success_count on existing fingerprint."""
    agent = _make_agent()

    run = MagicMock()
    run.repo_full_name = "acme/backend"
    run.id = "run-001"

    existing_fp = MagicMock()
    existing_fp.success_count = 2
    existing_fp.seen_count = 3

    call_count = {"n": 0}

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = run
        else:
            result.scalar_one_or_none.return_value = existing_fp
        return result

    mock_session.execute = mock_execute

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    from phalanx.ci_fixer.analyst import FilePatch
    from phalanx.ci_fixer.log_parser import ParsedLog

    patches = [FilePatch(path="src/foo.py", start_line=1, end_line=1, corrected_lines=["x\n"])]
    parsed = ParsedLog(tool="ruff")

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        await agent._update_fingerprint_on_success(
            fingerprint_hash="abc123",
            patches=patches,
            tool_version="ruff 0.4.1",
            parsed_log=parsed,
        )

    assert existing_fp.success_count == 3
    assert existing_fp.seen_count == 4
