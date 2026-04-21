"""Unit tests for the loop-level escalation evidence gate.

Some escalation reasons are load-bearing for downstream routing:
  - infra_failure_out_of_scope → ops oncall page
  - preexisting_main_failure    → flag that main itself is broken

If the LLM picks one of those reasons without evidence in the tool
trace, a human is getting paged for the wrong thing. The gate coerces
such cases to LOW_CONFIDENCE and logs the attempt. Tests here lock
down that behavior.

Mirrors the shape of `verification_gate_violation` on commit_and_push:
invariants live in the loop, not in the prompt.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import (
    LLMResponse,
    LLMToolUse,
    _force_low_confidence_if_no_evidence,
    _has_infra_failure_evidence,
    _has_preexisting_main_failure_evidence,
    run_ci_fix_v2,
)
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext, ToolInvocation
from phalanx.ci_fixer_v2.tools import base as tools_base


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx() -> AgentContext:
    return AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
    )


# ─────────────────────────────────────────────────────────────────
# Pure-function evidence checkers
# ─────────────────────────────────────────────────────────────────


class TestInfraEvidence:
    def test_no_tool_invocations_is_no_evidence(self):
        ctx = _ctx()
        assert _has_infra_failure_evidence(ctx) is False

    def test_fetch_ci_log_errored_counts(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={"job_id": "x"},
                tool_result=None,
                error="github_api_401",
            )
        )
        assert _has_infra_failure_evidence(ctx) is True

    def test_fetch_ci_log_success_is_not_evidence(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={"job_id": "x"},
                tool_result={"log": "some content", "lines": 200},
                error=None,
            )
        )
        assert _has_infra_failure_evidence(ctx) is False

    def test_sandbox_exit_127_counts(self):
        """exit 127 = command not found, which is sandbox/env infra."""
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="run_in_sandbox",
                tool_input={"command": "mypy ."},
                tool_result={"exit_code": 127, "stderr": "mypy: command not found"},
                error=None,
            )
        )
        assert _has_infra_failure_evidence(ctx) is True

    def test_sandbox_exit_1_is_not_evidence(self):
        """exit 1 is a real failure the agent should diagnose + fix."""
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="run_in_sandbox",
                tool_input={"command": "pytest"},
                tool_result={"exit_code": 1, "stderr": "1 failed, 3 passed"},
                error=None,
            )
        )
        assert _has_infra_failure_evidence(ctx) is False

    def test_sandbox_timeout_counts(self):
        """Sandbox timeout is an infra signal — agent can't observe."""
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="run_in_sandbox",
                tool_input={"command": "pytest"},
                tool_result={"exit_code": 124, "timed_out": True},
                error=None,
            )
        )
        assert _has_infra_failure_evidence(ctx) is True


class TestPreexistingMainEvidence:
    def test_no_tool_invocations_is_no_evidence(self):
        ctx = _ctx()
        assert _has_preexisting_main_failure_evidence(ctx) is False

    def test_ci_history_aggregate_counter_counts(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="get_ci_history",
                tool_input={},
                tool_result={"recent_main_failures": 3},
                error=None,
            )
        )
        assert _has_preexisting_main_failure_evidence(ctx) is True

    def test_ci_history_zero_counter_is_not_evidence(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="get_ci_history",
                tool_input={},
                tool_result={"recent_main_failures": 0},
                error=None,
            )
        )
        assert _has_preexisting_main_failure_evidence(ctx) is False

    def test_ci_history_runs_list_with_main_failure_counts(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="get_ci_history",
                tool_input={},
                tool_result={
                    "runs": [
                        {"branch": "main", "conclusion": "success"},
                        {"branch": "main", "conclusion": "failure"},
                    ]
                },
                error=None,
            )
        )
        assert _has_preexisting_main_failure_evidence(ctx) is True

    def test_ci_history_runs_only_feature_branches_is_not_evidence(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="get_ci_history",
                tool_input={},
                tool_result={
                    "runs": [
                        {"branch": "feat/x", "conclusion": "failure"},
                        {"branch": "fix/y", "conclusion": "failure"},
                    ]
                },
                error=None,
            )
        )
        assert _has_preexisting_main_failure_evidence(ctx) is False


class TestForceLowConfidence:
    def test_low_confidence_passes_through(self):
        """Reasons without a gate are untouched."""
        assert (
            _force_low_confidence_if_no_evidence(
                _ctx(), EscalationReason.LOW_CONFIDENCE
            )
            is EscalationReason.LOW_CONFIDENCE
        )

    def test_ambiguous_fix_passes_through(self):
        assert (
            _force_low_confidence_if_no_evidence(
                _ctx(), EscalationReason.AMBIGUOUS_FIX
            )
            is EscalationReason.AMBIGUOUS_FIX
        )

    def test_infra_without_evidence_forced(self):
        assert (
            _force_low_confidence_if_no_evidence(
                _ctx(), EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE
            )
            is EscalationReason.LOW_CONFIDENCE
        )

    def test_infra_with_evidence_preserved(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="run_in_sandbox",
                tool_input={},
                tool_result={"exit_code": 127},
                error=None,
            )
        )
        assert (
            _force_low_confidence_if_no_evidence(
                ctx, EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE
            )
            is EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE
        )

    def test_preexisting_main_without_evidence_forced(self):
        assert (
            _force_low_confidence_if_no_evidence(
                _ctx(), EscalationReason.PREEXISTING_MAIN_FAILURE
            )
            is EscalationReason.LOW_CONFIDENCE
        )

    def test_preexisting_main_with_evidence_preserved(self):
        ctx = _ctx()
        ctx.tool_invocations.append(
            ToolInvocation(
                turn=0,
                tool_name="get_ci_history",
                tool_input={},
                tool_result={"recent_main_failures": 1},
                error=None,
            )
        )
        assert (
            _force_low_confidence_if_no_evidence(
                ctx, EscalationReason.PREEXISTING_MAIN_FAILURE
            )
            is EscalationReason.PREEXISTING_MAIN_FAILURE
        )


# ─────────────────────────────────────────────────────────────────
# End-to-end loop behavior
# ─────────────────────────────────────────────────────────────────


async def test_loop_forces_low_confidence_when_llm_lies_about_infra():
    """Simulates the exact regression the gate was built for: agent
    diagnoses + reproduces a real test failure, then escalates with
    infra_failure_out_of_scope even though no infra signal exists in
    the tool trace. The loop MUST rewrite to LOW_CONFIDENCE."""
    ctx = _ctx()

    # Script: turn 0 runs ruff in sandbox (finds real failure, exit 1),
    # turn 1 gives up and escalates with infra_failure (no justification).
    calls = {"n": 0}

    async def scripted(_messages):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return LLMResponse(
                stop_reason="tool_use",
                tool_uses=[
                    LLMToolUse(
                        id="s1",
                        name="run_in_sandbox",
                        input={"command": "ruff check ."},
                    )
                ],
            )
        return LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="e1",
                    name="escalate",
                    input={
                        "reason": "infra_failure_out_of_scope",
                        "explanation": "sandbox is broken",
                    },
                )
            ],
        )

    # Make run_in_sandbox return exit 1 (not 127 — it's a REAL failure,
    # not infra). The gate should notice there's no infra evidence.
    from phalanx.ci_fixer_v2.tools import action as action_mod

    async def fake_exec(_argv, _timeout):
        return (1, "rule E501 violated", "", False, 0.1)

    from unittest.mock import patch

    # Provision a fake sandbox_container_id so run_in_sandbox tool path
    # doesn't short-circuit.
    ctx.sandbox_container_id = "fake-container"

    with patch.object(action_mod, "_exec_argv", fake_exec):
        outcome = await run_ci_fix_v2(ctx, scripted)

    assert outcome.verdict == RunVerdict.ESCALATED
    # Gate rewrites the reason — downstream triage routes as generic
    # uncertainty, not as an infra page.
    assert outcome.escalation_reason == EscalationReason.LOW_CONFIDENCE


async def test_loop_preserves_infra_reason_when_evidence_exists():
    """When the tool trace genuinely contains an exit-127 (command not
    found), infra_failure_out_of_scope is justified and should pass
    through unchanged."""
    ctx = _ctx()
    ctx.sandbox_container_id = "fake-container"
    calls = {"n": 0}

    async def scripted(_messages):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return LLMResponse(
                stop_reason="tool_use",
                tool_uses=[
                    LLMToolUse(
                        id="s1",
                        name="run_in_sandbox",
                        input={"command": "mypy ."},
                    )
                ],
            )
        return LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="e1",
                    name="escalate",
                    input={
                        "reason": "infra_failure_out_of_scope",
                        "explanation": "mypy missing from sandbox",
                    },
                )
            ],
        )

    from phalanx.ci_fixer_v2.tools import action as action_mod

    async def fake_exec(_argv, _timeout):
        return (127, "", "sh: mypy: not found", False, 0.05)

    from unittest.mock import patch

    with patch.object(action_mod, "_exec_argv", fake_exec):
        outcome = await run_ci_fix_v2(ctx, scripted)

    assert outcome.verdict == RunVerdict.ESCALATED
    assert (
        outcome.escalation_reason == EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE
    )
