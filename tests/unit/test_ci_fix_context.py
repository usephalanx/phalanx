"""
Tests for phalanx.ci_fixer.context — CIFixContext shared pipeline state.

Coverage targets:
  - CIFixContext: init, to_dict, from_dict, complete, is_complete, current_stage
  - StructuredFailure, ClassifiedFailure, ReproductionResult, VerifiedPatch, VerificationResult
  - Serialization round-trip fidelity
  - Edge cases: None fields, empty lists, partial population
"""

from __future__ import annotations

import json

import pytest

from phalanx.ci_fixer.context import (
    CIFixContext,
    ClassifiedFailure,
    ReproductionResult,
    StructuredFailure,
    VerificationResult,
    VerifiedPatch,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_ctx(**kwargs) -> CIFixContext:
    defaults = {
        "ci_fix_run_id": "run-123",
        "repo": "owner/repo",
        "branch": "feature/foo",
        "commit_sha": "abc123",
        "original_build_id": "build-456",
    }
    defaults.update(kwargs)
    return CIFixContext(**defaults)


# ── CIFixContext basics ───────────────────────────────────────────────────────


def test_context_init_defaults():
    ctx = _make_ctx()
    assert ctx.ci_fix_run_id == "run-123"
    assert ctx.repo == "owner/repo"
    assert ctx.branch == "feature/foo"
    assert ctx.commit_sha == "abc123"
    assert ctx.original_build_id == "build-456"
    assert ctx.structured_failure is None
    assert ctx.classified_failure is None
    assert ctx.reproduction_result is None
    assert ctx.verified_patch is None
    assert ctx.verification_result is None
    assert ctx.fix_commit_sha is None
    assert ctx.fix_pr_number is None
    assert ctx.fix_branch is None
    assert ctx.pr_was_existing is False
    assert ctx.final_status == "in_progress"
    assert ctx.pr_comment_posted is False
    assert ctx.error is None
    assert ctx.started_at is not None


def test_context_is_complete_initial():
    ctx = _make_ctx()
    assert ctx.is_complete is False


def test_context_complete_fixed():
    ctx = _make_ctx()
    ctx.complete("fixed")
    assert ctx.is_complete is True
    assert ctx.final_status == "fixed"
    assert ctx.completed_at is not None
    assert ctx.error is None


def test_context_complete_failed_with_error():
    ctx = _make_ctx()
    ctx.complete("failed", error="something went wrong")
    assert ctx.final_status == "failed"
    assert ctx.error == "something went wrong"


def test_context_complete_escalated():
    ctx = _make_ctx()
    ctx.complete("escalated")
    assert ctx.final_status == "escalated"
    assert ctx.is_complete is True


def test_context_complete_flaky():
    ctx = _make_ctx()
    ctx.complete("flaky")
    assert ctx.final_status == "flaky"


def test_context_complete_env_mismatch():
    ctx = _make_ctx()
    ctx.complete("env_mismatch")
    assert ctx.final_status == "env_mismatch"


# ── current_stage property ────────────────────────────────────────────────────


def test_current_stage_started():
    ctx = _make_ctx()
    assert ctx.current_stage == "started"


def test_current_stage_parsed():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="ruff", failure_type="lint", reproducer_cmd="ruff check ."
    )
    assert ctx.current_stage == "parsed"


def test_current_stage_classified():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="ruff", failure_type="lint", reproducer_cmd="ruff check ."
    )
    ctx.classified_failure = ClassifiedFailure(
        tier="L1_auto", root_cause="unused import", stack="python"
    )
    assert ctx.current_stage == "classified"


def test_current_stage_sandbox_ready():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="ruff", failure_type="lint", reproducer_cmd="ruff check ."
    )
    ctx.classified_failure = ClassifiedFailure(
        tier="L1_auto", root_cause="unused import", stack="python"
    )
    ctx.sandbox_id = "container-abc"
    assert ctx.current_stage == "sandbox_ready"


def test_current_stage_reproduced():
    ctx = _make_ctx()
    ctx.reproduction_result = ReproductionResult(verdict="confirmed")
    assert ctx.current_stage == "reproduced"


def test_current_stage_patched():
    ctx = _make_ctx()
    ctx.reproduction_result = ReproductionResult(verdict="confirmed")
    ctx.verified_patch = VerifiedPatch(files_modified=["src/foo.py"], success=True)
    assert ctx.current_stage == "patched"


def test_current_stage_verified():
    ctx = _make_ctx()
    ctx.reproduction_result = ReproductionResult(verdict="confirmed")
    ctx.verified_patch = VerifiedPatch(files_modified=["src/foo.py"], success=True)
    ctx.verification_result = VerificationResult(verdict="passed")
    assert ctx.current_stage == "verified"


def test_current_stage_committed():
    ctx = _make_ctx()
    ctx.fix_commit_sha = "deadbeef"
    assert ctx.current_stage == "committed"


# ── Serialization round-trip ──────────────────────────────────────────────────


def test_to_dict_minimal():
    ctx = _make_ctx()
    d = ctx.to_dict()
    assert d["ci_fix_run_id"] == "run-123"
    assert d["repo"] == "owner/repo"
    assert d["structured_failure"] is None
    assert d["final_status"] == "in_progress"


def test_to_dict_with_all_agents_populated():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="ruff",
        failure_type="lint",
        reproducer_cmd="ruff check .",
        errors=[{"file": "foo.py", "line": 1, "code": "F401"}],
        failing_files=["foo.py"],
        log_excerpt="foo.py:1:1: F401 ...",
        confidence=0.95,
    )
    ctx.classified_failure = ClassifiedFailure(
        tier="L1_auto",
        root_cause="unused import",
        stack="python",
        confidence=0.9,
    )
    ctx.reproduction_result = ReproductionResult(
        verdict="confirmed",
        exit_code=1,
        output="F401 ...",
        reproducer_cmd="ruff check .",
    )
    ctx.verified_patch = VerifiedPatch(
        files_modified=["foo.py"],
        validation_cmd="ruff check foo.py",
        validation_output="All checks passed!",
        success=True,
        turns_used=3,
    )
    ctx.verification_result = VerificationResult(
        verdict="passed",
        output="pytest passed",
        cmd_run="pytest tests/",
    )
    ctx.fix_commit_sha = "abc123"
    ctx.fix_pr_number = 42
    ctx.fix_branch = "phalanx/ci-fix/run-123"
    ctx.complete("fixed")

    d = ctx.to_dict()
    assert d["structured_failure"]["tool"] == "ruff"
    assert d["classified_failure"]["tier"] == "L1_auto"
    assert d["reproduction_result"]["verdict"] == "confirmed"
    assert d["verified_patch"]["success"] is True
    assert d["verification_result"]["verdict"] == "passed"
    assert d["fix_commit_sha"] == "abc123"
    assert d["fix_pr_number"] == 42
    assert d["final_status"] == "fixed"


def test_from_dict_round_trip_minimal():
    ctx = _make_ctx()
    d = ctx.to_dict()
    restored = CIFixContext.from_dict(d)
    assert restored.ci_fix_run_id == ctx.ci_fix_run_id
    assert restored.repo == ctx.repo
    assert restored.structured_failure is None
    assert restored.final_status == "in_progress"


def test_from_dict_round_trip_full():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="mypy", failure_type="type_error", reproducer_cmd="mypy ."
    )
    ctx.classified_failure = ClassifiedFailure(
        tier="L1_auto", root_cause="type mismatch", stack="python"
    )
    ctx.reproduction_result = ReproductionResult(verdict="skipped")
    ctx.verified_patch = VerifiedPatch(files_modified=["src/types.py"], success=True)
    ctx.verification_result = VerificationResult(verdict="skipped")
    ctx.fix_commit_sha = "sha456"
    ctx.fix_pr_number = 7
    ctx.fix_pr_url = "https://github.com/owner/repo/pull/7"
    ctx.fix_branch = "phalanx/ci-fix/run-123"
    ctx.pr_was_existing = True
    ctx.complete("fixed")

    d = ctx.to_dict()
    restored = CIFixContext.from_dict(d)

    assert restored.structured_failure.tool == "mypy"
    assert restored.classified_failure.tier == "L1_auto"
    assert restored.reproduction_result.verdict == "skipped"
    assert restored.verified_patch.success is True
    assert restored.verification_result.verdict == "skipped"
    assert restored.fix_commit_sha == "sha456"
    assert restored.fix_pr_number == 7
    assert restored.fix_pr_url == "https://github.com/owner/repo/pull/7"
    assert restored.pr_was_existing is True
    assert restored.final_status == "fixed"
    assert restored.is_complete is True


def test_json_serializable():
    ctx = _make_ctx()
    ctx.structured_failure = StructuredFailure(
        tool="ruff", failure_type="lint", reproducer_cmd="ruff check ."
    )
    ctx.complete("fixed")
    # Must not raise
    serialized = json.dumps(ctx.to_dict())
    restored = CIFixContext.from_dict(json.loads(serialized))
    assert restored.final_status == "fixed"


def test_from_dict_missing_optional_fields():
    """from_dict should handle dicts missing optional fields gracefully."""
    d = {
        "ci_fix_run_id": "run-xyz",
        "repo": "owner/repo",
        "branch": "main",
        "commit_sha": "abc",
        "original_build_id": "build-1",
    }
    ctx = CIFixContext.from_dict(d)
    assert ctx.ci_fix_run_id == "run-xyz"
    assert ctx.structured_failure is None
    assert ctx.fix_pr_number is None
    assert ctx.final_status == "in_progress"
    assert ctx.pr_was_existing is False


# ── Sub-object tests ──────────────────────────────────────────────────────────


def test_structured_failure_defaults():
    sf = StructuredFailure(tool="ruff", failure_type="lint", reproducer_cmd="ruff check .")
    assert sf.errors == []
    assert sf.failing_files == []
    assert sf.log_excerpt == ""
    assert sf.confidence == 1.0


def test_classified_failure_l2_escalate():
    cf = ClassifiedFailure(
        tier="L2_escalate",
        root_cause="test regression",
        stack="python",
        confidence=0.3,
        escalation_reason="test failure requires engineer judgment",
    )
    assert cf.tier == "L2_escalate"
    assert cf.escalation_reason == "test failure requires engineer judgment"


def test_reproduction_result_all_verdicts():
    for verdict in ("confirmed", "flaky", "env_mismatch", "timeout", "skipped"):
        r = ReproductionResult(verdict=verdict)
        assert r.verdict == verdict


def test_verified_patch_defaults():
    vp = VerifiedPatch()
    assert vp.files_modified == []
    assert vp.success is False
    assert vp.turns_used == 0


def test_verification_result_all_verdicts():
    for verdict in ("passed", "failed", "skipped", "timeout"):
        vr = VerificationResult(verdict=verdict)
        assert vr.verdict == verdict


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_context_pr_was_existing_default_false():
    ctx = _make_ctx()
    assert ctx.pr_was_existing is False


def test_context_set_pr_was_existing():
    ctx = _make_ctx()
    ctx.pr_was_existing = True
    d = ctx.to_dict()
    assert d["pr_was_existing"] is True
    restored = CIFixContext.from_dict(d)
    assert restored.pr_was_existing is True


def test_context_sandbox_fields():
    ctx = _make_ctx()
    ctx.sandbox_id = "container-xyz"
    ctx.sandbox_stack = "python"
    d = ctx.to_dict()
    restored = CIFixContext.from_dict(d)
    assert restored.sandbox_id == "container-xyz"
    assert restored.sandbox_stack == "python"


def test_context_error_persists_through_round_trip():
    ctx = _make_ctx()
    ctx.complete("failed", error="ruff not found in sandbox")
    d = ctx.to_dict()
    restored = CIFixContext.from_dict(d)
    assert restored.error == "ruff not found in sandbox"


def test_context_started_at_is_set():
    ctx = _make_ctx()
    assert ctx.started_at
    # Should be a valid ISO datetime string
    from datetime import datetime

    datetime.fromisoformat(ctx.started_at)
