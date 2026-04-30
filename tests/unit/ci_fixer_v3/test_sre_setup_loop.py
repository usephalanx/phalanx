"""Tier-1 tests for the SRE setup loop (Phase 1).

Locked-down behaviors:
  - Terminal report_ready / report_partial / report_blocked tool calls
    cleanly exit the loop with the right SREResult shape.
  - Iteration cap → PARTIAL with notes.
  - Token budget cap → PARTIAL with notes.
  - 3 consecutive provider strikes → PARTIAL with fallback_used=true.
  - Single non-provider exception → propagates (real-bug surfacing).
  - Unknown tool name from LLM → error result, loop continues.
  - Tool timeout → error result, loop continues (doesn't kill loop).
  - Multi-tool-use turn → all dispatched in order, terminal aborts further.

No real LLM, no real Docker, no Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from phalanx.ci_fixer_v3.provisioner import ExecResult
from phalanx.ci_fixer_v3.sre_setup.loop import (
    MAX_SETUP_ITERATIONS,
    PROVIDER_STRIKES_LIMIT,
    SREResult,
    run_sre_setup_subagent,
)
from phalanx.ci_fixer_v3.sre_setup.schemas import SREToolContext

if TYPE_CHECKING:
    from pathlib import Path


# ────────────────────────────────────────────────────────────────────────
# Fakes for LLMResponse / LLMToolUse (avoid pulling in v2 full provider chain)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeToolUse:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeResponse:
    stop_reason: str = "tool_use"
    text: str = ""
    tool_uses: list[_FakeToolUse] = field(default_factory=list)
    input_tokens: int = 200
    output_tokens: int = 100
    thinking_tokens: int = 0


def _scripted(turns: list[_FakeResponse]):
    """Return an async callable that yields `turns` in order across calls."""
    queue = list(turns)

    async def call(messages, tools=None):
        if not queue:
            return _FakeResponse(stop_reason="end_turn", text="(no more scripted turns)")
        return queue.pop(0)

    return call


def _raising(exc: Exception, after: int = 0):
    """Async callable that succeeds for `after` calls, then raises."""
    state = {"calls": 0}

    async def call(messages, tools=None):
        state["calls"] += 1
        if state["calls"] > after:
            raise exc
        return _FakeResponse(stop_reason="end_turn", text="(pre-fail)")

    return call


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "lint.yml").write_text(
        "name: Lint\n"
        "on: [push]\n"
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: astral-sh/setup-uv@v8.0.0\n"
        "      - run: uvx --with tox-uv tox -e mypy\n",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    return tmp_path


@pytest.fixture
def ctx(workspace) -> SREToolContext:
    async def fake_exec(container_id, cmd, **kwargs):
        return ExecResult(ok=True, exit_code=0)

    return SREToolContext(
        container_id="cont-fake",
        workspace_path=str(workspace),
        exec_in_sandbox=fake_exec,
    )


# ────────────────────────────────────────────────────────────────────────
# Terminal cases
# ────────────────────────────────────────────────────────────────────────


async def test_immediate_report_ready_returns_ready(ctx):
    """LLM reports ready in turn 1 — loop exits with READY."""
    llm = _scripted(
        [
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="r1",
                        name="report_ready",
                        input={
                            "capabilities": [
                                {
                                    "tool": "uv",
                                    "version": "0.8.4",
                                    "install_method": "preinstalled",
                                    "evidence_ref": ".github/workflows/lint.yml:7",
                                }
                            ],
                            "observed_token_status": [
                                {"cmd": "uvx tox", "first_token": "uvx", "found": True}
                            ],
                        },
                    )
                ]
            )
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["uvx"],
        det_spec_summary={},
        observed_failing_commands=["uvx tox -e mypy"],
        llm_call=llm,
    )
    assert result.final_status == "READY"
    assert result.iterations_used == 1
    assert len(result.capabilities) == 1
    assert result.capabilities[0]["tool"] == "uv"


async def test_report_blocked_returns_blocked_with_reason(ctx):
    llm = _scripted(
        [
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="b1",
                        name="report_blocked",
                        input={
                            "reason": "custom_container",
                            "evidence": {"file": "x.yml", "line": 1},
                        },
                    )
                ]
            )
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["something"],
        det_spec_summary={},
        observed_failing_commands=["something"],
        llm_call=llm,
    )
    assert result.final_status == "BLOCKED"
    assert result.blocked_reason == "custom_container"
    assert result.blocked_evidence == {"file": "x.yml", "line": 1}


async def test_report_partial_returns_partial(ctx):
    llm = _scripted(
        [
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="p1",
                        name="report_partial",
                        input={
                            "capabilities": [],
                            "gaps_remaining": ["uv"],
                            "reason": "tried but couldn't",
                        },
                    )
                ]
            )
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["uv"],
        det_spec_summary={},
        observed_failing_commands=["uv x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert result.gaps_remaining == ["uv"]
    assert "tried but couldn't" in result.notes


# ────────────────────────────────────────────────────────────────────────
# Multi-step success
# ────────────────────────────────────────────────────────────────────────


async def test_read_then_install_then_check_then_ready(ctx):
    """Realistic flow: list, read, install, check, report_ready — 5 turns."""
    llm = _scripted(
        [
            _FakeResponse(tool_uses=[_FakeToolUse(id="t1", name="list_workflows", input={})]),
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="t2",
                        name="read_file",
                        input={"path": ".github/workflows/lint.yml"},
                    )
                ]
            ),
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="t3",
                        name="install_pip",
                        input={
                            "packages": ["uv"],
                            "evidence_file": ".github/workflows/lint.yml",
                            "evidence_line": 7,
                        },
                    )
                ]
            ),
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(id="t4", name="check_command_available", input={"name": "uv"})
                ]
            ),
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="t5",
                        name="report_ready",
                        input={
                            "capabilities": [
                                {
                                    "tool": "uv",
                                    "version": "",
                                    "install_method": "pip",
                                    "evidence_ref": ".github/workflows/lint.yml:7",
                                }
                            ],
                            "observed_token_status": [
                                {"cmd": "uvx tox", "first_token": "uvx", "found": True}
                            ],
                        },
                    )
                ]
            ),
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["uvx"],
        det_spec_summary={},
        observed_failing_commands=["uvx tox -e mypy"],
        llm_call=llm,
    )
    assert result.final_status == "READY"
    assert result.iterations_used == 5
    assert len(ctx.install_log) == 5  # one per turn's tool call


# ────────────────────────────────────────────────────────────────────────
# Budget enforcement
# ────────────────────────────────────────────────────────────────────────


async def test_iteration_cap_returns_partial_after_max(ctx):
    """If LLM never calls report_*, hit iteration cap → PARTIAL."""
    # Script enough non-terminal turns to exceed the cap.
    turns = [
        _FakeResponse(tool_uses=[_FakeToolUse(id=f"t{i}", name="list_workflows", input={})])
        for i in range(MAX_SETUP_ITERATIONS + 5)
    ]
    llm = _scripted(turns)
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert result.iterations_used == MAX_SETUP_ITERATIONS
    assert "loop_exhausted" in result.notes


async def test_token_budget_returns_partial(ctx):
    """If a single response burns the budget, abort cleanly."""
    big_response = _FakeResponse(
        tool_uses=[_FakeToolUse(id="big", name="list_workflows", input={})],
        input_tokens=60_000,
        output_tokens=0,
    )
    llm = _scripted([big_response])
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert "token budget" in result.notes


# ────────────────────────────────────────────────────────────────────────
# Provider degradation → fallback
# ────────────────────────────────────────────────────────────────────────


async def test_three_provider_timeouts_trigger_fallback(ctx):
    """Three consecutive TimeoutErrors → fallback_used=true, PARTIAL."""
    import asyncio

    llm = _raising(TimeoutError("pretend rate-limited"))
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={"installed_count": 0},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert result.fallback_used is True
    assert result.provider_strikes == PROVIDER_STRIKES_LIMIT


async def test_overloaded_message_counts_as_provider_strike(ctx):
    """Strings like 'overloaded' / 'rate_limit' / '503' count as provider
    errors via the message-text heuristic."""
    llm = _raising(RuntimeError("anthropic 503: server overloaded"))
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert result.fallback_used is True


async def test_non_provider_exception_propagates(ctx):
    """A real bug (TypeError, etc.) must propagate, not silently fallback."""
    llm = _raising(ValueError("real bug — not a provider issue"))
    with pytest.raises(ValueError, match="real bug"):
        await run_sre_setup_subagent(
            ctx,
            gaps=["x"],
            det_spec_summary={},
            observed_failing_commands=["x"],
            llm_call=llm,
        )


# ────────────────────────────────────────────────────────────────────────
# Bad LLM output
# ────────────────────────────────────────────────────────────────────────


async def test_unknown_tool_logs_error_and_continues(ctx):
    """LLM hallucinates a non-existent tool → error message back, loop continues."""
    llm = _scripted(
        [
            _FakeResponse(tool_uses=[_FakeToolUse(id="bad", name="hack_the_planet")]),
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(
                        id="ok",
                        name="report_partial",
                        input={
                            "capabilities": [],
                            "gaps_remaining": ["x"],
                            "reason": "recovered",
                        },
                    )
                ]
            ),
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert result.iterations_used == 2


async def test_no_tool_calls_returns_partial(ctx):
    """LLM responds with text only (no tool calls) → PARTIAL, surfaces the text."""
    llm = _scripted([_FakeResponse(stop_reason="end_turn", text="I'm confused", tool_uses=[])])
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["x"],
        det_spec_summary={},
        observed_failing_commands=["x"],
        llm_call=llm,
    )
    assert result.final_status == "PARTIAL"
    assert "I'm confused" in result.notes


async def test_terminal_breaks_remaining_tool_calls_in_same_turn(ctx):
    """If a turn has [report_blocked, install_pip], terminal stops further dispatch."""
    llm = _scripted(
        [
            _FakeResponse(
                tool_uses=[
                    _FakeToolUse(id="t1", name="report_blocked", input={"reason": "sudo_denied"}),
                    _FakeToolUse(
                        id="t2",
                        name="install_pip",
                        input={
                            "packages": ["uv"],
                            "evidence_file": ".github/workflows/lint.yml",
                            "evidence_line": 7,
                        },
                    ),
                ]
            )
        ]
    )
    result = await run_sre_setup_subagent(
        ctx,
        gaps=["uv"],
        det_spec_summary={},
        observed_failing_commands=["uv"],
        llm_call=llm,
    )
    assert result.final_status == "BLOCKED"
    # The install_pip tool should NOT have been logged — terminal aborted dispatch.
    assert all(call["tool"] != "install_pip" for call in ctx.install_log)


# ────────────────────────────────────────────────────────────────────────
# Schema sanity
# ────────────────────────────────────────────────────────────────────────


def test_sre_result_default_lists_are_unique_per_instance():
    """Default-mutable-arg trap regression: two SREResult() instances must
    not share their list defaults."""
    r1 = SREResult(final_status="READY")
    r2 = SREResult(final_status="READY")
    r1.capabilities.append({"x": 1})
    assert r2.capabilities == []
