"""Integration-ish test: main loop + real builtin tools (seams mocked).

Drives the whole loop with a scripted LLM response script over the real
registered tools (fetch_ci_log, read_file, run_in_sandbox) plus a fake
`commit_and_push` (real impl lands in Week 1.6). Verifies the happy
path: agent reads a file, runs the failing command in sandbox, sees
it pass, then commits — and the loop returns COMMITTED.

This is the "prove the loop actually works end-to-end before wiring
real LLMs" checkpoint from the spec's proof-first discipline.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse, run_ci_fix_v2
from phalanx.ci_fixer_v2.config import RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import action, base as tools_base


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _scripted_llm(responses):
    it = iter(responses)

    async def _call(_messages):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("LLM called more times than scripted")

    return _call


def _make_ctx(workspace: str) -> AgentContext:
    return AgentContext(
        ci_fix_run_id="run-happy-1",
        repo_full_name="acme/widget",
        repo_workspace_path=workspace,
        original_failing_command="ruff check app/",
        ci_api_key="tok",
        ci_provider="github_actions",
        sandbox_container_id="container-xyz",
    )


def _register_fake_commit_tool():
    """commit_and_push proper implementation is Week 1.6; for this test
    we register a stub that returns success so the loop can reach the
    COMMITTED terminal state and we can assert the verdict.
    """
    async def _handler(_ctx, _input):
        return tools_base.ToolResult(
            ok=True,
            data={"sha": "abc123", "branch": "feature/lint-fix"},
        )

    class _FakeCommitTool:
        schema = tools_base.ToolSchema(
            name="commit_and_push",
            description="(test stub) commit + push",
            input_schema={"type": "object"},
        )
        handler = staticmethod(_handler)

    tools_base.register(_FakeCommitTool())


async def test_loop_happy_path_read_then_sandbox_then_commit(tmp_path, monkeypatch):
    # Set up workspace with a minimal file the agent can "read".
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text(
        "def hello():\n    return 'hi'\n", encoding="utf-8"
    )

    # Mock the sandbox-exec seam so run_in_sandbox succeeds without docker.
    async def fake_exec(_argv, _timeout):
        return (0, "All checks passed!\n", "", False, 0.3)

    monkeypatch.setattr(action, "_exec_argv", fake_exec)
    monkeypatch.setattr(
        action,
        "_build_exec_argv",
        lambda cid, cmd: ["docker", "exec", cid, "sh", "-c", cmd],
    )

    # Register the stub commit tool for this test.
    _register_fake_commit_tool()

    ctx = _make_ctx(str(tmp_path))

    # Script 4 turns:
    #   1. agent reads app/api.py
    #   2. agent runs the failing command in sandbox → exit 0 → gate flips
    #   3. agent commits → COMMITTED verdict
    script = [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="t1",
                    name="read_file",
                    input={"path": "app/api.py"},
                )
            ],
            input_tokens=100,
            output_tokens=20,
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="t2",
                    name="run_in_sandbox",
                    input={"command": "ruff check app/", "timeout_seconds": 60},
                )
            ],
            input_tokens=120,
            output_tokens=30,
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="t3",
                    name="commit_and_push",
                    input={
                        "branch_strategy": "author_branch",
                        "commit_message": "fix: lint",
                        "files": ["app/api.py"],
                    },
                )
            ],
            input_tokens=80,
            output_tokens=25,
        ),
    ]

    outcome = await run_ci_fix_v2(ctx, _scripted_llm(script))

    assert outcome.verdict == RunVerdict.COMMITTED
    assert outcome.committed_sha == "abc123"
    assert outcome.committed_branch == "feature/lint-fix"

    # Telemetry integrity: input tokens accumulated across all turns.
    assert ctx.cost.gpt_reasoning_input_tokens == 300
    assert ctx.cost.gpt_reasoning_output_tokens == 75
    assert ctx.cost.sandbox_runtime_seconds == pytest.approx(0.3)

    # Tool invocation timeline — one entry per tool call made.
    tool_names = [inv.tool_name for inv in ctx.tool_invocations]
    assert tool_names == ["read_file", "run_in_sandbox", "commit_and_push"]
