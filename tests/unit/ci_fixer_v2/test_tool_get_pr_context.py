"""Unit tests for the get_pr_context tool.

Patches `diagnosis._call_github_api` directly so no HTTP fires.
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


def _ctx(**overrides) -> AgentContext:
    defaults = dict(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
        ci_api_key="tok",
        pr_number=42,
        has_write_permission=True,
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _fake_pr_payload() -> dict:
    return {
        "number": 42,
        "title": "fix: remove unused import",
        "body": "As discussed in issue #10.",
        "state": "open",
        "user": {"login": "alice"},
        "head": {"ref": "fix/unused-import"},
        "base": {"ref": "main"},
        "labels": [{"name": "bug"}, {"name": "lint"}],
        "created_at": "2026-04-18T10:00:00Z",
        "updated_at": "2026-04-18T11:00:00Z",
    }


def _patch_gh(monkeypatch, *, status: int, body):
    async def fake(_path, _api_key, accept="application/vnd.github+json"):
        return (status, "", body)

    monkeypatch.setattr(diagnosis, "_call_github_api", fake)


async def test_get_pr_context_happy_path(monkeypatch):
    _patch_gh(monkeypatch, status=200, body=_fake_pr_payload())

    ctx = _ctx()
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})

    assert result.ok is True
    pr = result.data["pr"]
    assert pr["number"] == 42
    assert pr["title"] == "fix: remove unused import"
    assert pr["author"] == "alice"
    assert pr["head_branch"] == "fix/unused-import"
    assert pr["base_branch"] == "main"
    assert pr["labels"] == ["bug", "lint"]
    assert result.data["has_write_permission"] is True


async def test_get_pr_context_defaults_pr_number_from_ctx(monkeypatch):
    captured_path: dict[str, str] = {}

    async def fake(path, _api_key, accept="application/vnd.github+json"):
        captured_path["p"] = path
        return (200, "", _fake_pr_payload())

    monkeypatch.setattr(diagnosis, "_call_github_api", fake)

    ctx = _ctx(pr_number=99)
    tool = tools_base.get("get_pr_context")
    await tool.handler(ctx, {})  # no pr_number in input
    assert "/pulls/99" in captured_path["p"]


async def test_get_pr_context_missing_pr_number_returns_error(monkeypatch):
    # Neither input nor ctx has pr_number.
    ctx = _ctx(pr_number=None)
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "pr_number" in (result.error or "")


async def test_get_pr_context_missing_api_key_returns_error():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_get_pr_context_non_200_returns_error(monkeypatch):
    _patch_gh(monkeypatch, status=404, body=None)
    ctx = _ctx()
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "404" in (result.error or "")


async def test_get_pr_context_wraps_exception(monkeypatch):
    async def raise_boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(diagnosis, "_call_github_api", raise_boom)
    ctx = _ctx()
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "github_call_failed" in (result.error or "")


async def test_get_pr_context_handles_missing_fields(monkeypatch):
    # Minimal body — fields absent should not crash; defaults to empty strings.
    _patch_gh(monkeypatch, status=200, body={"number": 1})
    ctx = _ctx()
    tool = tools_base.get("get_pr_context")
    result = await tool.handler(ctx, {})
    assert result.ok is True
    assert result.data["pr"]["title"] == ""
    assert result.data["pr"]["author"] == ""
    assert result.data["pr"]["labels"] == []


async def test_get_pr_context_reports_has_write_permission_false():
    # Tool reflects AgentContext.has_write_permission — tests the False path.
    async def fake(*_a, **_k):
        return (200, "", _fake_pr_payload())

    ctx = _ctx(has_write_permission=False)
    tool = tools_base.get("get_pr_context")
    # Patch using monkeypatch-like attribute swap; we're not using monkeypatch fixture here
    import phalanx.ci_fixer_v2.tools.diagnosis as diag_mod

    original = diag_mod._call_github_api
    diag_mod._call_github_api = fake
    try:
        result = await tool.handler(ctx, {})
    finally:
        diag_mod._call_github_api = original

    assert result.ok is True
    assert result.data["has_write_permission"] is False
