"""Unit tests for git_blame (+ porcelain parser)."""

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


def _ctx(workspace: str) -> AgentContext:
    return AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path=workspace,
        original_failing_command="ruff check app/",
    )


_SAMPLE_PORCELAIN = """\
abcd1234abcd1234abcd1234abcd1234abcd1234 10 10 1
author Alice
author-mail <alice@example.com>
author-time 1713398400
author-tz +0000
committer Alice
committer-mail <alice@example.com>
committer-time 1713398400
committer-tz +0000
summary add hello endpoint
filename app/api.py
\t    return 'hi'
abcd1234abcd1234abcd1234abcd1234abcd1234 11 11 1
\t    return 'bye'
ef009988ef009988ef009988ef009988ef009988 12 12 1
author Bob
author-mail <bob@example.com>
author-time 1713484800
author-tz +0000
committer Bob
committer-mail <bob@example.com>
committer-time 1713484800
committer-tz +0000
summary tweak copy
filename app/api.py
\tprint('added later')
"""


async def test_git_blame_happy_path(tmp_path, monkeypatch):
    # Create the file git-blame claims to inspect.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text("a\nb\nc\n", encoding="utf-8")

    async def fake_run(_ws, _file, _s, _e):
        return (0, _SAMPLE_PORCELAIN, "")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"file": "app/api.py", "line_start": 10, "line_end": 12},
    )
    assert result.ok is True
    lines = result.data["lines"]
    assert len(lines) == 3
    assert lines[0]["line"] == 10
    assert lines[0]["sha"].startswith("abcd1234")
    assert lines[0]["author"] == "Alice"
    assert lines[0]["summary"] == "add hello endpoint"
    # Second entry reuses same sha — metadata must still be attached.
    assert lines[1]["line"] == 11
    assert lines[1]["author"] == "Alice"
    assert lines[1]["summary"] == "add hello endpoint"
    # Third entry is a different sha with different author.
    assert lines[2]["line"] == 12
    assert lines[2]["sha"].startswith("ef009988")
    assert lines[2]["author"] == "Bob"
    assert lines[2]["summary"] == "tweak copy"
    # Dates are ISO-formatted.
    assert lines[0]["date"].startswith("2024-")  # epoch 1713398400 ≈ 2024-04-17


async def test_git_blame_line_end_defaults_to_line_start(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("hello\n", encoding="utf-8")

    captured = {}

    async def fake_run(_ws, _file, s, e):
        captured["s"] = s
        captured["e"] = e
        return (0, "", "")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    await tool.handler(_ctx(str(tmp_path)), {"file": "f.py", "line_start": 5})
    assert captured["s"] == 5
    assert captured["e"] == 5


async def test_git_blame_missing_file(tmp_path, monkeypatch):
    async def fake_run(*_a):  # should not be called
        raise AssertionError("subprocess should not be reached")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"file": "missing.py", "line_start": 1},
    )
    assert result.ok is False
    assert "missing" in (result.error or "")


async def test_git_blame_rejects_traversal(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.py").write_text("x", encoding="utf-8")

    async def fake_run(*_a):
        raise AssertionError("should not reach subprocess")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    result = await tool.handler(
        _ctx(str(ws)),
        {"file": "../outside.py", "line_start": 1},
    )
    assert result.ok is False


async def test_git_blame_requires_file():
    tool = tools_base.get("git_blame")
    result = await tool.handler(_ctx("/tmp"), {"line_start": 1})
    assert result.ok is False
    assert "file" in (result.error or "")


async def test_git_blame_requires_line_start():
    tool = tools_base.get("git_blame")
    result = await tool.handler(_ctx("/tmp"), {"file": "x.py"})
    assert result.ok is False
    assert "line_start" in (result.error or "")


async def test_git_blame_subprocess_failure(tmp_path, monkeypatch):
    (tmp_path / "x.py").write_text("a\n", encoding="utf-8")

    async def fake_run(*_a):
        return (128, "", "fatal: not a git repository\n")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"file": "x.py", "line_start": 1},
    )
    assert result.ok is False
    assert "git_blame_failed" in (result.error or "")
    assert "not a git repository" in (result.error or "")


async def test_git_blame_git_binary_missing(tmp_path, monkeypatch):
    (tmp_path / "x.py").write_text("a\n", encoding="utf-8")

    async def fake_run(*_a):
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(diagnosis, "_run_git_blame", fake_run)

    tool = tools_base.get("git_blame")
    result = await tool.handler(
        _ctx(str(tmp_path)),
        {"file": "x.py", "line_start": 1},
    )
    assert result.ok is False
    assert "git_binary_missing" in (result.error or "")
