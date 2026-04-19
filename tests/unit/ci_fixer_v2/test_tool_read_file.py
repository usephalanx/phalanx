"""Unit tests for the read_file tool.

Uses real tmp_path so the path-traversal protection is exercised
against actual filesystem state, not mocks.
"""

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


def _ctx_with_workspace(workspace: str) -> AgentContext:
    return AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path=workspace,
        original_failing_command="ruff check app/",
    )


async def test_read_file_full_content(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("print('hi')\nprint('bye')\n", encoding="utf-8")
    ctx = _ctx_with_workspace(str(tmp_path))

    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "hello.py"})
    assert result.ok is True
    assert result.data["content"] == "print('hi')\nprint('bye')\n"
    assert result.data["line_count"] == 2
    assert result.data["line_start"] == 1
    assert result.data["line_end"] == 2


async def test_read_file_line_range_slice(tmp_path):
    f = tmp_path / "multi.py"
    f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    ctx = _ctx_with_workspace(str(tmp_path))

    tool = tools_base.get("read_file")
    result = await tool.handler(
        ctx, {"path": "multi.py", "line_start": 2, "line_end": 4}
    )
    assert result.ok is True
    assert result.data["content"] == "b\nc\nd\n"
    assert result.data["line_count"] == 5  # total in file
    assert result.data["line_start"] == 2
    assert result.data["line_end"] == 4


async def test_read_file_line_start_only_runs_to_end(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("1\n2\n3\n", encoding="utf-8")
    ctx = _ctx_with_workspace(str(tmp_path))

    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "t.py", "line_start": 2})
    assert result.ok is True
    assert result.data["content"] == "2\n3\n"


async def test_read_file_rejects_traversal(tmp_path):
    # Put a file OUTSIDE the workspace.
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ctx = _ctx_with_workspace(str(workspace))
    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "../outside.txt"})

    assert result.ok is False
    assert "path_outside_workspace" in (result.error or "")


async def test_read_file_not_found(tmp_path):
    ctx = _ctx_with_workspace(str(tmp_path))
    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "nope.py"})
    assert result.ok is False
    assert "file_not_found" in (result.error or "")


async def test_read_file_rejects_directory(tmp_path):
    subdir = tmp_path / "pkg"
    subdir.mkdir()
    ctx = _ctx_with_workspace(str(tmp_path))
    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "pkg"})
    assert result.ok is False
    assert "not_a_file" in (result.error or "")


async def test_read_file_bad_line_range():
    ctx = _ctx_with_workspace("/tmp")
    tool = tools_base.get("read_file")
    result = await tool.handler(
        ctx, {"path": "x", "line_start": 5, "line_end": 2}
    )
    assert result.ok is False
    assert "line_end" in (result.error or "")


async def test_read_file_refuses_huge_file_without_range(tmp_path, monkeypatch):
    # Patch the size cap low so we don't have to write 256 KiB in tests.
    monkeypatch.setattr(reading, "_MAX_READ_BYTES", 10)

    f = tmp_path / "big.txt"
    f.write_text("x" * 50, encoding="utf-8")  # 50 bytes > 10 cap
    ctx = _ctx_with_workspace(str(tmp_path))

    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "big.txt"})
    assert result.ok is False
    assert "file_too_large" in (result.error or "")


async def test_read_file_huge_file_with_range_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(reading, "_MAX_READ_BYTES", 10)
    f = tmp_path / "big.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    ctx = _ctx_with_workspace(str(tmp_path))

    tool = tools_base.get("read_file")
    result = await tool.handler(
        ctx, {"path": "big.txt", "line_start": 2, "line_end": 2}
    )
    assert result.ok is True
    assert result.data["content"] == "line2\n"


async def test_read_file_missing_path():
    ctx = _ctx_with_workspace("/tmp")
    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "path" in (result.error or "")


async def test_read_file_rejects_when_workspace_does_not_exist(tmp_path):
    # Point ctx at a non-existent workspace — the resolver should return
    # None and the handler should surface path_outside_workspace rather
    # than raising.
    missing = tmp_path / "never_created"
    ctx = _ctx_with_workspace(str(missing))
    tool = tools_base.get("read_file")
    result = await tool.handler(ctx, {"path": "whatever.py"})
    assert result.ok is False
    assert "path_outside_workspace" in (result.error or "")
