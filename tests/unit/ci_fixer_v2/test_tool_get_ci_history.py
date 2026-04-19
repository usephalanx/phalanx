"""Unit tests for get_ci_history (flake detection).

Patches `diagnosis._call_github_api` with a fake workflow-runs payload.
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
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _run(name: str, conclusion: str | None, sha: str = "abc", msg: str = "commit"):
    return {
        "name": name,
        "conclusion": conclusion,
        "head_sha": sha,
        "created_at": "2026-04-18T10:00:00Z",
        "html_url": f"https://github.com/x/y/actions/runs/{sha}",
        "head_commit": {"message": msg},
    }


def _patch(monkeypatch, runs, status=200):
    async def fake(_path, _api_key, accept="application/vnd.github+json"):
        return (status, "", {"workflow_runs": runs})

    monkeypatch.setattr(diagnosis, "_call_github_api", fake)


async def test_get_ci_history_flake_rate_over_threshold(monkeypatch):
    # 3 failed / 2 passed → flake_rate = 3/5 = 0.6.
    _patch(
        monkeypatch,
        [
            _run("CI", "failure", "s1"),
            _run("CI", "success", "s2"),
            _run("CI", "failure", "s3"),
            _run("CI", "failure", "s4"),
            _run("CI", "success", "s5"),
        ],
    )

    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {"days": 7})
    assert result.ok is True
    assert result.data["passed"] == 2
    assert result.data["failed"] == 3
    assert result.data["flake_rate"] == 0.6
    assert result.data["total"] == 5
    assert result.data["branch"] == "main"


async def test_get_ci_history_passes_only_flake_rate_zero(monkeypatch):
    _patch(monkeypatch, [_run("CI", "success") for _ in range(5)])
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["flake_rate"] == 0.0


async def test_get_ci_history_ignores_skipped_cancelled_neutral(monkeypatch):
    # 'other' conclusions are not counted in flake_rate denominator.
    _patch(
        monkeypatch,
        [
            _run("CI", "success"),
            _run("CI", "failure"),
            _run("CI", "skipped"),
            _run("CI", "cancelled"),
            _run("CI", None),  # no conclusion yet
        ],
    )
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    # 1 passed + 1 failed counted; 3 "other"s excluded from denominator.
    assert result.data["passed"] == 1
    assert result.data["failed"] == 1
    assert result.data["flake_rate"] == 0.5


async def test_get_ci_history_filters_by_test_identifier(monkeypatch):
    _patch(
        monkeypatch,
        [
            _run("Unit Tests", "failure", msg="fix auth"),
            _run("Lint", "success", msg="lint cleanup"),
            _run("CI", "failure", msg="auth refactor"),  # matches "auth" in msg
        ],
    )
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {"test_identifier": "auth"})
    # Two of three runs match; both are failures.
    assert result.ok is True
    assert result.data["total"] == 2
    assert result.data["failed"] == 2


async def test_get_ci_history_days_clamped(monkeypatch):
    _patch(monkeypatch, [])
    tool = tools_base.get("get_ci_history")
    # days=1000 should clamp to 90 and still succeed.
    result = await tool.handler(_ctx(), {"days": 1000})
    assert result.ok is True
    assert result.data["days"] == 90


async def test_get_ci_history_empty_window(monkeypatch):
    _patch(monkeypatch, [])
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["total"] == 0
    assert result.data["flake_rate"] == 0.0


async def test_get_ci_history_api_error(monkeypatch):
    _patch(monkeypatch, [], status=503)
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "503" in (result.error or "")


async def test_get_ci_history_missing_api_key():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_get_ci_history_wraps_exception(monkeypatch):
    async def raise_boom(*_a, **_k):
        raise RuntimeError("dns")

    monkeypatch.setattr(diagnosis, "_call_github_api", raise_boom)
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "github_call_failed" in (result.error or "")


async def test_get_ci_history_ignores_non_dict_entries(monkeypatch):
    _patch(monkeypatch, [_run("CI", "failure"), "not-a-dict", None])
    tool = tools_base.get("get_ci_history")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["failed"] == 1
    assert result.data["total"] == 1
