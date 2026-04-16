"""
Tests for phalanx.api.routes.ci_fix_runs — CI fix run context API.

Coverage targets:
  - GET /v1/ci-fix-runs/{run_id}/context — found, not found, no context, parse error
  - GET /v1/ci-fix-runs/{run_id}         — found, not found
  - GET /v1/ci-fix-runs                  — list, filters
  - _find_existing_fix_pr                — found, not found, error handling
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from phalanx.ci_fixer.context import CIFixContext, StructuredFailure


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_ci_run(
    run_id="run-abc",
    repo="owner/repo",
    branch="main",
    commit_sha="abc123",
    build_id="build-1",
    status="FIXED",
    pipeline_context_json=None,
    fix_pr_number=None,
    fix_branch=None,
    fix_commit_sha=None,
    fingerprint_hash=None,
    error=None,
):
    run = MagicMock()
    run.id = run_id
    run.repo_full_name = repo
    run.branch = branch
    run.commit_sha = commit_sha
    run.ci_build_id = build_id
    run.ci_provider = "github_actions"
    run.status = status
    run.pipeline_context_json = pipeline_context_json
    run.fix_pr_number = fix_pr_number
    run.fix_branch = fix_branch
    run.fix_commit_sha = fix_commit_sha
    run.fingerprint_hash = fingerprint_hash
    run.error = error
    run.created_at = MagicMock()
    run.created_at.isoformat.return_value = "2026-04-15T12:00:00+00:00"
    run.completed_at = None
    return run


def _make_context_json(run_id="run-abc") -> str:
    ctx = CIFixContext(
        ci_fix_run_id=run_id,
        repo="owner/repo",
        branch="main",
        commit_sha="abc123",
        original_build_id="build-1",
    )
    ctx.structured_failure = StructuredFailure(
        tool="ruff", failure_type="lint", reproducer_cmd="ruff check ."
    )
    ctx.complete("fixed")
    return json.dumps(ctx.to_dict())


# ── GET /v1/ci-fix-runs/{run_id}/context ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_context_not_found():
    from phalanx.api.routes.ci_fix_runs import get_fix_run_context

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_fix_run_context("nonexistent")
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_context_no_pipeline_json():
    """Run exists but has no pipeline_context_json (pre-Phase 1 run)."""
    from phalanx.api.routes.ci_fix_runs import get_fix_run_context

    ci_run = _make_ci_run(pipeline_context_json=None, status="FIXED")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ci_run
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await get_fix_run_context("run-abc")

    assert result["ci_fix_run_id"] == "run-abc"
    assert result["final_status"] == "unknown"
    assert "_note" in result
    assert result["current_stage"] == "unknown"


@pytest.mark.asyncio
async def test_get_context_with_pipeline_json():
    """Run has pipeline_context_json — returns full parsed context."""
    from phalanx.api.routes.ci_fix_runs import get_fix_run_context

    ctx_json = _make_context_json("run-abc")
    ci_run = _make_ci_run(pipeline_context_json=ctx_json, status="FIXED")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ci_run
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await get_fix_run_context("run-abc")

    assert result["ci_fix_run_id"] == "run-abc"
    assert result["final_status"] == "fixed"
    assert result["current_stage"] in ("parsed", "committed", "patched", "classified", "started")
    assert result["structured_failure"]["tool"] == "ruff"


@pytest.mark.asyncio
async def test_get_context_invalid_json():
    """pipeline_context_json is corrupt — returns 500."""
    from phalanx.api.routes.ci_fix_runs import get_fix_run_context

    ci_run = _make_ci_run(pipeline_context_json="not valid json{{{", status="FIXED")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ci_run
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_fix_run_context("run-abc")
        assert exc_info.value.status_code == 500


# ── GET /v1/ci-fix-runs/{run_id} ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_fix_run_found():
    from phalanx.api.routes.ci_fix_runs import get_fix_run

    ci_run = _make_ci_run(fix_pr_number=7, fix_branch="phalanx/ci-fix/run-abc")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ci_run
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await get_fix_run("run-abc")

    assert result["id"] == "run-abc"
    assert result["fix_pr_number"] == 7
    assert result["fix_branch"] == "phalanx/ci-fix/run-abc"
    assert result["has_context"] is False


@pytest.mark.asyncio
async def test_get_fix_run_not_found():
    from phalanx.api.routes.ci_fix_runs import get_fix_run

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_fix_run("nonexistent")
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_fix_run_has_context_true():
    from phalanx.api.routes.ci_fix_runs import get_fix_run

    ctx_json = _make_context_json("run-abc")
    ci_run = _make_ci_run(pipeline_context_json=ctx_json)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ci_run
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await get_fix_run("run-abc")

    assert result["has_context"] is True


# ── GET /v1/ci-fix-runs (list) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_fix_runs_empty():
    from phalanx.api.routes.ci_fix_runs import list_fix_runs

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await list_fix_runs(limit=20, run_status=None)

    assert result["runs"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_list_fix_runs_with_results():
    from phalanx.api.routes.ci_fix_runs import list_fix_runs

    runs = [
        _make_ci_run(run_id="run-1", status="FIXED"),
        _make_ci_run(run_id="run-2", status="FAILED", error="no_structured_errors"),
    ]

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = runs
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await list_fix_runs(limit=20, run_status=None)

    assert result["count"] == 2
    assert result["runs"][0]["id"] == "run-1"
    assert result["runs"][1]["error"] == "no_structured_errors"


@pytest.mark.asyncio
async def test_list_fix_runs_filters_applied():
    """Filters are passed through — just test the query builds without error."""
    from phalanx.api.routes.ci_fix_runs import list_fix_runs

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx_manager = AsyncMock()
    mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx_manager.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.api.routes.ci_fix_runs.get_db", return_value=mock_ctx_manager):
        result = await list_fix_runs(
            repo="owner/repo", branch="main", run_status="FIXED", limit=5
        )

    assert result["count"] == 0


# ── _find_existing_fix_pr ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_existing_fix_pr_found():
    """Returns PR number when an open phalanx/ci-fix/* PR exists."""
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent.__new__(CIFixerAgent)
    agent._log = MagicMock()
    agent._log.info = MagicMock()
    agent._log.warning = MagicMock()

    integration = MagicMock()
    integration.github_token = "ghp_test"

    ci_run = MagicMock()
    ci_run.repo_full_name = "owner/repo"
    ci_run.branch = "feature/foo"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "number": 42,
            "head": {"ref": "phalanx/ci-fix/old-run-id"},
        }
    ]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._find_existing_fix_pr(integration, ci_run)

    assert result == 42
    agent._log.info.assert_called_once()


@pytest.mark.asyncio
async def test_find_existing_fix_pr_not_found():
    """Returns None when no phalanx/ci-fix/* PR exists."""
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent.__new__(CIFixerAgent)
    agent._log = MagicMock()
    agent._log.info = MagicMock()
    agent._log.warning = MagicMock()

    integration = MagicMock()
    integration.github_token = "ghp_test"

    ci_run = MagicMock()
    ci_run.repo_full_name = "owner/repo"
    ci_run.branch = "feature/foo"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "number": 5,
            "head": {"ref": "feature/some-other-fix"},  # not a phalanx fix branch
        }
    ]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._find_existing_fix_pr(integration, ci_run)

    assert result is None


@pytest.mark.asyncio
async def test_find_existing_fix_pr_api_error():
    """Returns None on HTTP error — does not raise."""
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent.__new__(CIFixerAgent)
    agent._log = MagicMock()
    agent._log.info = MagicMock()
    agent._log.warning = MagicMock()

    integration = MagicMock()
    integration.github_token = "ghp_test"

    ci_run = MagicMock()
    ci_run.repo_full_name = "owner/repo"
    ci_run.branch = "feature/foo"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("network error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._find_existing_fix_pr(integration, ci_run)

    assert result is None
    agent._log.warning.assert_called_once()


@pytest.mark.asyncio
async def test_find_existing_fix_pr_non_200():
    """Returns None when GitHub API returns non-200."""
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent.__new__(CIFixerAgent)
    agent._log = MagicMock()
    agent._log.info = MagicMock()
    agent._log.warning = MagicMock()

    integration = MagicMock()
    integration.github_token = "ghp_test"

    ci_run = MagicMock()
    ci_run.repo_full_name = "owner/repo"
    ci_run.branch = "feature/foo"

    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._find_existing_fix_pr(integration, ci_run)

    assert result is None


@pytest.mark.asyncio
async def test_find_existing_fix_pr_empty_list():
    """Returns None when PR list is empty."""
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent.__new__(CIFixerAgent)
    agent._log = MagicMock()
    agent._log.info = MagicMock()
    agent._log.warning = MagicMock()

    integration = MagicMock()
    integration.github_token = "ghp_test"

    ci_run = MagicMock()
    ci_run.repo_full_name = "owner/repo"
    ci_run.branch = "feature/foo"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._find_existing_fix_pr(integration, ci_run)

    assert result is None
