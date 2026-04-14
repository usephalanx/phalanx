"""
Slice 2 unit tests — RepairAgent FSM.

Proves all 7 critical FSM paths terminate correctly.
No LLM calls, no DB, no subprocesses — all mocked.

Scenarios:
  1.  No errors → GIVE_UP immediately, LLM never called
  2.  L1 F401   → deterministic fix → VALIDATE → SUBMIT, LLM never called
  3.  Iter 1 low confidence → ESCALATE
  4.  Iter 1 validation fail → RETRY → iter 2 pass → SUBMIT
  5.  All 3 iters validation fail → GIVE_UP max_iterations_exhausted
  6.  Total delta too large → GIVE_UP
  7.  History replay succeeds → SUBMIT, LLM never called
  8.  Too many files in patch → GIVE_UP
  9.  No patches returned (empty list) → ESCALATE iter 1
  10. L1 all test files → falls through to GENERATE_PATCH
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.analyst import FilePatch, FixPlan
from phalanx.ci_fixer.classifier import ClassificationResult
from phalanx.ci_fixer.context_retriever import ContextBundle, SimilarFix
from phalanx.ci_fixer.log_parser import LintError, ParsedLog, TestFailure
from phalanx.ci_fixer.repair_agent import RepairResult, RepairState, run_repair
from phalanx.ci_fixer.validator import ValidationResult


# ── Helpers ────────────────────────────────────────────────────────────────────


def _classification(tier="L2", failure_type="lint", confidence=0.9) -> ClassificationResult:
    return ClassificationResult(
        failure_type=failure_type,
        language="python",
        tool="ruff",
        complexity_tier=tier,
        confidence=confidence,
        root_cause_hypothesis="test hypothesis",
    )


def _parsed_with_lint(file="src/foo.py", code="F401") -> ParsedLog:
    p = ParsedLog(tool="ruff")
    p.lint_errors = [LintError(file=file, line=1, col=1, code=code, message="unused")]
    return p


def _empty_parsed() -> ParsedLog:
    return ParsedLog(tool="unknown")


def _bundle(
    tmp_path: Path,
    parsed: ParsedLog | None = None,
    tier: str = "L2",
    similar_fixes: list | None = None,
    failure_type: str = "lint",
) -> ContextBundle:
    p = parsed or _parsed_with_lint()
    clf = _classification(tier=tier, failure_type=failure_type)
    return ContextBundle(
        parsed_log=p,
        classification=clf,
        workspace=tmp_path,
        failing_files=["src/foo.py"],
        file_contents={"src/foo.py": "import os\nx = 1\n"},
        similar_fixes=similar_fixes or [],
        log_excerpt="",
    )


def _valid_fix_plan(tmp_path: Path) -> FixPlan:
    return FixPlan(
        confidence="high",
        root_cause="unused import",
        patches=[FilePatch(
            path="src/foo.py",
            start_line=1,
            end_line=1,
            corrected_lines=["x = 1\n"],
            reason="remove unused import",
        )],
    )


def _low_confidence_plan() -> str:
    return json.dumps({
        "confidence": "low",
        "root_cause": "cannot determine fix",
        "patches": [],
        "needs_new_test": False,
    })


def _high_confidence_plan_json(tmp_path: Path) -> str:
    return json.dumps({
        "confidence": "high",
        "root_cause": "unused import os",
        "patches": [{
            "path": "src/foo.py",
            "start_line": 1,
            "end_line": 1,
            "corrected_lines": ["x = 1\n"],
            "reason": "removed unused import",
        }],
        "needs_new_test": False,
    })


# ── Scenario 1: No errors → GIVE_UP immediately ───────────────────────────────


def test_no_errors_gives_up_immediately(tmp_path):
    mock_llm = MagicMock()
    bundle = _bundle(tmp_path, parsed=_empty_parsed())

    result = run_repair(bundle, call_claude=mock_llm, workspace=tmp_path, original_parsed=_empty_parsed())

    assert result.success is False
    assert result.reason == "no_structured_errors"
    mock_llm.assert_not_called()
    assert RepairState.GIVE_UP in result.state_trace


# ── Scenario 2: L1 F401 → deterministic fix → SUBMIT, no LLM ─────────────────


def test_l1_f401_no_llm_called(tmp_path):
    # Write a real file so ruff --fix has something to work on
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("import os\nx = 1\n")

    mock_llm = MagicMock()
    parsed = ParsedLog(tool="ruff")
    parsed.lint_errors = [LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused")]
    bundle = _bundle(tmp_path, parsed=parsed, tier="L1")

    with patch("phalanx.ci_fixer.repair_agent.validate_fix") as mock_val:
        mock_val.return_value = ValidationResult(passed=True, tool="ruff", output="")
        with patch("phalanx.ci_fixer.repair_agent._try_l1_fix", return_value=["src/foo.py"]) as mock_l1:
            result = run_repair(bundle, call_claude=mock_llm, workspace=tmp_path, original_parsed=parsed)

    assert result.success is True
    assert result.used_l1_pattern is True
    mock_llm.assert_not_called()
    mock_l1.assert_called_once()


# ── Scenario 3: Iter 1 low confidence → ESCALATE ─────────────────────────────


def test_low_confidence_iter1_escalates(tmp_path):
    bundle = _bundle(tmp_path)

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = FixPlan(
            confidence="low", root_cause="unclear", patches=[],
        )
        result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=_parsed_with_lint())

    assert result.success is False
    assert result.escalate is True
    assert result.reason == "low_confidence"
    assert result.iteration == 1


# ── Scenario 4: Iter 1 fail → retry → iter 2 pass → SUBMIT ───────────────────


def test_validation_failure_triggers_retry_then_success(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("import os\nx = 1\n")

    bundle = _bundle(tmp_path)
    call_count = {"n": 0}

    good_plan = FixPlan(
        confidence="high",
        root_cause="unused import",
        patches=[FilePatch("src/foo.py", 1, 1, ["x = 1\n"])],
    )

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = good_plan
        with patch("phalanx.ci_fixer.repair_agent.validate_fix") as mock_val:
            mock_val.side_effect = [
                ValidationResult(passed=False, tool="ruff", output="still errors"),
                ValidationResult(passed=True,  tool="ruff", output=""),
            ]
            result = run_repair(
                bundle, call_claude=MagicMock(), workspace=tmp_path,
                original_parsed=_parsed_with_lint(), max_iterations=3,
            )

    assert result.success is True
    assert result.iteration == 2


# ── Scenario 5: All 3 iters fail → GIVE_UP ────────────────────────────────────


def test_max_iterations_gives_up(tmp_path):
    bundle = _bundle(tmp_path)

    good_plan = FixPlan(
        confidence="high",
        root_cause="error",
        patches=[FilePatch("src/foo.py", 1, 1, ["x = 1\n"])],
    )

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = good_plan
        with patch("phalanx.ci_fixer.repair_agent.validate_fix") as mock_val:
            mock_val.return_value = ValidationResult(passed=False, tool="ruff", output="still broken")
            result = run_repair(
                bundle, call_claude=MagicMock(), workspace=tmp_path,
                original_parsed=_parsed_with_lint(), max_iterations=3,
            )

    assert result.success is False
    assert result.reason == "max_iterations_exhausted"
    assert result.iteration == 3


# ── Scenario 6: Total delta too large → GIVE_UP ──────────────────────────────


def test_total_delta_too_large_gives_up(tmp_path):
    bundle = _bundle(tmp_path)

    # 31 lines changed → exceeds _MAX_TOTAL_DELTA=30
    big_plan = FixPlan(
        confidence="high",
        root_cause="big change",
        patches=[FilePatch("src/foo.py", 1, 1, [f"line{i}\n" for i in range(32)])],
    )

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = big_plan
        result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=_parsed_with_lint())

    assert result.success is False
    assert result.reason == "total_delta_too_large"


# ── Scenario 7: History replay succeeds → SUBMIT, no LLM ─────────────────────


def test_history_replay_skips_llm(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("import os\nx = 1\n")

    cached_patches = [{"path": "src/foo.py", "start_line": 1, "end_line": 1,
                       "corrected_lines": ["x = 1\n"], "reason": "cached"}]
    similar = SimilarFix(
        fingerprint_hash="abc123",
        tool="ruff",
        sample_errors="F401",
        last_good_patch_json=json.dumps(cached_patches),
        success_count=3,
        similarity_score=-1.0,
    )
    bundle = _bundle(tmp_path, similar_fixes=[similar])
    mock_llm = MagicMock()

    with patch("phalanx.ci_fixer.repair_agent.validate_fix") as mock_val:
        mock_val.return_value = ValidationResult(passed=True, tool="ruff", output="")
        result = run_repair(bundle, call_claude=mock_llm, workspace=tmp_path, original_parsed=_parsed_with_lint())

    assert result.success is True
    assert result.used_history is True
    mock_llm.assert_not_called()


# ── Scenario 8: Too many files in patch → GIVE_UP ────────────────────────────


def test_too_many_files_gives_up(tmp_path):
    bundle = _bundle(tmp_path)

    # 4 patches (> _MAX_FILES_CHANGED=3)
    many_files_plan = FixPlan(
        confidence="high",
        root_cause="multi-file",
        patches=[
            FilePatch(f"src/file{i}.py", 1, 1, ["x = 1\n"])
            for i in range(4)
        ],
    )

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = many_files_plan
        result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=_parsed_with_lint())

    assert result.success is False
    assert result.reason == "too_many_files_changed"


# ── Scenario 9: No patches returned → ESCALATE iter 1 ────────────────────────


def test_no_patches_iter1_escalates(tmp_path):
    bundle = _bundle(tmp_path)

    no_patch_plan = FixPlan(confidence="high", root_cause="cannot fix", patches=[])

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = no_patch_plan
        result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=_parsed_with_lint())

    assert result.success is False
    assert result.escalate is True
    assert result.reason == "no_patches"


# ── Scenario 10: L1 but all test files → falls through to GENERATE_PATCH ─────


def test_l1_all_test_files_falls_through_to_generate(tmp_path):
    parsed = ParsedLog(tool="ruff")
    parsed.lint_errors = [LintError(file="tests/unit/test_foo.py", line=1, col=1, code="F401", message="unused")]
    bundle = _bundle(tmp_path, parsed=parsed, tier="L1")

    good_plan = FixPlan(
        confidence="high",
        root_cause="unused import in non-test file",
        patches=[FilePatch("src/foo.py", 1, 1, ["x = 1\n"])],
    )

    with patch("phalanx.ci_fixer.repair_agent.RootCauseAnalyst") as MockAnalyst:
        MockAnalyst.return_value.analyze.return_value = good_plan
        with patch("phalanx.ci_fixer.repair_agent.validate_fix") as mock_val:
            mock_val.return_value = ValidationResult(passed=True, tool="ruff", output="")
            result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=parsed)

    # L1 pattern was skipped (all test files), fell through to LLM → success
    assert result.success is True
    assert result.used_l1_pattern is False


# ── State trace is always populated ──────────────────────────────────────────


def test_state_trace_populated(tmp_path):
    bundle = _bundle(tmp_path, parsed=_empty_parsed())
    result = run_repair(bundle, call_claude=MagicMock(), workspace=tmp_path, original_parsed=_empty_parsed())
    assert len(result.state_trace) >= 2
    assert result.state_trace[0] == RepairState.GATHER_CONTEXT
    assert result.state_trace[-1] == RepairState.GIVE_UP
