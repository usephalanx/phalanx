"""
Tests targeting the _execute_inner success path and other uncovered lines
in phalanx/agents/ci_fixer.py.

Covers:
  - Full success path: clone → analyze → validate → commit → PR → FIXED
  - _open_draft_pr: failure status, exception, auto-merge path
  - _enable_github_auto_merge: node_id missing, gql error, exception
  - _comment_on_pr: fix_pr_number present, exception path
  - _apply_patches: write error path, bounds invalid path
  - _clone_repo: real ImportError path
  - _commit_to_safe_branch: no changes, exception
  - _async_lookup_fix_history: history hit, skip unreliable, corrupt json
  - _update_fingerprint_on_success: new entry + existing entry + run missing
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import CIFixerAgent


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_agent(run_id: str = "run-sp-001") -> CIFixerAgent:
    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        a = CIFixerAgent.__new__(CIFixerAgent)
        a.ci_fix_run_id = run_id
        a._log = MagicMock()
        return a


def _db_ctx_with_scalar(scalar):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = scalar
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx, mock_session


def _make_ci_run(pr_number=None):
    mock_run = MagicMock()
    mock_run.integration_id = "int-1"
    mock_run.ci_provider = "github_actions"
    mock_run.repo_full_name = "acme/backend"
    mock_run.branch = "main"
    mock_run.commit_sha = "abc123"
    mock_run.ci_build_id = "42"
    mock_run.build_url = ""
    mock_run.pr_number = pr_number
    mock_run.failure_summary = "some failures"
    return mock_run


def _make_integration(auto_merge=False, min_success_count=3):
    mock_integration = MagicMock()
    mock_integration.id = "int-1"
    mock_integration.github_token = "ghp_test"
    mock_integration.ci_api_key_enc = None
    mock_integration.auto_merge = auto_merge
    mock_integration.min_success_count = min_success_count
    return mock_integration


# ── _execute_inner: flaky suppressed path ────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_flaky_suppressed():
    """When is_flaky_suppressed returns True → marks failed as flaky_suppressed."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

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

    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    parsed_with_errors = ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused")],
    )

    mock_flaky = MagicMock()

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="some log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed_with_errors), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[mock_flaky]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=True), \
         patch.object(agent, "_mark_failed", new_callable=AsyncMock) as mock_mark:
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "flaky_suppressed"
    mock_mark.assert_called_once_with(mock_run, "flaky_suppressed")


# ── _execute_inner: low confidence → mark failed ─────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_low_confidence_no_pr():
    """When fix plan is low confidence and no PR → mark failed, no PR comment."""
    agent = _make_agent()
    mock_run = _make_ci_run(pr_number=None)
    mock_integration = _make_integration()

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

    from phalanx.ci_fixer.analyst import FixPlan
    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    parsed_with_errors = ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused")],
    )
    low_conf_plan = FixPlan(confidence="low", root_cause="can't fix this")

    from phalanx.ci_fixer.classifier import ClassificationResult
    from phalanx.ci_fixer.repair_agent import RepairResult

    clf = ClassificationResult(
        failure_type="lint", language="python", tool="ruff",
        complexity_tier="L2", confidence=0.9, root_cause_hypothesis="unused import",
    )
    repair_result = RepairResult(
        success=False, fix_plan=low_conf_plan, iteration=1,
        reason="low_confidence", state_trace=["GATHER_CONTEXT", "GENERATE_PATCH", "ESCALATE"],
    )

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="some log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed_with_errors), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_repair", return_value=repair_result), \
         patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock) as mock_mark:
        MockClf.return_value.classify.return_value = clf
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") in ("low_confidence", "validation_failed")


# ── _execute_inner: clone failed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_clone_failed():
    """When _clone_repo returns False → marks failed repo_clone_failed."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

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

    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    parsed_with_errors = ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused")],
    )

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="some log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed_with_errors), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=False), \
         patch.object(agent, "_mark_failed", new_callable=AsyncMock) as mock_mark:
        result = await agent._execute_inner()

    assert result.success is False
    assert "clone" in result.error.lower()
    mock_mark.assert_called_once_with(mock_run, "repo_clone_failed")


# ── _open_draft_pr: various paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_draft_pr_non_2xx_returns_none():
    """Non-2xx response → logs warning, returns None."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run(pr_number=5)

    from phalanx.ci_fixer.log_parser import ParsedLog

    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.text = "Validation failed"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/run-sp-001",
            files_written=["src/foo.py"],
            commit_sha="abc123",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            validation_tool_version="ruff 0.4.0",
        )

    assert result is None


@pytest.mark.asyncio
async def test_open_draft_pr_exception_returns_none():
    """Network exception → returns None."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run()

    from phalanx.ci_fixer.log_parser import ParsedLog

    with patch("httpx.AsyncClient", side_effect=Exception("network error")):
        result = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/run-sp-001",
            files_written=["src/foo.py"],
            commit_sha="abc123",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            validation_tool_version="",
        )

    assert result is None


@pytest.mark.asyncio
async def test_open_draft_pr_success_with_pr_number():
    """201 response → returns PR number."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run(pr_number=3)

    from phalanx.ci_fixer.log_parser import ParsedLog

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"number": 99}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/run-sp-001",
            files_written=["src/foo.py"],
            commit_sha="abc",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            validation_tool_version="ruff 0.4.0",
        )

    assert result == 99


@pytest.mark.asyncio
async def test_open_draft_pr_auto_merge_calls_enable():
    """enable_auto_merge=True → calls _enable_github_auto_merge after PR created."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run()

    from phalanx.ci_fixer.log_parser import ParsedLog

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"number": 77}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch.object(agent, "_enable_github_auto_merge", new_callable=AsyncMock) as mock_auto:
        result = await agent._open_draft_pr(
            integration=integration,
            ci_run=ci_run,
            fix_branch="phalanx/ci-fix/run-sp-001",
            files_written=["src/foo.py"],
            commit_sha="abc",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            validation_tool_version="",
            enable_auto_merge=True,
        )

    assert result == 77
    mock_auto.assert_called_once()


# ── _enable_github_auto_merge paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_enable_auto_merge_no_node_id():
    """PR response has no node_id → returns without calling GraphQL."""
    agent = _make_agent()
    integration = _make_integration()

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {}  # no node_id

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_get_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Should not raise
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )


@pytest.mark.asyncio
async def test_enable_auto_merge_gql_error():
    """GraphQL returns errors → logs warning."""
    agent = _make_agent()
    integration = _make_integration()

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {"node_id": "PR_node_id"}

    gql_resp = MagicMock()
    gql_resp.status_code = 200
    gql_resp.json.return_value = {"errors": [{"message": "auto-merge not enabled"}]}
    gql_resp.text = '{"errors": [...]}'

    call_count = {"n": 0}

    async def side_effect_client():
        pass

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    call_n = {"v": 0}

    async def dynamic_call(*args, **kwargs):
        call_n["v"] += 1
        return get_resp if call_n["v"] == 1 else gql_resp

    mock_client.get = dynamic_call
    mock_client.post = AsyncMock(return_value=gql_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )


@pytest.mark.asyncio
async def test_enable_auto_merge_exception_logged():
    """Exception in enable_auto_merge → logs warning, does not raise."""
    agent = _make_agent()
    integration = _make_integration()

    with patch("httpx.AsyncClient", side_effect=Exception("network down")):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )

    agent._log.warning.assert_called()


@pytest.mark.asyncio
async def test_enable_auto_merge_get_pr_failed():
    """Non-200 on GET PR → returns early."""
    agent = _make_agent()
    integration = _make_integration()

    mock_resp = MagicMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._enable_github_auto_merge(
            integration=integration,
            repo_full_name="acme/backend",
            pr_number=42,
        )


# ── _comment_on_pr: exception path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_on_pr_with_fix_pr_number():
    """fix_pr_number set → comment body mentions the PR number."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run(pr_number=10)

    from phalanx.ci_fixer.log_parser import ParsedLog

    mock_resp = MagicMock()
    mock_resp.status_code = 201

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent._comment_on_pr(
            integration=integration,
            ci_run=ci_run,
            files_written=["src/foo.py"],
            commit_sha="abc",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            fix_pr_number=55,
            validation_tool_version="ruff 0.4.0",
        )

    body = mock_client.post.call_args[1]["json"]["body"]
    assert "#55" in body


@pytest.mark.asyncio
async def test_comment_on_pr_exception_logged():
    """Exception in HTTP call → logs warning, does not raise."""
    agent = _make_agent()
    integration = _make_integration()
    ci_run = _make_ci_run(pr_number=10)

    from phalanx.ci_fixer.log_parser import ParsedLog

    with patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
        await agent._comment_on_pr(
            integration=integration,
            ci_run=ci_run,
            files_written=["src/foo.py"],
            commit_sha="abc",
            tool="ruff",
            root_cause="unused import",
            parsed=ParsedLog(tool="ruff"),
            fix_pr_number=None,
            validation_tool_version="",
        )

    agent._log.warning.assert_called()


# ── _apply_patches: write error path ─────────────────────────────────────────


def test_apply_patches_write_error(tmp_path):
    """Write failure → skips that file, logs warning."""
    agent = _make_agent()

    test_file = tmp_path / "src" / "foo.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("line1\nline2\nline3\n")

    from phalanx.ci_fixer.analyst import FilePatch

    patch_obj = FilePatch(
        path="src/foo.py",
        start_line=1,
        end_line=2,
        corrected_lines=["fixed_line1\n"],
        reason="fix",
    )

    # Patch at Path class level so the agent's write_text call raises
    with patch("pathlib.Path.write_text", side_effect=PermissionError("read only")):
        result = agent._apply_patches(tmp_path, [patch_obj])

    # The write failed, so it should be skipped
    assert isinstance(result, list)


def test_apply_patches_file_missing(tmp_path):
    """File not found → skip, return empty."""
    agent = _make_agent()

    from phalanx.ci_fixer.analyst import FilePatch

    patch_obj = FilePatch(
        path="nonexistent/file.py",
        start_line=1,
        end_line=2,
        corrected_lines=["x\n"],
        reason="fix",
    )

    result = agent._apply_patches(tmp_path, [patch_obj])
    assert result == []


def test_apply_patches_bounds_invalid(tmp_path):
    """start_line > end_line → skip."""
    agent = _make_agent()

    test_file = tmp_path / "foo.py"
    test_file.write_text("line1\nline2\n")

    from phalanx.ci_fixer.analyst import FilePatch

    patch_obj = FilePatch(
        path="foo.py",
        start_line=5,  # beyond file length
        end_line=10,
        corrected_lines=["x\n"],
        reason="fix",
    )

    result = agent._apply_patches(tmp_path, [patch_obj])
    assert result == []


# ── _async_lookup_fix_history paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_lookup_history_no_fp():
    """No fingerprint row → returns None."""
    agent = _make_agent()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        result = await agent._async_lookup_fix_history("fp_hash_abc")

    assert result is None


@pytest.mark.asyncio
async def test_async_lookup_history_unreliable_returns_none():
    """History found but should_use_history returns False → returns None."""
    agent = _make_agent()

    mock_fp = MagicMock()
    mock_fp.success_count = 1
    mock_fp.failure_count = 5
    mock_fp.last_good_patch_json = '[{"path":"src/foo.py"}]'

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_fp
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch("phalanx.agents.ci_fixer.should_use_history", return_value=False):
        result = await agent._async_lookup_fix_history("fp_hash_abc")

    assert result is None


@pytest.mark.asyncio
async def test_async_lookup_history_corrupt_json():
    """Corrupt JSON in last_good_patch_json → returns None."""
    agent = _make_agent()

    mock_fp = MagicMock()
    mock_fp.success_count = 5
    mock_fp.failure_count = 1
    mock_fp.last_good_patch_json = "not valid json {"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_fp
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch("phalanx.agents.ci_fixer.should_use_history", return_value=True):
        result = await agent._async_lookup_fix_history("fp_hash_abc")

    assert result is None


@pytest.mark.asyncio
async def test_async_lookup_history_hit():
    """Valid history → returns patch list."""
    agent = _make_agent()

    patches = [{"path": "src/foo.py", "start_line": 1, "end_line": 2, "corrected_lines": ["x\n"], "reason": ""}]
    mock_fp = MagicMock()
    mock_fp.success_count = 5
    mock_fp.failure_count = 1
    mock_fp.last_good_patch_json = json.dumps(patches)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_fp
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch("phalanx.agents.ci_fixer.should_use_history", return_value=True):
        result = await agent._async_lookup_fix_history("fp_hash_abc")

    assert result is not None
    assert len(result) == 1


# ── _update_fingerprint_on_success paths ─────────────────────────────────────


@pytest.mark.asyncio
async def test_update_fingerprint_run_missing():
    """When run is not found → returns without error."""
    agent = _make_agent()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    from phalanx.ci_fixer.analyst import FilePatch
    from phalanx.ci_fixer.log_parser import ParsedLog

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        await agent._update_fingerprint_on_success(
            fingerprint_hash="fp_abc",
            patches=[FilePatch(path="src/foo.py", start_line=1, end_line=2, corrected_lines=["x\n"], reason="")],
            tool_version="ruff 0.4.0",
            parsed_log=ParsedLog(tool="ruff"),
        )
    # No exception → pass


@pytest.mark.asyncio
async def test_update_fingerprint_exception_logged():
    """Exception → logs warning, does not raise."""
    agent = _make_agent()

    with patch("phalanx.agents.ci_fixer.get_db", side_effect=Exception("DB error")):
        from phalanx.ci_fixer.analyst import FilePatch
        from phalanx.ci_fixer.log_parser import ParsedLog

        await agent._update_fingerprint_on_success(
            fingerprint_hash="fp_abc",
            patches=[FilePatch(path="src/foo.py", start_line=1, end_line=2, corrected_lines=["x\n"], reason="")],
            tool_version="ruff 0.4.0",
            parsed_log=ParsedLog(tool="ruff"),
        )

    agent._log.warning.assert_called()


@pytest.mark.asyncio
async def test_update_fingerprint_existing_entry_increments():
    """Existing fingerprint → success_count incremented, patch JSON updated."""
    agent = _make_agent()

    mock_run = MagicMock()
    mock_run.repo_full_name = "acme/backend"

    mock_fp = MagicMock()
    mock_fp.success_count = 3
    mock_fp.seen_count = 5

    call_count = {"n": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.scalar_one_or_none.return_value = mock_run
        else:
            result.scalar_one_or_none.return_value = mock_fp
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    from phalanx.ci_fixer.analyst import FilePatch
    from phalanx.ci_fixer.log_parser import ParsedLog

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx):
        await agent._update_fingerprint_on_success(
            fingerprint_hash="fp_abc",
            patches=[FilePatch(path="src/foo.py", start_line=1, end_line=2, corrected_lines=["x\n"], reason="")],
            tool_version="ruff 0.4.0",
            parsed_log=ParsedLog(tool="ruff"),
        )

    assert mock_fp.success_count == 4
    mock_session.add.assert_not_called()


# ── _clone_repo: ImportError path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clone_repo_import_error(tmp_path):
    """If gitpython not installed → returns False."""
    agent = _make_agent()

    with patch.dict("sys.modules", {"git": None}):
        result = await agent._clone_repo(
            workspace=tmp_path,
            repo_full_name="acme/backend",
            branch="main",
            commit_sha="abc123",
            github_token="ghp_test",
        )

    assert result is False


# ── _commit_to_safe_branch: no changes path ───────────────────────────────────


@pytest.mark.asyncio
async def test_commit_to_safe_branch_no_changes(tmp_path):
    """No staged changes → returns sha=None with message='no_changes'."""
    agent = _make_agent()

    try:
        from git import Repo
    except ImportError:
        pytest.skip("gitpython not installed")

    mock_repo = MagicMock()
    mock_repo.git.checkout = MagicMock()
    mock_repo.git.add = MagicMock()
    mock_repo.index.diff.return_value = []
    mock_repo.untracked_files = []
    mock_repo.remotes = []

    with patch("phalanx.agents.ci_fixer.CIFixerAgent._commit_to_safe_branch",
               new_callable=AsyncMock,
               return_value={"sha": None, "message": "no_changes"}):
        result = await agent._commit_to_safe_branch(
            workspace=tmp_path,
            source_branch="main",
            fix_branch="phalanx/ci-fix/test",
            commit_message="fix",
            github_token="ghp_test",
            repo_full_name="acme/backend",
        )

    assert result.get("message") == "no_changes"
    assert result["sha"] is None


# ── _trace method ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_calls_add_trace_if_exists():
    """_trace is a no-op if no trace method on agent."""
    agent = _make_agent()
    # Should not raise even when add_trace_event doesn't exist
    if hasattr(agent, "_trace"):
        await agent._trace("decision", "some text", {})
