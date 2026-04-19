"""Skeleton smoke tests for phalanx.ci_fixer_v2.

Proves the loop's control flow (verification gate, turn cap, escalation
triggers) with mocked LLM responses + a fake registered tool. No real
LLM, no real git, no real sandbox.

These are the bare-minimum tests that must pass before any tool
implementation lands in Week 1.5+. They establish the shape the rest of
the v2 tests will layer on top of.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import __version__
from phalanx.ci_fixer_v2.agent import (
    LLMResponse,
    LLMToolUse,
    RunOutcome,
    run_ci_fix_v2,
)
from phalanx.ci_fixer_v2.config import (
    MAX_MAIN_TURNS,
    EscalationReason,
    RunVerdict,
)
from phalanx.ci_fixer_v2.context import AgentContext, CostRecord
from phalanx.ci_fixer_v2.tools import base as tools_base


# pyproject asyncio_mode=auto handles async tests without per-test decorators.


# ── Fixtures ──────────────────────────────────────────────────────────────
def _make_context(**overrides) -> AgentContext:
    defaults = dict(
        ci_fix_run_id="test-run-1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/test-ws",
        original_failing_command="ruff check app/",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _scripted_llm(responses: list[LLMResponse]):
    """Return a fake LLMCallable that yields the given responses in
    order. Extra calls beyond the script raise — that way a runaway
    loop fails the test loudly instead of deadlocking."""
    iterator = iter(responses)

    async def _call(_messages):
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError("LLM called more times than the script provided")

    return _call


@pytest.fixture(autouse=True)
def _clean_tool_registry():
    """Each test starts with an empty registry so fakes don't leak."""
    tools_base.clear_registry_for_testing()
    yield
    tools_base.clear_registry_for_testing()


# ── Package-level assertions ──────────────────────────────────────────────
def test_package_version_exposed():
    assert __version__.startswith("0.")


def test_tool_registry_starts_empty():
    assert tools_base.all_schemas() == []
    assert tools_base.is_registered("anything") is False


# ── Cost accumulator ──────────────────────────────────────────────────────
def test_cost_record_totals_and_serialization():
    cost = CostRecord(
        gpt_reasoning_cost_usd=0.12,
        sonnet_coder_cost_usd=0.03,
        sandbox_runtime_seconds=14.5,
    )
    assert cost.total_cost_usd == pytest.approx(0.15)
    out = cost.to_dict()
    assert out["total_cost_usd"] == pytest.approx(0.15)
    assert out["sandbox_runtime_seconds"] == pytest.approx(14.5)
    assert set(out["gpt_reasoning"].keys()) == {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cost_usd",
    }


# ── Context guarantees ────────────────────────────────────────────────────
def test_sandbox_verification_requires_original_command_match():
    ctx = _make_context(original_failing_command="ruff check app/")
    assert ctx.last_sandbox_verified is False

    # Unrelated command — must NOT flip the gate.
    flipped = ctx.mark_sandbox_verified("pytest tests/")
    assert flipped is False
    assert ctx.last_sandbox_verified is False

    # Exact match — flips.
    flipped = ctx.mark_sandbox_verified("ruff check app/")
    assert flipped is True
    assert ctx.last_sandbox_verified is True


def test_sandbox_verification_accepts_superset_command():
    ctx = _make_context(original_failing_command="ruff check app/")
    # A wrapping command that includes the original counts.
    flipped = ctx.mark_sandbox_verified("cd repo && ruff check app/ --no-cache")
    assert flipped is True


def test_invalidate_sandbox_verification_clears_the_flag():
    ctx = _make_context()
    ctx.mark_sandbox_verified(ctx.original_failing_command)
    assert ctx.last_sandbox_verified is True
    ctx.invalidate_sandbox_verification()
    assert ctx.last_sandbox_verified is False


# ── Loop: implicit stop ───────────────────────────────────────────────────
async def test_loop_end_turn_without_commit_escalates_implicit_stop():
    ctx = _make_context()
    llm = _scripted_llm(
        [LLMResponse(stop_reason="end_turn", text="I think we're done.")]
    )

    outcome = await run_ci_fix_v2(ctx, llm)

    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.IMPLICIT_STOP


# ── Loop: turn cap ────────────────────────────────────────────────────────
async def test_loop_turn_cap_auto_escalates():
    ctx = _make_context()
    # Agent keeps emitting tool_use for an unregistered tool, never
    # commits or escalates. Registry is clean → every call becomes a
    # tool_not_registered error the agent "sees" but never acts on.
    # Loop should hit turn cap and escalate.
    unregistered_tool = LLMToolUse(id="t1", name="not_a_real_tool", input={})
    llm = _scripted_llm(
        [LLMResponse(stop_reason="tool_use", tool_uses=[unregistered_tool])] * 3
    )

    outcome = await run_ci_fix_v2(ctx, llm, max_turns=3)

    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.TURN_CAP_REACHED


# ── Loop: verification gate ───────────────────────────────────────────────
async def test_commit_without_sandbox_verification_is_blocked():
    ctx = _make_context()
    # No mark_sandbox_verified call → gate should fire.
    commit_use = LLMToolUse(
        id="c1",
        name="commit_and_push",
        input={
            "branch_strategy": "author_branch",
            "commit_message": "fix: lint",
            "files": ["app/api.py"],
        },
    )
    llm = _scripted_llm(
        [LLMResponse(stop_reason="tool_use", tool_uses=[commit_use])]
    )

    outcome = await run_ci_fix_v2(ctx, llm)

    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.VERIFICATION_GATE_VIOLATION


# ── Loop: explicit escalate tool ──────────────────────────────────────────
async def test_explicit_escalate_tool_terminates_with_given_reason():
    from phalanx.ci_fixer_v2.tools.base import (
        ToolResult,
        ToolSchema,
        register,
    )

    # Register a fake escalate tool — the hard-gate logic in the loop
    # treats any registered "escalate" tool as terminal.
    async def _handler(_ctx, _input):
        return ToolResult(ok=True, data={"acknowledged": True})

    class _FakeEscalate:
        schema = ToolSchema(
            name="escalate",
            description="terminal escalation",
            input_schema={"type": "object"},
        )
        handler = staticmethod(_handler)

    register(_FakeEscalate())

    ctx = _make_context()
    esc_use = LLMToolUse(
        id="e1",
        name="escalate",
        input={
            "reason": EscalationReason.AMBIGUOUS_FIX.value,
            "draft_patch": "",
            "explanation": "two plausible fixes; asking the author",
        },
    )
    llm = _scripted_llm(
        [LLMResponse(stop_reason="tool_use", tool_uses=[esc_use])]
    )

    outcome = await run_ci_fix_v2(ctx, llm)

    assert outcome.verdict == RunVerdict.ESCALATED
    assert outcome.escalation_reason == EscalationReason.AMBIGUOUS_FIX
    assert "author" in outcome.explanation


# ── MAX_MAIN_TURNS sanity ─────────────────────────────────────────────────
def test_max_main_turns_matches_spec():
    # Spec §6 pins this at 25. If someone changes it, they must also
    # update docs/ci-fixer-v2-spec.md and this test.
    assert MAX_MAIN_TURNS == 25
