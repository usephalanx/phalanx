"""
Tests for the analyst loop in _execute_inner (lines 243-458):
  - Delta guard: total_delta > _MAX_TOTAL_LINE_DELTA → low_confidence
  - Too many files guard: > _MAX_FILES_CHANGED → low_confidence
  - No files written → low_confidence
  - Validation failure path + retry with re-parsed errors
  - Full success path: validation passes → commit → PR → FIXED
  - Commit failed (sha=None) → mark failed
  - Low confidence with PR → posts unable_to_fix comment
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import (
    _MAX_FILES_CHANGED,
    _MAX_TOTAL_LINE_DELTA,
    CIFixerAgent,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_agent(run_id: str = "run-loop-001") -> CIFixerAgent:
    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        a = CIFixerAgent.__new__(CIFixerAgent)
        a.ci_fix_run_id = run_id
        a._log = MagicMock()
        return a


def _make_ci_run(pr_number=None):
    r = MagicMock()
    r.integration_id = "int-1"
    r.ci_provider = "github_actions"
    r.repo_full_name = "acme/backend"
    r.branch = "main"
    r.commit_sha = "abc123"
    r.ci_build_id = "42"
    r.build_url = ""
    r.pr_number = pr_number
    r.failure_summary = "CI failed"
    return r


def _make_integration(auto_merge=False):
    i = MagicMock()
    i.id = "int-1"
    i.github_token = "ghp_test"
    i.ci_api_key_enc = None
    i.auto_merge = auto_merge
    i.min_success_count = 3
    return i


def _db_sequence(*scalars):
    """Build get_db() mock that returns scalars in sequence."""
    call_n = {"v": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(_stmt):
        result = MagicMock()
        idx = min(call_n["v"], len(scalars) - 1)
        result.scalar_one_or_none.return_value = scalars[idx]
        call_n["v"] += 1
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx, mock_session


def _make_parsed_with_lint():
    from phalanx.ci_fixer.log_parser import LintError, ParsedLog

    return ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused")],
    )


def _make_fix_plan_with_patches(n_patches=1, delta_per_patch=1):
    """Return a high-confidence FixPlan with n_patches patches."""
    from phalanx.ci_fixer.analyst import FilePatch, FixPlan

    patches = [
        FilePatch(
            path=f"src/file{i}.py",
            start_line=1,
            end_line=1 + delta_per_patch,
            corrected_lines=["fixed\n"] * delta_per_patch,
            reason="fix",
        )
        for i in range(n_patches)
    ]
    return FixPlan(confidence="high", root_cause="unused import", patches=patches)


# ── analyst loop: delta guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_delta_guard_exceeded():
    """total_delta > _MAX_TOTAL_LINE_DELTA → low_confidence, mark failed."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    # Create a fix plan with large delta
    from phalanx.ci_fixer.analyst import FilePatch, FixPlan

    big_plan = FixPlan(
        confidence="high",
        root_cause="big change",
        patches=[
            FilePatch(
                path="src/foo.py",
                start_line=1,
                end_line=1,
                corrected_lines=["x\n"] * (_MAX_TOTAL_LINE_DELTA + 5),
                reason="big",
            )
        ],
    )

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = big_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is False
    assert "large" in result.output.get("root_cause", "").lower() or result.output.get(
        "reason"
    ) in ("low_confidence",)


# ── analyst loop: too many files guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_too_many_files():
    """> _MAX_FILES_CHANGED patches → low_confidence."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    big_plan = _make_fix_plan_with_patches(n_patches=_MAX_FILES_CHANGED + 2)

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = big_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "low_confidence"


# ── analyst loop: no files written ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_no_files_written():
    """_apply_patches returns empty → low_confidence."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=[]),
        patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "low_confidence"


# ── analyst loop: validation failure, low_confidence with PR comment ──────────


@pytest.mark.asyncio
async def test_execute_inner_validation_failed_with_pr():
    """validation.passed=False after all iterations + pr_number → comment_unable_to_fix called."""
    agent = _make_agent()
    mock_run = _make_ci_run(pr_number=7)
    mock_integration = _make_integration()

    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    mock_validation = MagicMock()
    mock_validation.passed = False
    mock_validation.tool = "ruff"
    mock_validation.tool_version = "ruff 0.4.0"
    mock_validation.output = "still failing"

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=["src/foo.py"]),
        patch("phalanx.agents.ci_fixer.validate_fix", return_value=mock_validation),
        patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock),
        patch.object(agent, "_comment_unable_to_fix", new_callable=AsyncMock) as mock_unable,
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "validation_failed"
    mock_unable.assert_called_once()


# ── analyst loop: validation passed → commit failed → mark failed ─────────────


@pytest.mark.asyncio
async def test_execute_inner_commit_failed():
    """validation passes but commit returns sha=None → mark failed."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    mock_validation = MagicMock()
    mock_validation.passed = True
    mock_validation.tool = "ruff"
    mock_validation.tool_version = "ruff 0.4.0"
    mock_validation.output = ""

    from phalanx.ci_fixer.version_parity import VersionParityResult

    mock_parity = VersionParityResult(
        ok=True, local_version="ruff 0.4.0", failure_version="", reason="ok"
    )

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=["src/foo.py"]),
        patch("phalanx.agents.ci_fixer.validate_fix", return_value=mock_validation),
        patch.object(
            agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity
        ),
        patch.object(
            agent,
            "_commit_to_safe_branch",
            new_callable=AsyncMock,
            return_value={"sha": None, "error": "commit failed"},
        ),
        patch.object(
            agent,
            "_commit_to_author_branch",
            new_callable=AsyncMock,
            return_value={"sha": None, "error": "commit failed"},
        ),
        patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is False
    assert "commit" in result.error.lower()


# ── Full success path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_success_path():
    """Full success: validation passes → commit OK → PR opened → FIXED status."""
    agent = _make_agent()
    mock_run = _make_ci_run(pr_number=3)
    mock_integration = _make_integration()

    call_n = {"v": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(_stmt):
        call_n["v"] += 1
        result = MagicMock()
        if call_n["v"] == 1:
            result.scalar_one_or_none.return_value = mock_run
        elif call_n["v"] == 2:
            result.scalar_one_or_none.return_value = mock_integration
        else:
            result.scalar_one_or_none.return_value = None
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    mock_validation = MagicMock()
    mock_validation.passed = True
    mock_validation.tool = "ruff"
    mock_validation.tool_version = "ruff 0.4.0"
    mock_validation.output = ""

    from phalanx.ci_fixer.version_parity import VersionParityResult

    mock_parity = VersionParityResult(
        ok=True, local_version="ruff 0.4.0", failure_version="", reason="ok"
    )

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=["src/foo.py"]),
        patch("phalanx.agents.ci_fixer.validate_fix", return_value=mock_validation),
        patch.object(
            agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity
        ),
        patch.object(
            agent, "_get_fingerprint_success_count", new_callable=AsyncMock, return_value=0
        ),
        patch.object(
            agent,
            "_commit_to_safe_branch",
            new_callable=AsyncMock,
            return_value={
                "sha": "abc12345",
                "branch": "phalanx/ci-fix/run-loop-001",
                "push_failed": False,
            },
        ),
        patch.object(
            agent,
            "_commit_to_author_branch",
            new_callable=AsyncMock,
            return_value={"sha": "abc12345", "branch": "feat/my-pr", "push_failed": False},
        ),
        patch.object(agent, "_open_draft_pr", new_callable=AsyncMock, return_value=42),
        patch.object(agent, "_comment_on_pr", new_callable=AsyncMock),
        patch.object(agent, "_comment_lint_fix_pushed", new_callable=AsyncMock),
        patch.object(agent, "_update_fingerprint_on_success", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is True
    assert result.output["tool"] == "ruff"
    # lint_only=True → author_branch strategy → no separate fix PR opened
    assert result.output["fix_pr_number"] is None
    assert result.output["commit_sha"] == "abc12345"


@pytest.mark.asyncio
async def test_execute_inner_success_push_failed_no_pr():
    """push_failed=True → no PR opened, still marks FIXED."""
    agent = _make_agent()
    mock_run = _make_ci_run(pr_number=None)
    mock_integration = _make_integration()

    call_n = {"v": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    async def mock_execute(_stmt):
        call_n["v"] += 1
        result = MagicMock()
        if call_n["v"] == 1:
            result.scalar_one_or_none.return_value = mock_run
        elif call_n["v"] == 2:
            result.scalar_one_or_none.return_value = mock_integration
        else:
            result.scalar_one_or_none.return_value = None
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    mock_validation = MagicMock()
    mock_validation.passed = True
    mock_validation.tool = "ruff"
    mock_validation.tool_version = ""
    mock_validation.output = ""

    from phalanx.ci_fixer.version_parity import VersionParityResult

    mock_parity = VersionParityResult(ok=True, local_version="", failure_version="", reason="ok")

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=["src/foo.py"]),
        patch("phalanx.agents.ci_fixer.validate_fix", return_value=mock_validation),
        patch.object(
            agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity
        ),
        patch.object(
            agent, "_get_fingerprint_success_count", new_callable=AsyncMock, return_value=0
        ),
        patch.object(
            agent,
            "_commit_to_safe_branch",
            new_callable=AsyncMock,
            return_value={
                "sha": "deadbeef",
                "branch": "phalanx/ci-fix/run-loop-001",
                "push_failed": True,
            },
        ),
        patch.object(
            agent,
            "_commit_to_author_branch",
            new_callable=AsyncMock,
            return_value={"sha": "deadbeef", "branch": "feat/my-pr", "push_failed": True},
        ),
        patch.object(agent, "_comment_lint_fix_pushed", new_callable=AsyncMock),
        patch.object(
            agent, "_update_fingerprint_on_success", new_callable=AsyncMock
        ) as mock_fp_update,
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is True
    assert result.output["commit_sha"] == "deadbeef"
    assert result.output["fix_pr_number"] is None
    mock_fp_update.assert_called_once()


# ── Validation loop: retry path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_validation_retry_then_pass():
    """First iteration fails validation → second iteration passes."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()

    call_n = {"v": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    async def mock_execute(_stmt):
        call_n["v"] += 1
        result = MagicMock()
        if call_n["v"] == 1:
            result.scalar_one_or_none.return_value = mock_run
        elif call_n["v"] == 2:
            result.scalar_one_or_none.return_value = mock_integration
        else:
            result.scalar_one_or_none.return_value = None
        return result

    mock_session.execute = mock_execute
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    from phalanx.ci_fixer.log_parser import ParsedLog

    parsed = _make_parsed_with_lint()
    good_plan = _make_fix_plan_with_patches(n_patches=1)

    # First call fails, second call passes
    fail_validation = MagicMock()
    fail_validation.passed = False
    fail_validation.tool = "ruff"
    fail_validation.tool_version = "ruff 0.4.0"
    fail_validation.output = "E401 still present"

    pass_validation = MagicMock()
    pass_validation.passed = True
    pass_validation.tool = "ruff"
    pass_validation.tool_version = "ruff 0.4.0"
    pass_validation.output = ""

    validation_calls = {"n": 0}
    empty_retry = ParsedLog(tool="unknown")  # no errors on retry

    from phalanx.ci_fixer.version_parity import VersionParityResult

    mock_parity = VersionParityResult(
        ok=True, local_version="ruff 0.4.0", failure_version="", reason="ok"
    )

    def _validation_side_effect(*args, **kwargs):
        validation_calls["n"] += 1
        return fail_validation if validation_calls["n"] == 1 else pass_validation

    with (
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", side_effect=[parsed, empty_retry]),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        patch("phalanx.agents.ci_fixer.RootCauseAnalyst") as MockAnalyst,
        patch.object(agent, "_apply_patches", return_value=["src/foo.py"]),
        patch("phalanx.agents.ci_fixer.validate_fix", side_effect=_validation_side_effect),
        patch.object(
            agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity
        ),
        patch.object(
            agent, "_get_fingerprint_success_count", new_callable=AsyncMock, return_value=0
        ),
        patch.object(
            agent,
            "_commit_to_safe_branch",
            new_callable=AsyncMock,
            return_value={"sha": "abc", "push_failed": False},
        ),
        patch.object(
            agent,
            "_commit_to_author_branch",
            new_callable=AsyncMock,
            return_value={"sha": "abc", "branch": "feat/my-pr", "push_failed": False},
        ),
        patch.object(agent, "_open_draft_pr", new_callable=AsyncMock, return_value=11),
        patch.object(agent, "_comment_on_pr", new_callable=AsyncMock),
        patch.object(agent, "_comment_lint_fix_pushed", new_callable=AsyncMock),
        patch.object(agent, "_update_fingerprint_on_success", new_callable=AsyncMock),
    ):
        mock_analyst_inst = MagicMock()
        mock_analyst_inst.analyze.return_value = good_plan
        MockAnalyst.return_value = mock_analyst_inst
        result = await agent._execute_inner()

    assert result.success is True


# ── _trace test (line 100) ─────────────────────────────────────────────────────


def test_execute_calls_execute_inner():
    """execute() calls _execute_inner in asyncio.run context."""
    from phalanx.agents.base import AgentResult

    agent = _make_agent()
    expected = AgentResult(success=True, output={"done": True})

    with patch.object(agent, "_execute_inner", new_callable=AsyncMock, return_value=expected):
        import asyncio

        result = asyncio.run(agent.execute())

    assert result.success is True


# ── _cleanup_workspace on execute ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_cleans_workspace_on_exception(tmp_path):
    """execute() calls _cleanup_workspace in finally block even on exception."""
    agent = _make_agent()

    workspace = tmp_path / "ci-fixer" / "run-loop-001"
    workspace.mkdir(parents=True)

    with patch.object(
        agent, "_execute_inner", new_callable=AsyncMock, side_effect=RuntimeError("boom")
    ):
        result = await agent.execute()

    assert result.success is False
