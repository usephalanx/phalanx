"""Unit tests for the fetch_ci_log tool.

Tests monkeypatch the `_fetch_log_via_v1` seam so no HTTP is made. Each
test covers a branch of the handler: happy path, missing api key, unknown
provider (simulated by patching the seam to raise KeyError), generic
fetch failure, and missing required input.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg  # registers builtins
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
        ci_api_key="test-token",
        ci_provider="github_actions",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


async def test_fetch_ci_log_happy_path(monkeypatch):
    captured_args: dict[str, object] = {}

    async def fake_fetch(provider, repo_full_name, build_id, failed_jobs, pr_number, api_key):
        captured_args["provider"] = provider
        captured_args["repo_full_name"] = repo_full_name
        captured_args["build_id"] = build_id
        captured_args["failed_jobs"] = failed_jobs
        captured_args["api_key"] = api_key
        return "FAILED tests/test_foo.py::test_bar\nAssertionError\n"

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", fake_fetch)

    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "job-42", "failed_jobs": ["lint"]})

    assert result.ok is True
    assert "AssertionError" in result.data["log_text"]
    assert result.data["provider"] == "github_actions"
    assert result.data["job_id"] == "job-42"
    assert result.data["char_count"] == len(result.data["log_text"])
    # Seam was called with the right repo + api key from context.
    assert captured_args["repo_full_name"] == "acme/widget"
    assert captured_args["api_key"] == "test-token"
    assert captured_args["build_id"] == "job-42"
    assert captured_args["failed_jobs"] == ["lint"]


async def test_fetch_ci_log_missing_job_id_rejected():
    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "job_id" in (result.error or "")


async def test_fetch_ci_log_missing_api_key_rejected():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_fetch_ci_log_unknown_provider_returns_clean_error(monkeypatch):
    async def raise_keyerror(*_a, **_k):
        raise KeyError("nonesuch")

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", raise_keyerror)
    ctx = _ctx(ci_provider="nonesuch")
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "unsupported_provider" in (result.error or "")


async def test_fetch_ci_log_wraps_generic_exception_as_fetch_failed(monkeypatch):
    async def raise_runtime(*_a, **_k):
        raise RuntimeError("HTTP 502")

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", raise_runtime)
    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "fetch_failed" in (result.error or "")
