"""
Unit tests for phalanx/ci_fixer/analyst.py — Phase 1 windowed context API.

Covers:
  - _read_windows: window creation, merging, rglob fallback
  - _parse_and_validate_patches: all guard rails
  - analyze: happy path, low-confidence paths, JSON edge cases
  - _is_test_file helper
  - FilePatch.delta property
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phalanx.ci_fixer.analyst import (
    FilePatch,
    FileWindow,
    FixPlan,
    RootCauseAnalyst,
    _is_test_file,
)
from phalanx.ci_fixer.log_parser import LintError, ParsedLog, TestFailure, TypeError


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_analyst(response: str) -> RootCauseAnalyst:
    return RootCauseAnalyst(call_llm=lambda **_: response)


def _write(tmp_path: Path, rel: str, lines: list[str]) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("".join(lines))
    return full


def _lint_log(file: str, line: int = 1, code: str = "F401") -> ParsedLog:
    return ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file=file, line=line, col=1, code=code, message="unused")],
    )


def _patch_json(path: str, start: int, end: int, corrected: list[str],
                confidence: str = "high") -> str:
    return json.dumps({
        "confidence": confidence,
        "root_cause": "test root cause",
        "patches": [{
            "path": path,
            "start_line": start,
            "end_line": end,
            "corrected_lines": corrected,
            "reason": "test",
        }],
        "needs_new_test": False,
    })


# ── FilePatch.delta ────────────────────────────────────────────────────────────


class TestFilePatchDelta:
    def test_no_change(self):
        p = FilePatch(path="f.py", start_line=1, end_line=3,
                      corrected_lines=["a\n", "b\n", "c\n"])
        assert p.delta == 0

    def test_line_removed(self):
        p = FilePatch(path="f.py", start_line=1, end_line=3,
                      corrected_lines=["a\n", "b\n"])
        assert p.delta == -1

    def test_line_added(self):
        p = FilePatch(path="f.py", start_line=1, end_line=2,
                      corrected_lines=["a\n", "b\n", "c\n"])
        assert p.delta == 1

    def test_original_window_size(self):
        p = FilePatch(path="f.py", start_line=5, end_line=10,
                      corrected_lines=["x\n"])
        assert p.original_window_size == 6


# ── _is_test_file ──────────────────────────────────────────────────────────────


class TestIsTestFile:
    def test_test_prefix(self):
        assert _is_test_file("tests/unit/test_foo.py")

    def test_test_underscore(self):
        assert _is_test_file("src/test_bar.py")

    def test_not_test_file(self):
        assert not _is_test_file("phalanx/agents/builder.py")

    def test_tests_directory(self):
        assert _is_test_file("tests/integration/test_health.py")

    def test_src_file_with_test_in_name(self):
        # "context_resolver.py" — contains no test pattern
        assert not _is_test_file("phalanx/agents/context_resolver.py")


# ── _read_windows ──────────────────────────────────────────────────────────────


class TestReadWindows:
    def test_reads_window_around_error_line(self, tmp_path):
        lines = [f"line {i}\n" for i in range(1, 101)]  # 100 lines
        _write(tmp_path, "src/foo.py", lines)
        analyst = _make_analyst("{}")
        parsed = _lint_log("src/foo.py", line=50)
        windows = analyst._read_windows(tmp_path, parsed)
        assert len(windows) == 1
        w = windows[0]
        # Window should include line 50 ± WINDOW (40)
        assert w.start_line <= 50
        assert w.end_line >= 50

    def test_window_clamped_at_file_start(self, tmp_path):
        lines = [f"line {i}\n" for i in range(1, 11)]  # 10 lines
        _write(tmp_path, "src/foo.py", lines)
        analyst = _make_analyst("{}")
        parsed = _lint_log("src/foo.py", line=1)
        windows = analyst._read_windows(tmp_path, parsed)
        assert windows[0].start_line == 1

    def test_window_clamped_at_file_end(self, tmp_path):
        lines = [f"line {i}\n" for i in range(1, 11)]  # 10 lines
        _write(tmp_path, "src/foo.py", lines)
        analyst = _make_analyst("{}")
        parsed = _lint_log("src/foo.py", line=10)
        windows = analyst._read_windows(tmp_path, parsed)
        assert windows[0].end_line == 10

    def test_missing_file_skipped(self, tmp_path):
        analyst = _make_analyst("{}")
        parsed = _lint_log("src/missing.py", line=1)
        windows = analyst._read_windows(tmp_path, parsed)
        assert windows == []

    def test_rglob_fallback_finds_file(self, tmp_path):
        # File lives at a different prefix than what the log reports
        _write(tmp_path, "packages/api/src/foo.py", ["import os\n", "x = 1\n"])
        analyst = _make_analyst("{}")
        # Log reports bare path without packages/api prefix
        parsed = _lint_log("src/foo.py", line=1)
        windows = analyst._read_windows(tmp_path, parsed)
        assert len(windows) == 1

    def test_multiple_error_lines_merged_into_one_window(self, tmp_path):
        lines = [f"line {i}\n" for i in range(1, 20)]
        _write(tmp_path, "src/foo.py", lines)
        analyst = _make_analyst("{}")
        # Two errors close together → single merged window
        parsed = ParsedLog(
            tool="ruff",
            lint_errors=[
                LintError(file="src/foo.py", line=2, col=1, code="F401", message="a"),
                LintError(file="src/foo.py", line=5, col=1, code="E501", message="b"),
            ],
        )
        windows = analyst._read_windows(tmp_path, parsed)
        assert len(windows) == 1   # merged, not two separate windows

    def test_max_files_respected(self, tmp_path):
        for i in range(6):
            _write(tmp_path, f"src/file{i}.py", [f"import os{i}\n"])
        parsed = ParsedLog(
            tool="ruff",
            lint_errors=[
                LintError(file=f"src/file{i}.py", line=1, col=1, code="F401", message="x")
                for i in range(6)
            ],
        )
        analyst = _make_analyst("{}")
        windows = analyst._read_windows(tmp_path, parsed)
        assert len(windows) <= 4   # _MAX_FILES = 4


# ── _parse_and_validate_patches ────────────────────────────────────────────────


class TestParseAndValidatePatches:
    def _window(self, path: str, start: int, end: int, n_lines: int) -> FileWindow:
        return FileWindow(
            path=path,
            start_line=start,
            end_line=end,
            original_lines=[f"line {i}\n" for i in range(n_lines)],
        )

    def _analyst(self) -> RootCauseAnalyst:
        return RootCauseAnalyst(call_llm=lambda **_: "")

    def test_valid_patch_accepted(self):
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/foo.py", "start_line": 1, "end_line": 5,
                "corrected_lines": ["a\n", "b\n", "c\n", "d\n"], "reason": "ok"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert len(patches) == 1
        assert patches[0].delta == -1

    def test_unknown_file_rejected(self):
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/bar.py", "start_line": 1, "end_line": 5,
                "corrected_lines": ["x\n"], "reason": "bad"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert patches == []

    def test_test_file_rejected(self):
        w = self._window("tests/test_foo.py", 1, 5, 5)
        raw = [{"path": "tests/test_foo.py", "start_line": 1, "end_line": 5,
                "corrected_lines": ["x\n"], "reason": "bad"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert patches == []

    def test_delta_too_large_rejected(self):
        w = self._window("src/foo.py", 1, 5, 5)
        big = [f"line {i}\n" for i in range(50)]
        raw = [{"path": "src/foo.py", "start_line": 1, "end_line": 5,
                "corrected_lines": big, "reason": "too big"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert patches == []

    def test_missing_line_range_rejected(self):
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/foo.py", "corrected_lines": ["x\n"], "reason": "missing range"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert patches == []

    def test_empty_corrected_lines_rejected(self):
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/foo.py", "start_line": 1, "end_line": 5,
                "corrected_lines": [], "reason": "empty"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert patches == []

    def test_lines_without_newline_get_newline_appended(self):
        w = self._window("src/foo.py", 1, 3, 3)
        raw = [{"path": "src/foo.py", "start_line": 1, "end_line": 3,
                "corrected_lines": ["no newline", "also no newline"], "reason": "ok"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        assert len(patches) == 1
        assert all(line.endswith("\n") for line in patches[0].corrected_lines)

    def test_line_range_within_tolerance_accepted(self):
        """start/end off by ≤2 lines → not rejected; LLM values passed through."""
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/foo.py", "start_line": 2, "end_line": 6,  # off by 1
                "corrected_lines": ["a\n", "b\n"], "reason": "off by one"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        # Accepted — within tolerance (off by 1 ≤ 2)
        assert len(patches) == 1
        # LLM values passed through unchanged when within tolerance
        assert patches[0].start_line == 2
        assert patches[0].end_line == 6

    def test_line_range_beyond_tolerance_rejected(self):
        """start/end entirely outside window → rejected (not clamped)."""
        w = self._window("src/foo.py", 1, 5, 5)
        raw = [{"path": "src/foo.py", "start_line": 10, "end_line": 20,  # entirely outside
                "corrected_lines": ["a\n", "b\n"], "reason": "way off"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        # Rejected — patch range doesn't overlap window at all
        assert len(patches) == 0

    def test_line_range_subrange_accepted(self):
        """start/end within a larger window → accepted as-is (sub-range patch)."""
        w = self._window("src/foo.py", 1, 80, 80)
        raw = [{"path": "src/foo.py", "start_line": 26, "end_line": 57,
                "corrected_lines": ["a\n"] * 30, "reason": "import cleanup"}]
        patches = self._analyst()._parse_and_validate_patches(raw, [w])
        # Accepted — valid sub-range of the window, delta = -2 (32 → 30 lines)
        assert len(patches) == 1
        assert patches[0].start_line == 26
        assert patches[0].end_line == 57


# ── analyze (full integration) ─────────────────────────────────────────────────


class TestAnalyzeIntegration:
    _FILE = ["import os\n", "import asyncio\n", "\n", "def main():\n", "    pass\n"]

    def test_high_confidence_fix_applied(self, tmp_path):
        _write(tmp_path, "src/foo.py", self._FILE)
        corrected = self._FILE[1:]   # remove "import os\n"
        response = _patch_json("src/foo.py", 1, len(self._FILE), corrected)
        analyst = _make_analyst(response)
        plan = analyst.analyze(_lint_log("src/foo.py"), tmp_path)
        assert plan.is_actionable
        assert plan.confidence == "high"
        assert plan.patches[0].delta == -1

    def test_no_files_in_workspace_returns_low(self, tmp_path):
        analyst = _make_analyst(_patch_json("src/foo.py", 1, 5, ["x\n"]))
        plan = analyst.analyze(_lint_log("src/foo.py"), tmp_path)
        assert plan.confidence == "low"
        assert not plan.is_actionable

    def test_all_patches_rejected_downgrades_confidence(self, tmp_path):
        _write(tmp_path, "src/foo.py", self._FILE)
        # LLM returns a patch for a file it was never shown
        response = _patch_json("src/invented.py", 1, 5, ["x\n"])
        analyst = _make_analyst(response)
        plan = analyst.analyze(_lint_log("src/foo.py"), tmp_path)
        assert plan.confidence == "low"

    def test_needs_new_test_propagated(self, tmp_path):
        _write(tmp_path, "src/foo.py", self._FILE)
        corrected = self._FILE[1:]
        data = json.loads(_patch_json("src/foo.py", 1, len(self._FILE), corrected))
        data["needs_new_test"] = True
        analyst = _make_analyst(json.dumps(data))
        plan = analyst.analyze(_lint_log("src/foo.py"), tmp_path)
        assert plan.needs_new_test is True

    def test_no_errors_returns_low(self, tmp_path):
        analyst = _make_analyst("{}")
        plan = analyst.analyze(ParsedLog(tool="unknown"), tmp_path)
        assert plan.confidence == "low"
