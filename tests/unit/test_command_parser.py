"""
Unit tests for forge/gateway/command_parser.py.

Pure-logic module — no I/O — so 100% reachable without mocks.
"""
from __future__ import annotations

import pytest
from forge.gateway.command_parser import (
    CommandType,
    ParsedCommand,
    parse_command,
    HELP_TEXT,
)


class TestEmptyAndHelp:
    def test_empty_string_returns_help(self):
        result = parse_command("")
        assert result.command_type == CommandType.HELP
        assert result.is_valid

    def test_whitespace_only_returns_help(self):
        result = parse_command("   ")
        assert result.command_type == CommandType.HELP

    def test_help_verb_returns_help(self):
        result = parse_command("help")
        assert result.command_type == CommandType.HELP
        assert result.is_valid

    def test_help_text_is_non_empty(self):
        assert len(HELP_TEXT) > 50
        assert "/forge" in HELP_TEXT


class TestBuildCommand:
    def test_build_with_title(self):
        result = parse_command("build Add OAuth login")
        assert result.command_type == CommandType.BUILD
        assert result.title == "Add OAuth login"
        assert result.is_valid
        assert result.parse_error is None

    def test_build_without_title_is_invalid(self):
        result = parse_command("build")
        assert result.command_type == CommandType.BUILD
        assert not result.is_valid
        assert "Usage" in result.parse_error

    def test_build_default_priority_is_p2(self):
        result = parse_command("build Fix the bug")
        assert result.priority == 50

    def test_build_priority_p0(self):
        result = parse_command("build Fix critical issue --priority P0")
        assert result.priority == 90
        assert result.is_valid

    def test_build_priority_p1(self):
        result = parse_command("build Urgent fix --priority P1")
        assert result.priority == 75

    def test_build_priority_p3(self):
        result = parse_command("build Low priority task --priority P3")
        assert result.priority == 25

    def test_build_priority_p4(self):
        result = parse_command("build Backlog item --priority P4")
        assert result.priority == 10

    def test_build_priority_equals_syntax(self):
        result = parse_command("build Add feature --priority=P2")
        assert result.priority == 50
        assert result.is_valid

    def test_build_priority_case_insensitive(self):
        result = parse_command("build Fix bug --priority p1")
        assert result.priority == 75

    def test_build_priority_stripped_from_title(self):
        result = parse_command("build Add OAuth --priority P1")
        assert result.title == "Add OAuth"
        assert "--priority" not in result.title

    def test_build_title_capped_at_200_chars(self):
        long_title = "A" * 300
        result = parse_command(f"build {long_title}")
        assert len(result.title) == 200

    def test_build_raw_text_preserved(self):
        text = "build Add OAuth login --priority P1"
        result = parse_command(text)
        assert result.raw_text == text

    def test_build_description_defaults_to_title(self):
        result = parse_command("build Implement rate limiting")
        assert result.description == "Implement rate limiting"

    def test_build_with_description_flag(self):
        result = parse_command('build Add feature --desc "Implements the new feature per RFC-42"')
        assert "Implements" in result.description

    def test_build_multiword_title(self):
        result = parse_command("build Refactor the authentication module to use JWT")
        assert "Refactor" in result.title
        assert result.is_valid


class TestStatusCommand:
    def test_status_no_run_id(self):
        result = parse_command("status")
        assert result.command_type == CommandType.STATUS
        assert result.run_id is None
        assert result.is_valid

    def test_status_with_run_id(self):
        run_id = "abc-123-def"
        result = parse_command(f"status {run_id}")
        assert result.command_type == CommandType.STATUS
        assert result.run_id == run_id
        assert result.is_valid

    def test_status_with_uuid_run_id(self):
        run_id = "12345678-1234-5678-1234-567812345678"
        result = parse_command(f"status {run_id}")
        assert result.run_id == run_id


class TestCancelCommand:
    def test_cancel_with_run_id(self):
        run_id = "abc-123"
        result = parse_command(f"cancel {run_id}")
        assert result.command_type == CommandType.CANCEL
        assert result.run_id == run_id
        assert result.is_valid

    def test_cancel_without_run_id_is_invalid(self):
        result = parse_command("cancel")
        assert result.command_type == CommandType.CANCEL
        assert not result.is_valid
        assert "Usage" in result.parse_error


class TestUnknownCommand:
    def test_unknown_verb_returns_unknown(self):
        result = parse_command("deploy production")
        assert result.command_type == CommandType.UNKNOWN
        assert not result.is_valid
        assert "Unknown command" in result.parse_error
        assert "'deploy'" in result.parse_error

    def test_unknown_verb_suggests_help(self):
        result = parse_command("foo bar")
        assert "help" in result.parse_error.lower()


class TestParsedCommandProperties:
    def test_is_valid_true_when_no_error(self):
        cmd = ParsedCommand(command_type=CommandType.HELP, raw_text="help")
        assert cmd.is_valid is True

    def test_is_valid_false_when_has_error(self):
        cmd = ParsedCommand(
            command_type=CommandType.BUILD,
            raw_text="build",
            parse_error="Usage: /forge build <title>",
        )
        assert cmd.is_valid is False

    def test_default_priority_is_50(self):
        cmd = ParsedCommand(command_type=CommandType.BUILD, raw_text="build x")
        assert cmd.priority == 50

    def test_tags_default_empty(self):
        cmd = ParsedCommand(command_type=CommandType.BUILD, raw_text="build x")
        assert cmd.tags == []
