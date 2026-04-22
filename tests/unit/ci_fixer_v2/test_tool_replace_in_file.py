"""Unit tests for the replace_in_file coder tool.

replace_in_file is the preferred edit primitive for the coder subagent:
literal find-and-replace with no line numbers, no diff syntax, no
context-match pitfalls. apply_patch remains as the fallback for
complex multi-site edits.
"""

from __future__ import annotations

from pathlib import Path

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


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A disposable workspace with a math_ops.js seed file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math_ops.js").write_text(
        "'use strict';\n"
        "\n"
        "function add(a, b) {\n"
        "  return a + b;\n"
        "}\n"
        "\n"
        "function multiply(a, b) {\n"
        "  return a * b;\n"
        "}\n"
        "\n"
        "module.exports = { add, multiply };\n"
    )
    return tmp_path


def _ctx(ws: Path) -> AgentContext:
    return AgentContext(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path=str(ws),
        original_failing_command="npm test",
    )


def _tool():
    return tools_base.get("replace_in_file")


# ─────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────


class TestInputValidation:
    async def test_missing_path(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {"old_string": "x", "new_string": "y", "target_files": ["src/math_ops.js"]},
        )
        assert result.ok is False
        assert "path" in (result.error or "")

    async def test_empty_old_string_rejected(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "",
                "new_string": "y",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is False
        assert "empty_old_string" in (result.error or "")

    async def test_missing_target_files(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "foo",
                "new_string": "bar",
            },
        )
        assert result.ok is False
        assert "target_files" in (result.error or "")

    async def test_invalid_occurrence(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "x",
                "new_string": "y",
                "target_files": ["src/math_ops.js"],
                "occurrence": "bogus",
            },
        )
        assert result.ok is False
        assert "occurrence" in (result.error or "")


# ─────────────────────────────────────────────────────────────────
# Scope + path safety
# ─────────────────────────────────────────────────────────────────


class TestScopeAndSafety:
    async def test_path_not_in_target_files_rejected(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/other.js",
                "old_string": "x",
                "new_string": "y",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is False
        assert "path_not_in_target_files" in (result.error or "")

    async def test_parent_traversal_rejected(self, ws):
        # path listed in target_files but uses ..
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "../etc/passwd",
                "old_string": "root",
                "new_string": "x",
                "target_files": ["../etc/passwd"],
            },
        )
        assert result.ok is False
        assert "unsafe_path" in (result.error or "")

    async def test_absolute_path_rejected(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "/etc/passwd",
                "old_string": "root",
                "new_string": "x",
                "target_files": ["/etc/passwd"],
            },
        )
        assert result.ok is False
        assert "unsafe_path" in (result.error or "")

    async def test_missing_file_returns_file_not_found(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/does_not_exist.js",
                "old_string": "x",
                "new_string": "y",
                "target_files": ["src/does_not_exist.js"],
            },
        )
        assert result.ok is False
        assert "file_not_found" in (result.error or "")


# ─────────────────────────────────────────────────────────────────
# Find + replace semantics
# ─────────────────────────────────────────────────────────────────


class TestFindReplace:
    async def test_not_found_surfaces_clearly(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "this string does not exist in the file",
                "new_string": "anything",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is False
        assert "not_found" in (result.error or "")

    async def test_unique_match_replaces_exactly_once(self, ws):
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "return a * b;",
                "new_string": "return a + b;  // intentionally wrong",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is True
        assert result.data["replacements"] == 1
        assert result.data["applied_to"] == ["src/math_ops.js"]
        new_text = (ws / "src" / "math_ops.js").read_text()
        assert "return a + b;  // intentionally wrong" in new_text
        # Original multiply-body line should be gone.
        assert "  return a * b;\n" not in new_text

    async def test_ambiguous_match_blocks_with_line_numbers(self, ws):
        # "return a" appears in both add and multiply bodies.
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "  return a",
                "new_string": "  return a",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is False
        assert "ambiguous" in (result.error or "")
        # Error should name concrete line numbers.
        assert "lines" in (result.error or "")

    async def test_occurrence_all_replaces_everything(self, ws):
        # Use a string appearing twice: the newline-and-function keyword.
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "function ",
                "new_string": "export function ",
                "target_files": ["src/math_ops.js"],
                "occurrence": "all",
            },
        )
        assert result.ok is True
        assert result.data["replacements"] == 2
        new_text = (ws / "src" / "math_ops.js").read_text()
        assert new_text.count("export function ") == 2
        assert "\nfunction " not in new_text  # plain form fully replaced

    async def test_delete_block_with_empty_new_string(self, ws):
        # Delete the whole multiply function block.
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "function multiply(a, b) {\n  return a * b;\n}\n\n",
                "new_string": "",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is True
        assert result.data["replacements"] == 1
        new_text = (ws / "src" / "math_ops.js").read_text()
        assert "multiply" not in new_text.split("module.exports")[0]

    async def test_append_via_last_line_anchor(self, ws):
        # Canonical append pattern: anchor on module.exports, replace
        # with new-block + module.exports.
        result = await _tool().handler(
            _ctx(ws),
            {
                "path": "src/math_ops.js",
                "old_string": "module.exports = { add, multiply };\n",
                "new_string": (
                    "function subtract(a, b) {\n  return a - b;\n}\n\n"
                    "module.exports = { add, multiply, subtract };\n"
                ),
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is True
        new_text = (ws / "src" / "math_ops.js").read_text()
        assert "function subtract" in new_text
        assert new_text.rstrip().endswith(
            "module.exports = { add, multiply, subtract };"
        )


# ─────────────────────────────────────────────────────────────────
# Ctx side effects (replay fidelity depends on these)
# ─────────────────────────────────────────────────────────────────


class TestCtxSideEffects:
    async def test_successful_edit_invalidates_sandbox_verification(self, ws):
        ctx = _ctx(ws)
        ctx.last_sandbox_verified = True  # pretend a prior run was verified
        result = await _tool().handler(
            ctx,
            {
                "path": "src/math_ops.js",
                "old_string": "return a * b;",
                "new_string": "return a + b;",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is True
        # Edits must invalidate verification — same contract as apply_patch.
        assert ctx.last_sandbox_verified is False

    async def test_failed_edit_does_not_invalidate(self, ws):
        ctx = _ctx(ws)
        ctx.last_sandbox_verified = True
        result = await _tool().handler(
            ctx,
            {
                "path": "src/math_ops.js",
                "old_string": "NOPE",
                "new_string": "anything",
                "target_files": ["src/math_ops.js"],
            },
        )
        assert result.ok is False
        # Nothing changed on disk → verification flag must survive.
        assert ctx.last_sandbox_verified is True
