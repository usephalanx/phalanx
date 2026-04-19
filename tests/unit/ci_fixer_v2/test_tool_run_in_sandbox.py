"""Unit tests for the run_in_sandbox tool.

The docker-exec subprocess path is replaced via the `_exec_argv` seam so
tests do not spawn real containers or require a Docker daemon. The
`_build_exec_argv` seam is also patched where we want to assert the
command shape without reaching into v1's wrap_shell_cmd_for_container.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import action, base as tools_base


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
        sandbox_container_id="test-container-abc",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _patch_exec(
    monkeypatch,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    elapsed: float = 0.5,
):
    async def fake_exec(_argv, _timeout):
        return (exit_code, stdout, stderr, timed_out, elapsed)

    monkeypatch.setattr(action, "_exec_argv", fake_exec)


def _patch_build_argv(monkeypatch):
    def fake_build(container_id, shell_cmd):
        return ["docker", "exec", "-w", "/workspace", container_id, "sh", "-c", shell_cmd]

    monkeypatch.setattr(action, "_build_exec_argv", fake_build)


async def test_run_in_sandbox_happy_path_verifies_when_command_matches(monkeypatch):
    _patch_build_argv(monkeypatch)
    _patch_exec(monkeypatch, exit_code=0, stdout="All checks passed!\n")

    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {"command": "ruff check app/"})

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert result.data["sandbox_verified"] is True
    assert ctx.last_sandbox_verified is True
    assert ctx.cost.sandbox_runtime_seconds == pytest.approx(0.5)


async def test_run_in_sandbox_no_verification_when_command_differs(monkeypatch):
    _patch_build_argv(monkeypatch)
    _patch_exec(monkeypatch, exit_code=0, stdout="ok")

    ctx = _ctx(original_failing_command="ruff check app/")
    tool = tools_base.get("run_in_sandbox")
    # Different command — exit 0 but verification gate should NOT flip.
    result = await tool.handler(ctx, {"command": "pytest tests/test_unrelated.py"})

    assert result.ok is True
    assert result.data["sandbox_verified"] is False
    assert ctx.last_sandbox_verified is False


async def test_run_in_sandbox_failing_exit_does_not_verify(monkeypatch):
    _patch_build_argv(monkeypatch)
    _patch_exec(monkeypatch, exit_code=1, stderr="E501 line too long")

    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {"command": "ruff check app/"})

    assert result.ok is True  # tool succeeded — the command didn't
    assert result.data["exit_code"] == 1
    assert result.data["sandbox_verified"] is False
    assert ctx.last_sandbox_verified is False


async def test_run_in_sandbox_superset_command_verifies(monkeypatch):
    _patch_build_argv(monkeypatch)
    _patch_exec(monkeypatch, exit_code=0)

    ctx = _ctx(original_failing_command="ruff check app/")
    tool = tools_base.get("run_in_sandbox")
    # Command wraps the original — should count per AgentContext._command_covers_original.
    result = await tool.handler(
        ctx,
        {"command": "cd /workspace && ruff check app/ --no-cache"},
    )

    assert result.ok is True
    assert result.data["sandbox_verified"] is True


async def test_run_in_sandbox_missing_container_returns_error():
    ctx = _ctx(sandbox_container_id=None)
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {"command": "echo ok"})

    assert result.ok is False
    assert "sandbox_not_provisioned" in (result.error or "")


async def test_run_in_sandbox_missing_command_rejected():
    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "command" in (result.error or "")


async def test_run_in_sandbox_timeout_clamped_low(monkeypatch):
    _patch_build_argv(monkeypatch)
    captured = {}

    async def fake_exec(_argv, timeout):
        captured["timeout"] = timeout
        return (0, "", "", False, 0.1)

    monkeypatch.setattr(action, "_exec_argv", fake_exec)
    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    await tool.handler(ctx, {"command": "echo", "timeout_seconds": 1})
    assert captured["timeout"] == action._TIMEOUT_MIN_S


async def test_run_in_sandbox_timeout_clamped_high(monkeypatch):
    _patch_build_argv(monkeypatch)
    captured = {}

    async def fake_exec(_argv, timeout):
        captured["timeout"] = timeout
        return (0, "", "", False, 0.1)

    monkeypatch.setattr(action, "_exec_argv", fake_exec)
    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    await tool.handler(ctx, {"command": "echo", "timeout_seconds": 99999})
    assert captured["timeout"] == action._TIMEOUT_MAX_S


async def test_run_in_sandbox_timeout_default_when_missing(monkeypatch):
    _patch_build_argv(monkeypatch)
    captured = {}

    async def fake_exec(_argv, timeout):
        captured["timeout"] = timeout
        return (0, "", "", False, 0.1)

    monkeypatch.setattr(action, "_exec_argv", fake_exec)
    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    await tool.handler(ctx, {"command": "echo"})
    assert captured["timeout"] == 120  # the code's default


async def test_run_in_sandbox_propagates_timed_out_flag(monkeypatch):
    _patch_build_argv(monkeypatch)
    _patch_exec(monkeypatch, exit_code=124, timed_out=True, elapsed=120.0)

    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {"command": "sleep 999", "timeout_seconds": 120})

    assert result.ok is True
    assert result.data["timed_out"] is True
    assert result.data["sandbox_verified"] is False


async def test_run_in_sandbox_docker_binary_missing_returns_error(monkeypatch):
    _patch_build_argv(monkeypatch)

    async def raise_missing(_argv, _timeout):
        raise RuntimeError("docker_binary_missing: /usr/bin/docker")

    monkeypatch.setattr(action, "_exec_argv", raise_missing)
    ctx = _ctx()
    tool = tools_base.get("run_in_sandbox")
    result = await tool.handler(ctx, {"command": "echo"})

    assert result.ok is False
    assert "docker_binary_missing" in (result.error or "")


# ── Direct tests of _exec_argv against /bin/echo and /bin/sleep ───────────
# These exercise the real asyncio subprocess path. No docker involved — we
# just need a real executable so the critical-tool 90% coverage floor
# includes the actual exec body, not just the mocked seam.

import os  # noqa: E402  — placed here to keep the top stanza focused on pytest


def _bin(name: str) -> str:
    """Resolve a standard UNIX binary; skip the test gracefully if missing."""
    candidates = [f"/bin/{name}", f"/usr/bin/{name}"]
    for path in candidates:
        if os.path.exists(path):
            return path
    pytest.skip(f"{name} not found on system — skipping real-exec test")


async def test_exec_argv_real_echo_exit_0():
    echo = _bin("echo")
    exit_code, stdout, stderr, timed_out, elapsed = await action._exec_argv(
        [echo, "hello"], timeout_seconds=10
    )
    assert exit_code == 0
    assert "hello" in stdout
    assert stderr == ""
    assert timed_out is False
    assert elapsed >= 0.0


async def test_exec_argv_real_false_exit_1():
    false_bin = _bin("false")
    exit_code, _stdout, _stderr, timed_out, _elapsed = await action._exec_argv(
        [false_bin], timeout_seconds=10
    )
    assert exit_code == 1
    assert timed_out is False


async def test_exec_argv_real_timeout_kills_process():
    sleep = _bin("sleep")
    exit_code, stdout, stderr, timed_out, elapsed = await action._exec_argv(
        [sleep, "10"], timeout_seconds=1
    )
    assert timed_out is True
    assert exit_code == 124
    assert stderr == "timed_out"
    assert elapsed < 5.0  # killed promptly — not waited out


async def test_exec_argv_raises_runtime_error_when_binary_missing():
    with pytest.raises(RuntimeError, match="docker_binary_missing"):
        await action._exec_argv(
            ["/nonexistent/path/to/docker-xyzzy"], timeout_seconds=5
        )
