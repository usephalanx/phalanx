"""Unit tests for query_fingerprint (Tier-1 memory lookup).

Patches `diagnosis._load_fingerprint_row` with a canned SimpleNamespace
(or None) so no real DB is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

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
        fingerprint_hash="abc123def4567890",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _patch_load(monkeypatch, returned):
    async def fake(_repo, _hash):
        return returned

    monkeypatch.setattr(diagnosis, "_load_fingerprint_row", fake)


async def test_query_fingerprint_hit_returns_counts_and_patch(monkeypatch):
    row = SimpleNamespace(
        tool="ruff",
        sample_errors="E501 line too long",
        seen_count=7,
        success_count=6,
        failure_count=1,
        last_good_patch_json='[{"path":"app/api.py","diff":"..."}]',
        last_good_tool_version="ruff 0.4.1",
        last_seen_at=datetime(2026, 4, 17, 12, 30, tzinfo=timezone.utc),
    )
    _patch_load(monkeypatch, row)

    tool = tools_base.get("query_fingerprint")
    result = await tool.handler(_ctx(), {})

    assert result.ok is True
    assert result.data["found"] is True
    assert result.data["seen_count"] == 7
    assert result.data["success_count"] == 6
    assert result.data["failure_count"] == 1
    assert result.data["tool"] == "ruff"
    assert "app/api.py" in result.data["last_good_patch_json"]
    assert result.data["last_good_tool_version"] == "ruff 0.4.1"
    assert result.data["last_seen_at"].startswith("2026-04-17")


async def test_query_fingerprint_miss_returns_found_false(monkeypatch):
    _patch_load(monkeypatch, None)
    tool = tools_base.get("query_fingerprint")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["found"] is False
    assert result.data["fingerprint_hash"] == "abc123def4567890"


async def test_query_fingerprint_uses_explicit_input_over_ctx(monkeypatch):
    captured = {}

    async def fake(_repo, hash_):
        captured["hash"] = hash_
        return None

    monkeypatch.setattr(diagnosis, "_load_fingerprint_row", fake)

    ctx = _ctx(fingerprint_hash="ctx_hash_123")
    tool = tools_base.get("query_fingerprint")
    await tool.handler(ctx, {"fingerprint_hash": "override_hash_xyz"})
    assert captured["hash"] == "override_hash_xyz"


async def test_query_fingerprint_requires_hash_from_input_or_ctx():
    ctx = _ctx(fingerprint_hash=None)
    tool = tools_base.get("query_fingerprint")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "fingerprint_hash" in (result.error or "")


async def test_query_fingerprint_db_failure_surfaces_as_error(monkeypatch):
    async def raise_db(_repo, _hash):
        raise RuntimeError("db conn lost")

    monkeypatch.setattr(diagnosis, "_load_fingerprint_row", raise_db)
    tool = tools_base.get("query_fingerprint")
    result = await tool.handler(_ctx(), {})
    assert result.ok is False
    assert "db_query_failed" in (result.error or "")


async def test_query_fingerprint_row_with_missing_optionals(monkeypatch):
    # Minimal row — only counts; optional columns are None.
    row = SimpleNamespace(
        tool="mypy",
        sample_errors=None,
        seen_count=1,
        success_count=0,
        failure_count=1,
        last_good_patch_json=None,
        last_good_tool_version=None,
        last_seen_at=None,
    )
    _patch_load(monkeypatch, row)
    tool = tools_base.get("query_fingerprint")
    result = await tool.handler(_ctx(), {})
    assert result.ok is True
    assert result.data["found"] is True
    assert result.data["sample_errors"] == ""
    assert result.data["last_good_patch_json"] == ""
    assert result.data["last_good_tool_version"] == ""
    assert result.data["last_seen_at"] == ""
