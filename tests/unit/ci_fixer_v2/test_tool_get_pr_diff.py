"""Unit tests for get_pr_diff.

Makes two calls (raw-diff + files-stats) — tests verify both branches
of the call sequence and the combined output shape.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools import diagnosis


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
        ci_api_key="tok",
        pr_number=42,
    )


def _make_dual_call_fake(diff_text: str, files_body, diff_status=200, files_status=200):
    """Returns a fake _call_github_api that branches on `accept` header:
    diff request → diff_text; files request → files_body.
    """
    async def fake(path, _api_key, accept="application/vnd.github+json"):
        if accept == "application/vnd.github.diff":
            return (diff_status, diff_text, None)
        if path.endswith("/files?per_page=100"):
            return (files_status, "", files_body)
        raise AssertionError(f"unexpected call path={path} accept={accept}")

    return fake


async def test_get_pr_diff_happy_path(monkeypatch):
    diff_text = (
        "diff --git a/app/api.py b/app/api.py\n"
        "--- a/app/api.py\n+++ b/app/api.py\n"
        "@@ -1,2 +1,1 @@\n-import os\n def hello(): return 'hi'\n"
    )
    files_body = [
        {"filename": "app/api.py", "additions": 0, "deletions": 1, "status": "modified"},
    ]
    monkeypatch.setattr(
        diagnosis,
        "_call_github_api",
        _make_dual_call_fake(diff_text, files_body),
    )

    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["diff"] == diff_text
    assert result.data["file_count"] == 1
    assert result.data["files_changed"][0] == {
        "path": "app/api.py",
        "additions": 0,
        "deletions": 1,
        "status": "modified",
    }


async def test_get_pr_diff_missing_pr_number():
    ctx = AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="x",
        ci_api_key="tok",
    )
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "pr_number" in (result.error or "")


async def test_get_pr_diff_no_api_key():
    ctx = AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="x",
        pr_number=7,
    )
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_get_pr_diff_api_error_on_diff(monkeypatch):
    monkeypatch.setattr(
        diagnosis,
        "_call_github_api",
        _make_dual_call_fake("", [], diff_status=500),
    )
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "status=500" in (result.error or "")


async def test_get_pr_diff_api_error_on_files(monkeypatch):
    monkeypatch.setattr(
        diagnosis,
        "_call_github_api",
        _make_dual_call_fake("diff-text", None, files_status=503),
    )
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "files" in (result.error or "")


async def test_get_pr_diff_empty_files_list(monkeypatch):
    monkeypatch.setattr(
        diagnosis,
        "_call_github_api",
        _make_dual_call_fake("diff", []),
    )
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["file_count"] == 0
    assert result.data["files_changed"] == []


async def test_get_pr_diff_wraps_exception_on_diff_call(monkeypatch):
    async def raise_boom(*_a, **_k):
        raise RuntimeError("dns fail")

    monkeypatch.setattr(diagnosis, "_call_github_api", raise_boom)
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "diff_fetch_failed" in (result.error or "")


async def test_get_pr_diff_wraps_exception_on_files_call(monkeypatch):
    call_count = {"n": 0}

    async def fake(path, _api_key, accept="application/vnd.github+json"):
        call_count["n"] += 1
        if accept == "application/vnd.github.diff":
            return (200, "diff-ok", None)
        raise RuntimeError("files boom")

    monkeypatch.setattr(diagnosis, "_call_github_api", fake)
    tool = tools_base.get("get_pr_diff")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "files_fetch_failed" in (result.error or "")
    assert call_count["n"] == 2  # diff succeeded, files raised
