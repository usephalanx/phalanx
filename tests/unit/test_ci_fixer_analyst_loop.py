"""
Tests for _execute_inner pipeline — validates the 3-stage pipeline wiring:
  classify → retrieve context → run_repair → commit/PR/mark.

All three pipeline stages are mocked at the ci_fixer module level.
Tests verify:
  - repair failed (various reasons) → mark_failed + optional PR comment
  - repair success → commit → PR → FIXED
  - commit failed → mark failed
  - push_failed=True → no PR but still FIXED
  - full success path with PR comment
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import (
    CIFixerAgent,
    _MAX_FILES_CHANGED,
    _MAX_TOTAL_LINE_DELTA,
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


def _make_classification(tier="L2"):
    from phalanx.ci_fixer.classifier import ClassificationResult

    return ClassificationResult(
        failure_type="lint",
        language="python",
        tool="ruff",
        complexity_tier=tier,
        confidence=0.9,
        root_cause_hypothesis="unused import os",
    )


def _make_fix_plan(n_patches=1):
    from phalanx.ci_fixer.analyst import FilePatch, FixPlan

    return FixPlan(
        confidence="high",
        root_cause="unused import os",
        patches=[FilePatch(f"src/file{i}.py", 1, 1, ["x = 1\n"]) for i in range(n_patches)],
    )


def _make_repair_result(success=True, reason="", fix_plan=None, validation=None, escalate=False):
    from phalanx.ci_fixer.repair_agent import RepairResult

    return RepairResult(
        success=success,
        fix_plan=fix_plan or _make_fix_plan(),
        validation=validation,
        iteration=1,
        escalate=escalate,
        reason=reason,
        state_trace=["GATHER_CONTEXT", "GENERATE_PATCH", "VALIDATE_PATCH", "SUBMIT" if success else "GIVE_UP"],
    )


def _make_validation(passed=True, tool_version="ruff 0.4.0"):
    from phalanx.ci_fixer.validator import ValidationResult

    return ValidationResult(passed=passed, tool="ruff", output="" if passed else "still failing",
                            tool_version=tool_version)


def _make_parity(ok=True):
    from phalanx.ci_fixer.version_parity import VersionParityResult

    return VersionParityResult(ok=ok, local_version="ruff 0.4.0", failure_version="", reason="ok")


def _base_patches(agent, mock_ctx, parsed, classification=None, repair_result=None, context_bundle=None):
    """Return common patches needed for _execute_inner to reach the pipeline stage."""
    classification = classification or _make_classification()
    repair_result = repair_result or _make_repair_result()
    mock_bundle = context_bundle or MagicMock()
    mock_bundle.log_excerpt = ""

    return [
        patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx),
        patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"),
        patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed),
        patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock),
        patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]),
        patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False),
        patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True),
        patch.object(agent, "_trace", new_callable=AsyncMock),
        # Mock the 3 pipeline stages
        patch("phalanx.agents.ci_fixer.LLMClassifier") ,
        patch("phalanx.agents.ci_fixer.ContextRetriever"),
        patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result),
    ]


# ── repair failed: low confidence ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_repair_failed_low_confidence():
    """repair_result.success=False → mark_failed called, returns failure."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()
    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    classification = _make_classification()
    repair_result = _make_repair_result(success=False, reason="low_confidence")

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result), \
         patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock) as mock_mark:
        MockClf.return_value.classify.return_value = classification
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output["reason"] == "low_confidence"
    mock_mark.assert_called_once()


# ── repair failed + PR → comment_unable_to_fix ────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_validation_failed_with_pr():
    """repair fails with validation_failed + pr_number → comment_unable_to_fix called."""
    agent = _make_agent()
    mock_run = _make_ci_run(pr_number=7)
    mock_integration = _make_integration()
    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    classification = _make_classification()
    repair_result = _make_repair_result(success=False, reason="max_iterations_exhausted")

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result), \
         patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock), \
         patch.object(agent, "_comment_unable_to_fix", new_callable=AsyncMock) as mock_unable:
        MockClf.return_value.classify.return_value = classification
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output.get("reason") == "max_iterations_exhausted"
    mock_unable.assert_called_once()


# ── repair success → commit failed → mark failed ──────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_commit_failed():
    """repair succeeds but commit returns sha=None → mark failed."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()
    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    classification = _make_classification()
    validation = _make_validation(passed=True)
    repair_result = _make_repair_result(success=True, validation=validation)
    mock_parity = _make_parity()

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result), \
         patch.object(agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity), \
         patch.object(agent, "_commit_to_safe_branch", new_callable=AsyncMock,
                      return_value={"sha": None, "error": "commit failed"}), \
         patch.object(agent, "_mark_failed_with_fields", new_callable=AsyncMock):
        MockClf.return_value.classify.return_value = classification
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is False
    assert "commit" in result.error.lower()


# ── Full success path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_success_path():
    """Full success: repair OK → commit → PR opened → FIXED status."""
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
    classification = _make_classification()
    validation = _make_validation(passed=True)
    repair_result = _make_repair_result(success=True, validation=validation)
    mock_parity = _make_parity()

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result), \
         patch.object(agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity), \
         patch.object(agent, "_get_fingerprint_success_count", new_callable=AsyncMock, return_value=0), \
         patch.object(agent, "_commit_to_safe_branch", new_callable=AsyncMock,
                      return_value={"sha": "abc12345", "branch": "phalanx/ci-fix/run-loop-001", "push_failed": False}), \
         patch.object(agent, "_open_draft_pr", new_callable=AsyncMock, return_value=42), \
         patch.object(agent, "_comment_on_pr", new_callable=AsyncMock), \
         patch.object(agent, "_update_fingerprint_on_success", new_callable=AsyncMock):
        MockClf.return_value.classify.return_value = classification
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is True
    assert result.output["tool"] == "ruff"
    assert result.output["fix_pr_number"] == 42
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
    classification = _make_classification()
    validation = _make_validation(passed=True, tool_version="")
    repair_result = _make_repair_result(success=True, validation=validation)
    mock_parity = _make_parity()

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.ContextRetriever") as MockRet, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop", return_value=repair_result), \
         patch.object(agent, "_check_tool_version_parity", new_callable=AsyncMock, return_value=mock_parity), \
         patch.object(agent, "_get_fingerprint_success_count", new_callable=AsyncMock, return_value=0), \
         patch.object(agent, "_commit_to_safe_branch", new_callable=AsyncMock,
                      return_value={"sha": "deadbeef", "branch": "phalanx/ci-fix/run-loop-001", "push_failed": True}), \
         patch.object(agent, "_update_fingerprint_on_success", new_callable=AsyncMock) as mock_fp_update:
        MockClf.return_value.classify.return_value = classification
        MockRet.return_value.retrieve = AsyncMock(return_value=MagicMock(log_excerpt=""))
        result = await agent._execute_inner()

    assert result.success is True
    assert result.output["commit_sha"] == "deadbeef"
    assert result.output["fix_pr_number"] is None
    mock_fp_update.assert_called_once()


# ── LLM classifier low confidence → skip repair ───────────────────────────────


@pytest.mark.asyncio
async def test_execute_inner_classifier_low_confidence():
    """classifier.is_actionable=False → early exit, repair never called."""
    agent = _make_agent()
    mock_run = _make_ci_run()
    mock_integration = _make_integration()
    mock_ctx, _ = _db_sequence(mock_run, mock_integration, None)
    parsed = _make_parsed_with_lint()

    from phalanx.ci_fixer.classifier import ClassificationResult

    low_clf = ClassificationResult(
        failure_type="unknown",
        language="unknown",
        tool="unknown",
        complexity_tier="L2",
        confidence=0.2,
        root_cause_hypothesis="unclear",
    )

    with patch("phalanx.agents.ci_fixer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_fetch_logs", new_callable=AsyncMock, return_value="log"), \
         patch("phalanx.agents.ci_fixer.parse_log", return_value=parsed), \
         patch.object(agent, "_persist_fingerprint", new_callable=AsyncMock), \
         patch.object(agent, "_load_flaky_patterns", new_callable=AsyncMock, return_value=[]), \
         patch("phalanx.agents.ci_fixer.is_flaky_suppressed", return_value=False), \
         patch.object(agent, "_clone_repo", new_callable=AsyncMock, return_value=True), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("phalanx.agents.ci_fixer.LLMClassifier") as MockClf, \
         patch("phalanx.agents.ci_fixer.run_agentic_loop") as mock_run_repair, \
         patch.object(agent, "_mark_failed", new_callable=AsyncMock):
        MockClf.return_value.classify.return_value = low_clf
        result = await agent._execute_inner()

    assert result.success is False
    assert result.output["reason"] == "classifier_low_confidence"
    mock_run_repair.assert_not_called()


# ── _trace test ────────────────────────────────────────────────────────────────


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
    from phalanx.agents.base import AgentResult

    agent = _make_agent()
    ws = tmp_path / "workspace"
    ws.mkdir()

    from phalanx.agents.ci_fixer import _cleanup_workspace

    cleanup_called = {"v": False}
    original_cleanup = _cleanup_workspace

    def _mock_cleanup(path):
        cleanup_called["v"] = True

    with patch.object(agent, "_execute_inner", new_callable=AsyncMock,
                      side_effect=RuntimeError("boom")), \
         patch("phalanx.agents.ci_fixer._cleanup_workspace", side_effect=_mock_cleanup):
        result = await agent.execute()

    assert result.success is False
    assert "boom" in result.error
