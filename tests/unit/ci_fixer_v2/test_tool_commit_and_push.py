"""Unit tests for commit_and_push.

Patches the `_run_git_command` seam with a scripted queue of (ec, stdout, stderr)
tuples so every git step's outcome is controllable without spawning git.
"""

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
        ci_fix_run_id="run-12345678",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
        has_write_permission=True,
        author_head_branch="feature/add-auth",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _patch_git(monkeypatch, script: list[tuple[int, str, str]]):
    """Install a queue-based fake. Each call consumes one tuple."""
    calls: list[list[str]] = []
    iterator = iter(script)

    async def fake(_workspace, args, timeout=60):
        calls.append(list(args))
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError(
                f"git called more times than script provided; last args={args}"
            )

    monkeypatch.setattr(action, "_run_git_command", fake)
    return calls


def _valid_input(strategy="author_branch") -> dict:
    return {
        "branch_strategy": strategy,
        "commit_message": "fix(lint): wrap long line in app/api.py",
        "files": ["app/api.py"],
    }


async def test_commit_and_push_author_branch_happy_path(monkeypatch):
    calls = _patch_git(
        monkeypatch,
        [
            (0, "", ""),              # checkout -B
            (0, "", ""),              # add
            (0, "", ""),              # commit
            (0, "abc123sha\n", ""),   # rev-parse HEAD
            (0, "", ""),              # push
        ],
    )
    ctx = _ctx()
    # Pre-set verification so the loop gate would pass; the tool itself
    # doesn't check this (loop does), but we assert the tool *clears* it.
    ctx.last_sandbox_verified = True

    tool = tools_base.get("commit_and_push")
    result = await tool.handler(ctx, _valid_input())
    assert result.ok is True
    assert result.data["sha"] == "abc123sha"
    assert result.data["branch"] == "feature/add-auth"
    assert result.data["strategy"] == "author_branch"
    assert result.data["pushed"] is True
    assert result.data["files_committed"] == ["app/api.py"]
    # Verification flag invalidated — any follow-up commit needs re-verify.
    assert ctx.last_sandbox_verified is False

    # Git call sequence is what we expect.
    assert calls[0][:2] == ["checkout", "-B"]
    assert calls[0][2] == "feature/add-auth"
    assert calls[1] == ["add", "--", "app/api.py"]
    assert calls[2][-3:] == ["commit", "-m", "fix(lint): wrap long line in app/api.py"]
    assert calls[3] == ["rev-parse", "HEAD"]
    assert calls[4] == ["push", "--set-upstream", "origin", "feature/add-auth"]


async def test_commit_and_push_fix_branch_uses_phalanx_prefix(monkeypatch):
    _patch_git(
        monkeypatch,
        [(0, "", ""), (0, "", ""), (0, "", ""), (0, "feedcafe\n", ""), (0, "", "")],
    )
    ctx = _ctx(has_write_permission=False)
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(ctx, _valid_input(strategy="fix_branch"))
    assert result.ok is True
    assert result.data["branch"] == "phalanx/ci-fix/run-12345678"


async def test_commit_and_push_author_branch_without_write_perm_rejected():
    ctx = _ctx(has_write_permission=False)
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(ctx, _valid_input(strategy="author_branch"))
    assert result.ok is False
    assert "write_permission" in (result.error or "")


async def test_commit_and_push_author_branch_without_author_head_rejected():
    ctx = _ctx(author_head_branch=None)
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(ctx, _valid_input(strategy="author_branch"))
    assert result.ok is False
    assert "author_head_branch" in (result.error or "")


async def test_commit_and_push_invalid_strategy():
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(
        _ctx(), {**_valid_input(), "branch_strategy": "frobnicate"}
    )
    assert result.ok is False
    assert "author_branch" in (result.error or "")  # enum listed


async def test_commit_and_push_requires_message():
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), {**_valid_input(), "commit_message": ""})
    assert result.ok is False
    assert "commit_message" in (result.error or "")


async def test_commit_and_push_requires_non_empty_files():
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), {**_valid_input(), "files": []})
    assert result.ok is False
    assert "files" in (result.error or "")


async def test_commit_and_push_rejects_non_string_file_entries():
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), {**_valid_input(), "files": ["ok.py", ""]})
    assert result.ok is False


async def test_commit_and_push_checkout_failure(monkeypatch):
    _patch_git(monkeypatch, [(128, "", "error: pathspec did not match")])
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_checkout_failed" in (result.error or "")


async def test_commit_and_push_add_failure(monkeypatch):
    _patch_git(
        monkeypatch,
        [(0, "", ""), (1, "", "fatal: pathspec 'app/api.py' did not match any files")],
    )
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_add_failed" in (result.error or "")


async def test_commit_and_push_nothing_to_commit(monkeypatch):
    _patch_git(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (1, "nothing to commit, working tree clean", ""),
        ],
    )
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_commit_failed" in (result.error or "")
    assert "nothing to commit" in (result.error or "")


async def test_commit_and_push_rev_parse_failure(monkeypatch):
    _patch_git(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (128, "", "fatal: bad revision"),
        ],
    )
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_rev_parse_failed" in (result.error or "")


async def test_commit_and_push_push_failure(monkeypatch):
    _patch_git(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "abc\n", ""),
            (1, "", "error: failed to push some refs"),
        ],
    )
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_push_failed" in (result.error or "")


async def test_commit_and_push_surfaces_git_binary_missing(monkeypatch):
    async def raise_missing(_ws, _args, timeout=60):
        raise RuntimeError("git_binary_missing: not on PATH")

    monkeypatch.setattr(action, "_run_git_command", raise_missing)
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is False
    assert "git_binary_missing" in (result.error or "")


async def test_commit_uses_v2_specific_git_author_settings(monkeypatch):
    """commit_and_push must use settings.git_author_name_ci_fixer /
    _email_ci_fixer so commits attribute to 'Phalanx CI Fixer', not
    legacy 'FORGE' (audit item A residue)."""
    calls = _patch_git(
        monkeypatch,
        [
            (0, "", ""),              # checkout -B
            (0, "", ""),              # add
            (0, "", ""),              # commit
            (0, "shaface\n", ""),     # rev-parse HEAD
            (0, "", ""),              # push
        ],
    )
    from phalanx.config.settings import get_settings

    s = get_settings()
    # The commit call should carry -c user.name + -c user.email with
    # the CI-Fixer-specific identity.
    tool = tools_base.get("commit_and_push")
    result = await tool.handler(_ctx(), _valid_input())
    assert result.ok is True

    commit_args = calls[2]  # sequence: checkout / add / commit / rev-parse / push
    argv_joined = " ".join(commit_args)
    assert "user.name=" in argv_joined
    assert s.git_author_name_ci_fixer in argv_joined
    assert "user.email=" in argv_joined
    assert s.git_author_email_ci_fixer in argv_joined


async def test_commit_falls_back_to_legacy_author_when_v2_setting_empty(monkeypatch):
    """If an operator explicitly blanks the v2-specific settings, we
    fall back to the legacy (v1) git_author_name/email rather than
    committing with empty author info."""
    calls = _patch_git(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "sha\n", ""),
            (0, "", ""),
        ],
    )
    from phalanx.config.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "git_author_name_ci_fixer", "")
    monkeypatch.setattr(s, "git_author_email_ci_fixer", "")

    tool = tools_base.get("commit_and_push")
    await tool.handler(_ctx(), _valid_input())
    commit_argv = " ".join(calls[2])
    assert f"user.name={s.git_author_name}" in commit_argv
    assert f"user.email={s.git_author_email}" in commit_argv
