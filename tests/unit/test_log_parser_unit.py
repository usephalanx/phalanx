"""
Unit tests for phalanx/ci_fixer/log_parser.py

Tests deterministic log parsing for all supported tools:
ruff, mypy, pytest, tsc, eslint, build errors.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer.log_parser import (
    ParsedLog,
    clean_log,
    parse_log,
)


class TestCleanLog:
    def test_strips_timestamps(self):
        raw = "2026-04-12T17:36:04.123456Z phalanx/foo.py:1:1: F401 unused\n"
        cleaned = clean_log(raw)
        assert "2026-04-12" not in cleaned
        assert "F401" in cleaned

    def test_strips_ansi(self):
        raw = "\x1b[31mError\x1b[0m: something"
        cleaned = clean_log(raw)
        assert "\x1b" not in cleaned
        assert "Error" in cleaned

    def test_removes_nodejs_deprecation(self):
        raw = "Node.js 20 actions are deprecated. Please update FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"
        cleaned = clean_log(raw)
        assert "deprecated" not in cleaned

    def test_keeps_ruff_errors(self):
        raw = "phalanx/agents/foo.py:1:10: F401 'os' imported but unused"
        cleaned = clean_log(raw)
        assert "F401" in cleaned
        assert "phalanx/agents/foo.py" in cleaned


class TestParseLogRuff:
    def test_parses_f401(self):
        log = "phalanx/agents/context_resolver.py:1:1: F401 'os' imported but unused"
        parsed = parse_log(log)
        assert parsed.tool == "ruff"
        assert len(parsed.lint_errors) == 1
        err = parsed.lint_errors[0]
        assert err.file == "phalanx/agents/context_resolver.py"
        assert err.line == 1
        assert err.col == 1
        assert err.code == "F401"
        assert "imported but unused" in err.message

    def test_parses_e501(self):
        log = "phalanx/foo.py:42:101: E501 Line too long (120 > 100 characters)"
        parsed = parse_log(log)
        assert parsed.tool == "ruff"
        assert parsed.lint_errors[0].code == "E501"
        assert parsed.lint_errors[0].line == 42

    def test_parses_f404(self):
        log = "phalanx/agents/foo.py:3:1: F404 `from __future__` imports must occur at the beginning of the file"
        parsed = parse_log(log)
        assert len(parsed.lint_errors) == 1
        assert parsed.lint_errors[0].code == "F404"

    def test_multiple_errors_same_file(self):
        log = (
            "phalanx/foo.py:1:1: F401 'os' imported but unused\n"
            "phalanx/foo.py:3:1: F404 future import not at beginning\n"
        )
        parsed = parse_log(log)
        assert len(parsed.lint_errors) == 2
        assert parsed.all_files == ["phalanx/foo.py"]

    def test_has_errors_true(self):
        log = "phalanx/foo.py:1:1: F401 'os' imported but unused"
        assert parse_log(log).has_errors is True

    def test_summary_includes_codes(self):
        log = "phalanx/foo.py:1:1: F401 'os' imported but unused"
        parsed = parse_log(log)
        assert "F401" in parsed.summary()
        assert "1 lint error" in parsed.summary()

    def test_as_text_contains_file_and_code(self):
        log = "phalanx/foo.py:1:1: F401 'os' imported but unused"
        text = parse_log(log).as_text()
        assert "phalanx/foo.py" in text
        assert "F401" in text
        assert "TOOL: ruff" in text


class TestParseLogMypy:
    def test_parses_mypy_error(self):
        log = "phalanx/agents/builder.py:42: error: Incompatible return value type"
        parsed = parse_log(log)
        assert parsed.tool == "mypy"
        assert len(parsed.type_errors) == 1
        err = parsed.type_errors[0]
        assert err.file == "phalanx/agents/builder.py"
        assert err.line == 42
        assert "Incompatible" in err.message

    def test_multiple_mypy_errors(self):
        log = (
            "src/foo.py:10: error: Item has no attribute\n"
            "src/bar.py:20: error: Argument of type\n"
        )
        parsed = parse_log(log)
        assert len(parsed.type_errors) == 2
        assert len(parsed.all_files) == 2


class TestParseLogPytest:
    def test_parses_failed_test(self):
        log = "FAILED tests/unit/test_foo.py::TestBar::test_baz - AssertionError: expected 1"
        parsed = parse_log(log)
        assert parsed.tool == "pytest"
        assert len(parsed.test_failures) == 1
        f = parsed.test_failures[0]
        assert "test_foo.py" in f.file
        assert "TestBar::test_baz" in f.test_id

    def test_multiple_test_failures(self):
        log = (
            "FAILED tests/unit/test_a.py::TestA::test_one - AssertionError\n"
            "FAILED tests/unit/test_b.py::TestB::test_two - ValueError\n"
        )
        parsed = parse_log(log)
        assert len(parsed.test_failures) == 2


class TestParseLogBuild:
    def test_parses_module_not_found(self):
        log = "ModuleNotFoundError: No module named 'phalanx.missing'"
        parsed = parse_log(log)
        assert parsed.tool == "build"
        assert len(parsed.build_errors) == 1
        assert "ModuleNotFoundError" in parsed.build_errors[0].message

    def test_parses_syntax_error(self):
        log = "SyntaxError: invalid syntax (foo.py, line 5)"
        parsed = parse_log(log)
        assert parsed.tool == "build"


class TestParseLogUnknown:
    def test_empty_log_is_unknown(self):
        parsed = parse_log("")
        assert parsed.tool == "unknown"
        assert not parsed.has_errors

    def test_noise_only_is_unknown(self):
        log = "Node.js 20 actions are deprecated.\nSet up job\nComplete job"
        parsed = parse_log(log)
        assert not parsed.has_errors

    def test_has_errors_false_for_empty(self):
        assert ParsedLog(tool="unknown").has_errors is False

    def test_all_files_empty_for_no_errors(self):
        assert ParsedLog(tool="unknown").all_files == []
