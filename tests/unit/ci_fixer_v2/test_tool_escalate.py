"""Unit tests for escalate + loop termination on escalate."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse, run_ci_fix_v2
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx(**overrides) -> AgentContext:
    defaults = dict(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


async def test_escalate_happy_path_records_draft_on_context():
    ctx = _ctx()
    tool = tools_base.get("escalate")
    draft = "diff --git a/app/api.py b/app/api.py\n+print('tried')\n"
    result = await tool.handler(
        ctx,
        {
            "reason": "low_confidence",
            "explanation": "two plausible fixes; author should pick",
            "draft_patch": draft,
        },
    )
    assert result.ok is True
    assert result.data["acknowledged"] is True
    assert result.data["reason"] == "low_confidence"
    assert result.data["draft_patch_bytes"] == len(draft)
    # The draft is persisted on the context so escalation outcomes carry it.
    assert ctx.last_attempted_diff == draft


async def test_escalate_without_draft_leaves_context_unchanged():
    ctx = _ctx()
    ctx.last_attempted_diff = None
    tool = tools_base.get("escalate")
    result = await tool.handler(
        ctx,
        {
            "reason": "infra_failure_out_of_scope",
            "explanation": "the runner image is broken; not my PR",
        },
    )
    assert result.ok is True
    assert ctx.last_attempted_diff is None


async def test_escalate_rejects_invalid_reason():
    tool = tools_base.get("escalate")
    result = await tool.handler(
        _ctx(),
        {"reason": "i_give_up", "explanation": "because"},
    )
    assert result.ok is False
    assert "invalid_reason" in (result.error or "")


async def test_escalate_rejects_missing_explanation():
    tool = tools_base.get("escalate")
    result = await tool.handler(
        _ctx(),
        {"reason": "low_confidence", "explanation": "   "},
    )
    assert result.ok is False
    assert "explanation" in (result.error or "")


async def test_loop_terminates_with_escalate_reason():
    """Using the REAL registered escalate tool, verify the loop returns
    RunOutcome(ESCALATED) with the reason the agent supplied.

    Uses `low_confidence` (an evidence-free reason) so this test stays
    focused on "escalate terminates the loop" without entangling the
    escalation-evidence gate introduced alongside this test. Gate-
    specific behavior is covered by test_escalation_evidence_gate.py.
    """
    ctx = _ctx()

    async def scripted(_messages):
        return LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="e1",
                    name="escalate",
                    input={
                        "reason": "low_confidence",
                        "explanation": "unsure what the right fix is",
                        "draft_patch": "",
                    },
                )
            ],
        )

    outcome = await run_ci_fix_v2(ctx, scripted)
    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.LOW_CONFIDENCE
    assert "unsure" in outcome.explanation


async def test_loop_treats_unknown_reason_string_as_low_confidence():
    """The escalate TOOL validates reasons, but if the LLM ever smuggles
    an invalid reason past a tool-result error (shouldn't happen, but
    belt-and-suspenders), the loop's terminal handler should default to
    LOW_CONFIDENCE rather than crash.
    """
    # Register a fake escalate that returns ok=True even with a bad reason,
    # simulating the edge case where the tool-validation layer is bypassed.
    from phalanx.ci_fixer_v2.tools.base import ToolResult, ToolSchema, register

    async def fake_handler(_ctx, _input):
        return ToolResult(ok=True, data={"acknowledged": True})

    class _ShimEscalate:
        schema = ToolSchema(
            name="escalate",
            description="",
            input_schema={"type": "object"},
        )
        handler = staticmethod(fake_handler)

    # Overwrite the real escalate (register is idempotent by name).
    register(_ShimEscalate())

    async def scripted(_messages):
        return LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="e1",
                    name="escalate",
                    input={"reason": "bogus_value", "explanation": "x"},
                )
            ],
        )

    outcome = await run_ci_fix_v2(_ctx(), scripted)
    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.LOW_CONFIDENCE
