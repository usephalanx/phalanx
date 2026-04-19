"""Unit tests for comment_on_pr."""

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
        ci_api_key="tok",
        pr_number=42,
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _patch_post(monkeypatch, *, status: int, body):
    captured: dict = {}

    async def fake(path, _api_key, json_body):
        captured["path"] = path
        captured["json_body"] = json_body
        return (status, "", body)

    monkeypatch.setattr(action, "_call_github_post", fake)
    return captured


async def test_comment_on_pr_happy_path(monkeypatch):
    captured = _patch_post(
        monkeypatch,
        status=201,
        body={"id": 999, "html_url": "https://github.com/x/y/issues/42#comment-999"},
    )
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(_ctx(), {"body": "Fixed lint E501."})
    assert result.ok is True
    assert result.data["comment_id"] == 999
    assert "#comment-999" in result.data["url"]
    assert result.data["pr_number"] == 42
    assert captured["path"] == "/repos/acme/widget/issues/42/comments"
    assert captured["json_body"] == {"body": "Fixed lint E501."}


async def test_comment_on_pr_uses_ctx_pr_number_when_missing_from_input(monkeypatch):
    captured = _patch_post(monkeypatch, status=201, body={"id": 1, "html_url": ""})
    tool = tools_base.get("comment_on_pr")
    await tool.handler(_ctx(pr_number=77), {"body": "x"})
    assert "/issues/77/" in captured["path"]


async def test_comment_on_pr_rejects_empty_body():
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(_ctx(), {"body": ""})
    assert result.ok is False
    assert "body" in (result.error or "")


async def test_comment_on_pr_missing_pr_number():
    ctx = _ctx(pr_number=None)
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(ctx, {"body": "x"})
    assert result.ok is False
    assert "pr_number" in (result.error or "")


async def test_comment_on_pr_missing_api_key():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(ctx, {"body": "x"})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_comment_on_pr_non_201_is_error(monkeypatch):
    _patch_post(monkeypatch, status=500, body=None)
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(_ctx(), {"body": "x"})
    assert result.ok is False
    assert "500" in (result.error or "")


async def test_comment_on_pr_wraps_exception(monkeypatch):
    async def raise_boom(*_a, **_k):
        raise RuntimeError("net")

    monkeypatch.setattr(action, "_call_github_post", raise_boom)
    tool = tools_base.get("comment_on_pr")
    result = await tool.handler(_ctx(), {"body": "x"})
    assert result.ok is False
    assert "github_call_failed" in (result.error or "")
