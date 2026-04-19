"""Unit tests for grep (workspace content search)."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools import reading


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx(workspace: str) -> AgentContext:
    return AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path=workspace,
        original_failing_command="ruff check app/",
    )


async def test_grep_finds_matches(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text(
        "import os\n\ndef hello():\n    return 'hi'\n", encoding="utf-8"
    )
    (tmp_path / "app" / "b.py").write_text(
        "def hello():\n    return 'bye'\n", encoding="utf-8"
    )

    tool = tools_base.get("grep")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "def hello"})
    assert result.ok is True
    paths = sorted(m["file"] for m in result.data["matches"])
    assert paths == ["app/a.py", "app/b.py"]
    for m in result.data["matches"]:
        assert "def hello" in m["text"]


async def test_grep_case_insensitive(tmp_path):
    (tmp_path / "f.py").write_text("RAISE ValueError\nraise TypeError\n", encoding="utf-8")
    tool = tools_base.get("grep")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "raise", "case_insensitive": True},
    )
    assert result.ok is True
    assert result.data["match_count"] == 2


async def test_grep_respects_path_subdirectory(tmp_path):
    (tmp_path / "in_scope").mkdir()
    (tmp_path / "in_scope" / "x.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "out_scope").mkdir()
    (tmp_path / "out_scope" / "y.py").write_text("target\n", encoding="utf-8")

    tool = tools_base.get("grep")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "target", "path": "in_scope"},
    )
    assert result.ok is True
    assert result.data["match_count"] == 1
    assert result.data["matches"][0]["file"] == "in_scope/x.py"


async def test_grep_skips_excluded_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "objects").mkdir()
    (tmp_path / ".git" / "objects" / "pack").write_text("match me\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo.js").write_text("match me\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("match me\n", encoding="utf-8")

    tool = tools_base.get("grep")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "match me"})
    assert result.ok is True
    paths = [m["file"] for m in result.data["matches"]]
    assert paths == ["src.py"]


async def test_grep_truncates_at_max_matches(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("hit\n", encoding="utf-8")
    tool = tools_base.get("grep")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "hit", "max_matches": 5},
    )
    assert result.ok is True
    assert result.data["match_count"] == 5
    assert result.data["truncated"] is True


async def test_grep_invalid_regex(tmp_path):
    tool = tools_base.get("grep")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "("})
    assert result.ok is False
    assert "invalid_regex" in (result.error or "")


async def test_grep_missing_pattern():
    tool = tools_base.get("grep")
    result = await tool.handler(_ctx("/tmp"), {})
    assert result.ok is False
    assert "pattern" in (result.error or "")


async def test_grep_path_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = tools_base.get("grep")
    result = await tool.handler(_ctx(str(ws)), {"pattern": "x", "path": "../.."})
    assert result.ok is False
    assert "path_outside_workspace" in (result.error or "")


async def test_grep_path_not_found(tmp_path):
    tool = tools_base.get("grep")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "x", "path": "nonexistent"},
    )
    assert result.ok is False
    assert "path_not_found" in (result.error or "")


async def test_grep_skips_huge_files(tmp_path, monkeypatch):
    # Lower the per-file size cap so we don't have to write megabytes.
    monkeypatch.setattr(reading, "_GREP_MAX_FILE_BYTES", 10)
    (tmp_path / "small.txt").write_text("hit\n", encoding="utf-8")
    (tmp_path / "big.txt").write_text("x" * 50 + "\nhit\n", encoding="utf-8")

    tool = tools_base.get("grep")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "hit"})
    assert result.ok is True
    paths = [m["file"] for m in result.data["matches"]]
    assert paths == ["small.txt"]
