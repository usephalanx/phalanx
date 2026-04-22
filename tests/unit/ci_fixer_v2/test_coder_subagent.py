"""Unit tests for the coder subagent loop.

Drives `run_coder_subagent` with scripted Sonnet responses and real
registered tools (scoped to ALLOWED_CODER_TOOLS). Verifies:
  - apply_patch → run_in_sandbox verification → success exit
  - out-of-scope tool attempts are blocked (loop-level enforcement)
  - turn cap without verification → success=False
  - end_turn without verification → success=False
  - sonnet cost deltas land on AgentContext.cost
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.coder_subagent import (
    ALLOWED_CODER_TOOLS,
    run_coder_subagent,
)
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import action, base as tools_base, coder


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx(workspace: str, failing_cmd: str = "ruff check app/") -> AgentContext:
    return AgentContext(
        ci_fix_run_id="run-1",
        repo_full_name="acme/widget",
        repo_workspace_path=workspace,
        original_failing_command=failing_cmd,
        sandbox_container_id="container-x",
    )


def _scripted(responses: list[LLMResponse]):
    it = iter(responses)

    async def _call(_messages):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("LLM called more times than scripted")

    return _call


def test_coder_allowed_tools_exact_set():
    # The subagent's tool allow-list is the contract — lock it down.
    # replace_in_file is the preferred edit primitive; apply_patch
    # remains as the fallback for complex multi-site edits.
    assert ALLOWED_CODER_TOOLS == {
        "read_file",
        "grep",
        "replace_in_file",
        "apply_patch",
        "run_in_sandbox",
    }


async def test_coder_happy_path_apply_then_verify(tmp_path, monkeypatch):
    # Stub git-apply (for apply_patch) and docker-exec (for run_in_sandbox).
    async def fake_git_stdin(_ws, _args, _stdin, timeout=60):
        return (0, "", "")

    monkeypatch.setattr(coder, "_run_git_with_stdin", fake_git_stdin)

    async def fake_docker(_argv, _timeout):
        return (0, "All checks passed!\n", "", False, 0.4)

    monkeypatch.setattr(action, "_exec_argv", fake_docker)
    monkeypatch.setattr(
        action,
        "_build_exec_argv",
        lambda cid, cmd: ["docker", "exec", cid, "sh", "-c", cmd],
    )

    ctx = _ctx(str(tmp_path))

    diff = (
        "diff --git a/app/api.py b/app/api.py\n"
        "--- a/app/api.py\n+++ b/app/api.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    # Script 2 Sonnet turns:
    #   1. apply_patch
    #   2. run_in_sandbox matching the failing command → verifies
    script = [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="s1",
                    name="apply_patch",
                    input={"diff": diff, "target_files": ["app/api.py"]},
                )
            ],
            input_tokens=50,
            output_tokens=30,
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="s2",
                    name="run_in_sandbox",
                    input={"command": "ruff check app/"},
                )
            ],
            input_tokens=60,
            output_tokens=15,
            thinking_tokens=500,
        ),
    ]

    result = await run_coder_subagent(
        ctx,
        task_description="Fix lint E501 in app/api.py",
        target_files=["app/api.py"],
        diagnosis_summary="Line 42 too long.",
        failing_command="ruff check app/",
        llm_call=_scripted(script),
    )
    assert result.success is True
    assert result.sandbox_exit_code == 0
    assert result.attempts_used == 1
    assert result.sonnet_input_tokens == 110
    assert result.sonnet_output_tokens == 45
    assert result.sonnet_thinking_tokens == 500
    # Cost accumulated on the shared AgentContext for run-level telemetry.
    assert ctx.cost.sonnet_coder_input_tokens == 110
    assert ctx.cost.sonnet_coder_output_tokens == 45
    assert ctx.cost.sonnet_coder_thinking_tokens == 500
    # Verification flag flipped by the run_in_sandbox tool.
    assert ctx.last_sandbox_verified is True


async def test_coder_rejects_out_of_scope_tool(tmp_path, monkeypatch):
    # If Sonnet tries to call commit_and_push, the loop must reject it
    # via the allow-list and keep going.
    async def fake_docker(_argv, _timeout):
        return (0, "", "", False, 0.1)

    monkeypatch.setattr(action, "_exec_argv", fake_docker)
    monkeypatch.setattr(
        action, "_build_exec_argv", lambda cid, cmd: ["docker", cid, cmd]
    )

    ctx = _ctx(str(tmp_path))
    script = [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="oos",
                    name="commit_and_push",
                    input={
                        "branch_strategy": "author_branch",
                        "commit_message": "x",
                        "files": ["x.py"],
                    },
                )
            ],
        ),
        # After rejection, subagent decides to stop.
        LLMResponse(stop_reason="end_turn", text="tried wrong tool, giving up"),
    ]

    result = await run_coder_subagent(
        ctx,
        task_description="t",
        target_files=["x.py"],
        diagnosis_summary="d",
        failing_command="ruff check",
        llm_call=_scripted(script),
    )
    assert result.success is False
    # Tool trace records the out-of-scope rejection.
    assert any(
        entry.get("error") == "out_of_scope"
        and entry.get("tool") == "commit_and_push"
        for entry in result.tool_invocations
    )
    # AgentContext must not have received a verification flip from this attempt.
    assert ctx.last_sandbox_verified is False


async def test_coder_end_turn_without_verification_is_failure(tmp_path):
    ctx = _ctx(str(tmp_path))
    script = [LLMResponse(stop_reason="end_turn", text="I cannot find a fix")]
    result = await run_coder_subagent(
        ctx,
        task_description="t",
        target_files=["x.py"],
        diagnosis_summary="d",
        failing_command="ruff check",
        llm_call=_scripted(script),
    )
    assert result.success is False
    assert "cannot" in result.notes.lower()


async def test_coder_turn_cap_without_verification(tmp_path, monkeypatch):
    # Sonnet loops forever emitting tool_use for an unregistered tool.
    ctx = _ctx(str(tmp_path))
    bogus = LLMToolUse(id="b", name="no_such_tool", input={})
    script = [
        LLMResponse(stop_reason="tool_use", tool_uses=[bogus])
        for _ in range(3)
    ]
    result = await run_coder_subagent(
        ctx,
        task_description="t",
        target_files=["x.py"],
        diagnosis_summary="d",
        failing_command="ruff check",
        max_turns=3,
        llm_call=_scripted(script),
    )
    assert result.success is False


async def test_coder_sandbox_exit_nonzero_does_not_verify(tmp_path, monkeypatch):
    # Patch apply succeeds, sandbox returns exit 1 — subagent doesn't
    # terminate on verification (gate wasn't flipped); loop continues to
    # its next turn. Script ends with end_turn after no verification.
    async def fake_git_stdin(*_a, **_k):
        return (0, "", "")

    monkeypatch.setattr(coder, "_run_git_with_stdin", fake_git_stdin)

    async def fake_docker(_argv, _timeout):
        return (1, "", "still broken\n", False, 0.2)

    monkeypatch.setattr(action, "_exec_argv", fake_docker)
    monkeypatch.setattr(
        action, "_build_exec_argv", lambda cid, cmd: ["docker", cid, cmd]
    )

    ctx = _ctx(str(tmp_path))
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    script = [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="a",
                    name="apply_patch",
                    input={"diff": diff, "target_files": ["x.py"]},
                )
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="v",
                    name="run_in_sandbox",
                    input={"command": "ruff check"},
                )
            ],
        ),
        LLMResponse(stop_reason="end_turn", text="patch didn't help"),
    ]
    result = await run_coder_subagent(
        ctx,
        task_description="t",
        target_files=["x.py"],
        diagnosis_summary="d",
        failing_command="ruff check",
        llm_call=_scripted(script),
    )
    assert result.success is False
    assert result.sandbox_exit_code == 1
    assert result.attempts_used == 1
