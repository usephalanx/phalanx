"""Unit tests for apply_patch tool."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools import coder


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


def _patch_git_stdin(monkeypatch, script: list[tuple[int, str, str]]):
    calls: list[dict] = []
    it = iter(script)

    async def fake(_workspace, args, stdin_bytes, timeout=60):
        calls.append({"args": list(args), "stdin_len": len(stdin_bytes)})
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"git called more times than script; args={args}")

    monkeypatch.setattr(coder, "_run_git_with_stdin", fake)
    return calls


_SAMPLE_DIFF = """\
diff --git a/app/api.py b/app/api.py
index 0000000..1111111 100644
--- a/app/api.py
+++ b/app/api.py
@@ -1,2 +1,1 @@
-import os
 def hello(): return 'hi'
"""


async def test_apply_patch_happy_path(monkeypatch):
    calls = _patch_git_stdin(monkeypatch, [(0, "", ""), (0, "", "")])

    ctx = _ctx()
    ctx.last_sandbox_verified = True  # simulate prior verification

    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        ctx,
        {"diff": _SAMPLE_DIFF, "target_files": ["app/api.py"]},
    )
    assert result.ok is True
    assert result.data["applied_to"] == ["app/api.py"]
    assert result.data["file_count"] == 1
    assert result.data["diff_bytes"] > 0
    # Workspace changed → verification must be invalidated.
    assert ctx.last_sandbox_verified is False
    # Diff captured for escalation context.
    assert ctx.last_attempted_diff == _SAMPLE_DIFF
    # Call sequence: --check then actual apply.
    assert calls[0]["args"] == ["apply", "--check"]
    assert calls[1]["args"] == ["apply"]


async def test_apply_patch_rejects_out_of_scope_files(monkeypatch):
    # Patch tries to touch app/api.py but target_files only allows app/other.py.
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": _SAMPLE_DIFF, "target_files": ["app/other.py"]},
    )
    assert result.ok is False
    assert "patch_touches_unlisted_files" in (result.error or "")
    assert "app/api.py" in (result.error or "")


async def test_apply_patch_rejects_diff_with_no_file_headers():
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": "  hunks only, no headers\n", "target_files": ["x.py"]},
    )
    assert result.ok is False
    assert "no_file_headers" in (result.error or "")


async def test_apply_patch_check_failure_does_not_apply(monkeypatch):
    calls = _patch_git_stdin(monkeypatch, [(1, "", "error: patch does not apply")])
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": _SAMPLE_DIFF, "target_files": ["app/api.py"]},
    )
    assert result.ok is False
    assert "git_apply_check_failed" in (result.error or "")
    # Only --check ran; apply did NOT.
    assert len(calls) == 1
    assert calls[0]["args"] == ["apply", "--check"]


async def test_apply_patch_apply_fails_after_check_passes(monkeypatch):
    # Rare but possible (disk changes between check and apply).
    _patch_git_stdin(
        monkeypatch,
        [(0, "", ""), (1, "", "error: corrupt patch at line 5")],
    )
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": _SAMPLE_DIFF, "target_files": ["app/api.py"]},
    )
    assert result.ok is False
    assert "git_apply_failed" in (result.error or "")


async def test_apply_patch_git_binary_missing(monkeypatch):
    async def raise_missing(*_a, **_k):
        raise RuntimeError("git_binary_missing: /path")

    monkeypatch.setattr(coder, "_run_git_with_stdin", raise_missing)
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": _SAMPLE_DIFF, "target_files": ["app/api.py"]},
    )
    assert result.ok is False
    assert "git_binary_missing" in (result.error or "")


async def test_apply_patch_requires_diff():
    tool = tools_base.get("apply_patch")
    result = await tool.handler(_ctx(), {"target_files": ["x"]})
    assert result.ok is False
    assert "diff" in (result.error or "")


async def test_apply_patch_requires_target_files():
    tool = tools_base.get("apply_patch")
    result = await tool.handler(_ctx(), {"diff": _SAMPLE_DIFF, "target_files": []})
    assert result.ok is False
    assert "target_files" in (result.error or "")


async def test_apply_patch_rejects_non_string_target_files():
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": _SAMPLE_DIFF, "target_files": ["ok", ""]},
    )
    assert result.ok is False


async def test_apply_patch_accepts_minimal_three_dash_header(monkeypatch):
    # Some diffs skip `diff --git` but include `--- a/x` and `+++ b/x`.
    _patch_git_stdin(monkeypatch, [(0, "", ""), (0, "", "")])
    minimal = (
        "--- a/app/api.py\n"
        "+++ b/app/api.py\n"
        "@@ -1,1 +1,0 @@\n"
        "-print('x')\n"
    )
    tool = tools_base.get("apply_patch")
    result = await tool.handler(
        _ctx(),
        {"diff": minimal, "target_files": ["app/api.py"]},
    )
    assert result.ok is True
    assert "app/api.py" in result.data["applied_to"]
