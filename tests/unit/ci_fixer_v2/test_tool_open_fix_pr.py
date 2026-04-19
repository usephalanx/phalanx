"""Unit tests for open_fix_pr_against_author_branch."""

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
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _valid_input() -> dict:
    return {
        "title": "Fix: ruff E501 in app/api.py",
        "body": "Diagnosis: long line. Fix: wrap with parens.",
        "head_branch": "phalanx/ci-fix/run-123",
        "base_branch": "feature/author-pr",
    }


def _patch_post(monkeypatch, *, status: int, body):
    captured = {}

    async def fake(path, _api_key, json_body):
        captured["path"] = path
        captured["json_body"] = json_body
        return (status, "", body)

    monkeypatch.setattr(action, "_call_github_post", fake)
    return captured


async def test_open_fix_pr_happy_path(monkeypatch):
    captured = _patch_post(
        monkeypatch,
        status=201,
        body={"number": 101, "html_url": "https://github.com/x/y/pull/101"},
    )
    tool = tools_base.get("open_fix_pr_against_author_branch")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is True
    assert result.data["pr_number"] == 101
    assert result.data["pr_url"].endswith("/pull/101")
    assert captured["path"] == "/repos/acme/widget/pulls"
    assert captured["json_body"]["head"] == "phalanx/ci-fix/run-123"
    assert captured["json_body"]["base"] == "feature/author-pr"
    assert captured["json_body"]["title"] == "Fix: ruff E501 in app/api.py"


@pytest.mark.parametrize(
    "missing_field", ["title", "body", "head_branch", "base_branch"]
)
async def test_open_fix_pr_requires_all_fields(missing_field):
    inp = _valid_input()
    inp[missing_field] = ""
    tool = tools_base.get("open_fix_pr_against_author_branch")
    result = await tool.handler(_ctx(), inp)
    assert result.ok is False
    assert missing_field in (result.error or "")


async def test_open_fix_pr_missing_api_key():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("open_fix_pr_against_author_branch")
    result = await tool.handler(ctx, _valid_input())
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_open_fix_pr_non_201(monkeypatch):
    _patch_post(monkeypatch, status=422, body={"message": "No commits between"})
    tool = tools_base.get("open_fix_pr_against_author_branch")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "422" in (result.error or "")


async def test_open_fix_pr_wraps_exception(monkeypatch):
    async def raise_boom(*_a, **_k):
        raise RuntimeError("dns")

    monkeypatch.setattr(action, "_call_github_post", raise_boom)
    tool = tools_base.get("open_fix_pr_against_author_branch")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "github_call_failed" in (result.error or "")
