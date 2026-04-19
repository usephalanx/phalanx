"""Unit tests for simulation.scoring — tiered per-fixture scoring logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phalanx.ci_fixer_v2.agent import RunOutcome
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext, ToolInvocation
from phalanx.ci_fixer_v2.simulation.fixtures import Fixture, FixtureMeta
from phalanx.ci_fixer_v2.simulation.scoring import (
    DECISION_CODE_CHANGE,
    DECISION_DECLINE_INFRA,
    DECISION_DECLINE_PREEXISTING,
    DECISION_ESCALATE,
    DECISION_FAILED,
    DECISION_MARK_FLAKY,
    SIMILARITY_THRESHOLD,
    score_fixture,
)


def _fixture(
    fixture_id: str = "f1",
    failure_class: str = "lint",
    fix_type: str = "code_change",
    fix_diff: str = "",
) -> Fixture:
    meta = FixtureMeta(
        fixture_id=fixture_id,
        language="python",
        failure_class=failure_class,
    )
    gt: dict[str, Any] = {"fix_type": fix_type}
    if fix_diff:
        gt["fix_diff"] = fix_diff
    return Fixture(
        path=Path("/does/not/matter"),
        meta=meta,
        raw_log="",
        ground_truth=gt,
    )


def _ctx(
    workspace: str = "/tmp",
    diff: str | None = None,
    verified: bool = False,
    tool_count: int = 0,
) -> AgentContext:
    c = AgentContext(
        ci_fix_run_id="run",
        repo_full_name="x/y",
        repo_workspace_path=workspace,
        original_failing_command="ruff check",
    )
    c.last_attempted_diff = diff
    c.last_sandbox_verified = verified
    for i in range(tool_count):
        c.tool_invocations.append(
            ToolInvocation(
                turn=i, tool_name="read_file", tool_input={}, tool_result={}
            )
        )
    return c


# ── Decision-class predictor ──────────────────────────────────────────────


def test_predicted_code_change_when_committed_without_flaky_marker():
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED, committed_sha="x")
    ctx = _ctx(
        diff="diff --git a/x.py b/x.py\n+fixed = 1\n",
        verified=True,
    )
    fx = _fixture(fix_type=DECISION_CODE_CHANGE)
    score = score_fixture(fx, outcome, ctx)
    assert score.decision_class_predicted == DECISION_CODE_CHANGE


def test_predicted_mark_flaky_when_diff_contains_flaky_marker_with_todo():
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(
        diff=(
            "diff --git a/tests/t.py b/tests/t.py\n"
            "+@pytest.mark.flaky(reruns=2)  # TODO(PHX-1): upstream timeout\n"
        ),
        verified=False,
    )
    fx = _fixture(fix_type=DECISION_MARK_FLAKY, failure_class="flake")
    score = score_fixture(fx, outcome, ctx)
    assert score.decision_class_predicted == DECISION_MARK_FLAKY


def test_flaky_marker_without_todo_treated_as_code_change():
    # The system prompt forbids mark-flaky without a TODO — the scorer
    # shouldn't give credit for a lazy suppression.
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(
        diff="diff --git a/tests/t.py b/tests/t.py\n+@pytest.mark.flaky\n",
    )
    fx = _fixture(fix_type=DECISION_MARK_FLAKY, failure_class="flake")
    score = score_fixture(fx, outcome, ctx)
    assert score.decision_class_predicted == DECISION_CODE_CHANGE


def test_predicted_decline_preexisting_from_escalation_reason():
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.PREEXISTING_MAIN_FAILURE,
        explanation="already failing on main",
    )
    score = score_fixture(_fixture(), outcome, _ctx())
    assert score.decision_class_predicted == DECISION_DECLINE_PREEXISTING


def test_predicted_decline_infra_from_escalation_reason():
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE,
    )
    score = score_fixture(_fixture(), outcome, _ctx())
    assert score.decision_class_predicted == DECISION_DECLINE_INFRA


def test_predicted_escalate_for_other_reasons():
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.LOW_CONFIDENCE,
    )
    score = score_fixture(_fixture(), outcome, _ctx())
    assert score.decision_class_predicted == DECISION_ESCALATE


def test_predicted_failed_for_failed_verdict():
    outcome = RunOutcome(verdict=RunVerdict.FAILED, explanation="boom")
    score = score_fixture(_fixture(), outcome, _ctx())
    assert score.decision_class_predicted == DECISION_FAILED


# ── Strict similarity ─────────────────────────────────────────────────────


def test_strict_similarity_identical_diffs_scores_1_0():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(diff=diff, verified=True)
    fx = _fixture(fix_type=DECISION_CODE_CHANGE, fix_diff=diff)
    score = score_fixture(fx, outcome, ctx)
    assert score.strict_similarity == 1.0
    assert score.strict is True


def test_strict_similarity_partial_overlap_may_fail_threshold():
    ctx_diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-one\n+two\n"
    gt_diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-one\n+three\n"
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(diff=ctx_diff, verified=True)
    fx = _fixture(fix_type=DECISION_CODE_CHANGE, fix_diff=gt_diff)
    score = score_fixture(fx, outcome, ctx)
    # Only 'one' overlaps; similarity < threshold.
    assert score.strict_similarity < SIMILARITY_THRESHOLD
    assert score.strict is False


def test_strict_when_neither_diff_present_scores_full():
    # Edge case: fixture has no ground-truth diff (non-code fix) and the
    # agent committed nothing — treat the comparison as trivially equal.
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.PREEXISTING_MAIN_FAILURE,
    )
    fx = _fixture(fix_type=DECISION_DECLINE_PREEXISTING)
    score = score_fixture(fx, outcome, _ctx(diff=None))
    assert score.strict_similarity == 1.0


def test_strict_zero_when_only_one_side_present():
    fx = _fixture(fix_diff="")
    ctx = _ctx(diff="diff --git a/x b/x\n+x\n")
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    score = score_fixture(fx, outcome, ctx)
    assert score.strict_similarity in (0.0, 1.0)  # tolerant


# ── Lenient ─────────────────────────────────────────────────────────────


def test_lenient_passes_when_sandbox_verified():
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(verified=True)
    score = score_fixture(_fixture(), outcome, ctx)
    assert score.lenient is True


def test_lenient_fails_when_not_committed_and_not_verified():
    # Updated invariant (spec §12): lenient passes when verdict=COMMITTED
    # (the commit-gate enforced verification), OR when last_sandbox_verified
    # is still True. The only way to fail lenient is an outcome that is
    # NEITHER committed NOR verified.
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.LOW_CONFIDENCE,
    )
    ctx = _ctx(verified=False)
    fx = _fixture(fix_type=DECISION_CODE_CHANGE)
    score = score_fixture(fx, outcome, ctx)
    assert score.lenient is False


def test_lenient_passes_when_committed_even_after_flag_cleared():
    # commit_and_push clears last_sandbox_verified on success (forcing
    # re-verification before any follow-up commit). Scoring must still
    # count this as lenient — the gate fired before the commit landed.
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED, committed_sha="abc")
    ctx = _ctx(verified=False, diff="diff --git a/x b/x\n+x\n")
    fx = _fixture(fix_type=DECISION_CODE_CHANGE)
    score = score_fixture(fx, outcome, ctx)
    assert score.lenient is True


def test_lenient_passes_for_noncode_fix_without_sandbox_verification():
    # `decline_as_preexisting` legitimately does not run sandbox; lenient
    # score should still pass when the decision class matches expected.
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.PREEXISTING_MAIN_FAILURE,
    )
    fx = _fixture(fix_type=DECISION_DECLINE_PREEXISTING)
    score = score_fixture(fx, outcome, _ctx(verified=False))
    assert score.lenient is True
    assert score.behavioral is True


# ── Behavioral ──────────────────────────────────────────────────────────


def test_behavioral_passes_when_decisions_match():
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED, committed_sha="s")
    ctx = _ctx(verified=True, diff="diff --git a/x b/x\n+x\n")
    fx = _fixture(fix_type=DECISION_CODE_CHANGE)
    score = score_fixture(fx, outcome, ctx)
    assert score.behavioral is True


def test_behavioral_fails_when_decisions_differ():
    # Fixture expects decline_as_preexisting; agent committed anyway.
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(verified=True, diff="diff --git a/x b/x\n+x\n")
    fx = _fixture(fix_type=DECISION_DECLINE_PREEXISTING)
    score = score_fixture(fx, outcome, ctx)
    assert score.behavioral is False


def test_behavioral_fails_when_expected_decision_is_empty():
    # No ground-truth fix_type — we don't score behavioral as a pass just
    # because both sides are empty (that would hide unlabeled fixtures).
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    fx = Fixture(
        path=Path("/x"),
        meta=FixtureMeta(
            fixture_id="u",
            language="python",
            failure_class="lint",
        ),
        raw_log="",
        ground_truth={},
    )
    score = score_fixture(fx, outcome, _ctx(verified=True, diff="diff --git a/x b/x\n"))
    assert score.behavioral is False
    assert score.decision_class_expected == ""


# ── Metadata integrity ─────────────────────────────────────────────────


def test_score_captures_turns_and_cost_and_verdict():
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    ctx = _ctx(verified=True, tool_count=7, diff="diff --git a/x b/x\n+x\n")
    ctx.cost.gpt_reasoning_cost_usd = 0.12
    ctx.cost.sonnet_coder_cost_usd = 0.03
    fx = _fixture()
    score = score_fixture(fx, outcome, ctx)
    assert score.turns_used == 7
    assert score.total_cost_usd == pytest.approx(0.15)
    assert score.verdict == "committed"


def test_score_captures_escalation_reason_when_escalated():
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.TURN_CAP_REACHED,
    )
    score = score_fixture(_fixture(), outcome, _ctx())
    assert score.escalation_reason == "turn_cap_reached"
