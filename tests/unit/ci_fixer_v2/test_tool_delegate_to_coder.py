"""Unit tests for delegate_to_coder tool.

Patches `run_coder_subagent` + `_compute_final_diff` so the main tool's
input validation + output shaping are tested without spinning up the
Sonnet loop.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.coder_subagent import CoderResult
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools import coder


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx(**overrides) -> AgentContext:
    defaults = dict(
        ci_fix_run_id="run-1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _valid_input() -> dict:
    return {
        "task_description": "Fix E501 in app/api.py line 42",
        "target_files": ["app/api.py"],
        "diagnosis_summary": "Long line literal; wrap with parens.",
        "failing_command": "ruff check app/",
    }


def _patch_subagent(monkeypatch, result: CoderResult):
    called_with: dict = {}

    async def fake_run(ctx, **kwargs):
        called_with.update(kwargs)
        called_with["ctx_id"] = id(ctx)
        # Simulate the subagent flipping verification if it succeeded.
        if result.success:
            ctx.last_sandbox_verified = True
        return result

    # Patch at the import site (tools.coder imports it inside the handler).
    import phalanx.ci_fixer_v2.coder_subagent as sub_mod

    monkeypatch.setattr(sub_mod, "run_coder_subagent", fake_run)
    return called_with


def _patch_final_diff(monkeypatch, diff: str):
    async def fake(_ws):
        return diff

    monkeypatch.setattr(coder, "_compute_final_diff", fake)


async def test_delegate_to_coder_happy_path(monkeypatch):
    called = _patch_subagent(
        monkeypatch,
        CoderResult(
            success=True,
            sandbox_exit_code=0,
            sandbox_stdout_tail="All checks passed!",
            sandbox_stderr_tail="",
            attempts_used=1,
            sonnet_input_tokens=500,
            sonnet_output_tokens=200,
            sonnet_thinking_tokens=1000,
            notes="verified",
        ),
    )
    _patch_final_diff(monkeypatch, "diff --git a/app/api.py b/app/api.py\n...")

    ctx = _ctx()
    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(ctx, _valid_input())

    assert result.ok is True
    data = result.data
    assert data["success"] is True
    assert data["failing_command_matched"] is True
    assert data["sandbox_exit_code"] == 0
    assert "app/api.py" in data["unified_diff"]
    assert data["tokens_used"] == {
        "input": 500,
        "output": 200,
        "thinking": 1000,
    }
    assert data["attempts_used"] == 1

    # Subagent was called with the validated inputs.
    assert called["task_description"].startswith("Fix E501")
    assert called["target_files"] == ["app/api.py"]
    assert called["failing_command"] == "ruff check app/"
    # Diff landed on context for escalation telemetry.
    assert ctx.last_attempted_diff is not None
    assert "app/api.py" in ctx.last_attempted_diff


async def test_delegate_to_coder_failure_returns_empty_diff(monkeypatch):
    _patch_subagent(
        monkeypatch,
        CoderResult(
            success=False,
            sandbox_exit_code=1,
            sandbox_stdout_tail="",
            sandbox_stderr_tail="E501 line too long",
            attempts_used=2,
            sonnet_input_tokens=400,
            sonnet_output_tokens=150,
            sonnet_thinking_tokens=0,
            notes="could not satisfy both ruff and existing tests",
        ),
    )
    # Even if git diff could produce something, failures skip it.
    _patch_final_diff(monkeypatch, "some-diff")

    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is True
    assert result.data["success"] is False
    assert result.data["unified_diff"] == ""
    assert result.data["attempts_used"] == 2
    assert "could not satisfy" in result.data["notes"]


async def test_delegate_to_coder_defaults_failing_command_from_ctx(monkeypatch):
    called = _patch_subagent(
        monkeypatch,
        CoderResult(success=False, attempts_used=0),
    )
    _patch_final_diff(monkeypatch, "")

    ctx = _ctx(original_failing_command="pytest tests/test_auth.py::test_login")
    inp = _valid_input()
    del inp["failing_command"]  # omit from input
    tool = tools_base.get("delegate_to_coder")
    await tool.handler(ctx, inp)
    assert called["failing_command"] == "pytest tests/test_auth.py::test_login"


async def test_delegate_to_coder_max_attempts_clamped(monkeypatch):
    called = _patch_subagent(
        monkeypatch,
        CoderResult(success=False, attempts_used=0),
    )
    _patch_final_diff(monkeypatch, "")

    tool = tools_base.get("delegate_to_coder")
    await tool.handler(
        _ctx(),
        {**_valid_input(), "max_attempts": 99},
    )
    assert called["max_attempts"] == 5  # clamped to max


async def test_delegate_to_coder_requires_task_description():
    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(
        _ctx(),
        {**_valid_input(), "task_description": ""},
    )
    assert result.ok is False
    assert "task_description" in (result.error or "")


async def test_delegate_to_coder_requires_target_files():
    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(
        _ctx(),
        {**_valid_input(), "target_files": []},
    )
    assert result.ok is False
    assert "target_files" in (result.error or "")


async def test_delegate_to_coder_rejects_non_string_target_files():
    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(
        _ctx(),
        {**_valid_input(), "target_files": ["ok.py", ""]},
    )
    assert result.ok is False


async def test_delegate_to_coder_requires_failing_command_somewhere():
    # No failing_command in input AND AgentContext.original_failing_command empty.
    ctx = _ctx(original_failing_command="")
    inp = _valid_input()
    del inp["failing_command"]
    tool = tools_base.get("delegate_to_coder")
    result = await tool.handler(ctx, inp)
    assert result.ok is False
    assert "failing_command" in (result.error or "")
