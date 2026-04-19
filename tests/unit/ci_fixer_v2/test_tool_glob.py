"""Unit tests for glob (workspace file-pattern search)."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base


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
        original_failing_command="x",
    )


async def test_glob_recursive_double_star(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "sub").mkdir()
    (tmp_path / "app" / "sub" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "readme.md").write_text("", encoding="utf-8")

    tool = tools_base.get("glob")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "**/*.py"})
    assert result.ok is True
    assert sorted(result.data["files"]) == ["app/a.py", "app/sub/b.py"]


async def test_glob_single_level(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    (tmp_path / "y.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "z.py").write_text("", encoding="utf-8")

    tool = tools_base.get("glob")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "*.py"})
    assert result.ok is True
    assert sorted(result.data["files"]) == ["x.py", "y.py"]


async def test_glob_respects_path_scoping(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "api.py").write_text("", encoding="utf-8")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "app.py").write_text("", encoding="utf-8")

    tool = tools_base.get("glob")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "*.py", "path": "backend"},
    )
    assert result.ok is True
    assert result.data["files"] == ["backend/api.py"]


async def test_glob_excludes_standard_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo.ts").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bar.ts").write_text("", encoding="utf-8")

    tool = tools_base.get("glob")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "**/*.ts"})
    assert result.ok is True
    assert result.data["files"] == ["src/bar.ts"]


async def test_glob_truncates_at_max_files(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.py").write_text("", encoding="utf-8")
    tool = tools_base.get("glob")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "*.py", "max_files": 5},
    )
    assert result.ok is True
    assert len(result.data["files"]) == 5
    assert result.data["truncated"] is True


async def test_glob_missing_pattern():
    tool = tools_base.get("glob")
    result = await tool.handler(_ctx("/tmp"), {})
    assert result.ok is False
    assert "pattern" in (result.error or "")


async def test_glob_path_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = tools_base.get("glob")
    result = await tool.handler(
        _ctx(str(ws)),
        {"pattern": "*", "path": "../.."},
    )
    assert result.ok is False
    assert "path_outside_workspace" in (result.error or "")


async def test_glob_path_not_a_directory(tmp_path):
    (tmp_path / "f.py").write_text("", encoding="utf-8")
    tool = tools_base.get("glob")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"pattern": "*", "path": "f.py"},
    )
    assert result.ok is False
    assert "not_a_directory" in (result.error or "")


async def test_glob_skips_directories_in_match(tmp_path):
    # `*` matches both files and dirs via pathlib — we must filter to files.
    (tmp_path / "file.py").write_text("", encoding="utf-8")
    (tmp_path / "dir").mkdir()

    tool = tools_base.get("glob")
    result = await tool.handler(_ctx(str(tmp_path)), {"pattern": "*"})
    assert result.ok is True
    assert result.data["files"] == ["file.py"]
