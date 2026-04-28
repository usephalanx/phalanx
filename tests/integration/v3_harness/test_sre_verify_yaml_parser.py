"""SRE verify-mode workflow YAML parser tests.

Locks down the bug we hit during the 2026-04-25 v3 lint canary on the
Python testbed (PR #13, Run 8e065ff4): SRE's _collect_verify_commands
parsed `run: |` blocks line-by-line WITHOUT joining shell line
continuations. The first non-empty line of:

    run: |
      pytest \\
        --cov=src/calc \\
        --cov-fail-under=80

…was `pytest \\`, which the sandbox executed literally. exit_code=4
("file or directory not found: \\"). SRE then reported new_failures,
TL re-investigated, decided "the YAML is broken", engineer rewrote
`.github/workflows/ci.yml` as a single line — an unauthorized CI-infra
patch. v3 marked SHIPPED with a 2nd commit v2 never made.

Root cause: parser bug. These tests catch it locally pre-deploy.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from phalanx.agents.cifix_sre import _collect_verify_commands_for_test

if TYPE_CHECKING:
    from pathlib import Path


def _write_workflow(workspace: Path, content: str) -> None:
    """Write workspace/.github/workflows/ci.yml with the given content."""
    wf_dir = workspace / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "ci.yml").write_text(content, encoding="utf-8")


def test_multiline_run_block_with_shell_continuations_joins_to_one_command(tmp_path):
    """The exact failure shape from the canary. Before the fix, this
    test would assert "pytest \\" was emitted. After the fix, the
    continuations join and the parser sees the full pytest invocation.
    """
    # NOTE: this is the literal YAML shape on usephalanx/phalanx-ci-fixer-testbed
    # at the time of the 2026-04-25 lint canary. Reproducing it verbatim.
    _write_workflow(
        tmp_path,
        dedent(
            """\
            name: CI
            on: [pull_request]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - name: Pytest with coverage (fail-under 80)
                    run: |
                      pytest \\
                        --cov=src/calc \\
                        --cov-report=term-missing \\
                        --cov-report=xml \\
                        --cov-fail-under=80 \\
                        --timeout=2
            """
        ),
    )

    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="")

    # The bug emitted "pytest \\" as a standalone command. After the fix,
    # the continuations are collapsed and we see the full pytest invocation.
    just_commands = [c for _, c in cmds]
    assert "pytest \\" not in just_commands, (
        f"Bug #9 regression: parser emitted bare 'pytest \\\\' as a command. Got: {just_commands}"
    )

    # And the joined command must be present, with all flags preserved.
    matching = [c for c in just_commands if c.startswith("pytest ")]
    assert matching, f"No joined pytest command emitted. Got: {just_commands}"
    joined = matching[0]
    for flag in (
        "--cov=src/calc",
        "--cov-report=term-missing",
        "--cov-fail-under=80",
        "--timeout=2",
    ):
        assert flag in joined, f"Flag {flag!r} dropped during join. Got: {joined!r}"


def test_single_line_run_command_unchanged(tmp_path):
    """Sanity: single-line run commands (the common case) still work
    exactly as before — no regression for the v2 happy path.
    """
    _write_workflow(
        tmp_path,
        dedent(
            """\
            name: CI
            on: [pull_request]
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: ruff check .
            """
        ),
    )

    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="")
    just_commands = [c for _, c in cmds]
    assert just_commands == ["ruff check ."], just_commands


def test_multiline_run_with_setup_lines_picks_test_invocation(tmp_path):
    """Real-world: a `run: |` block often has setup lines BEFORE the
    interesting command. The parser already takes the first non-empty
    line, but the bug fix mustn't break that — verify the first non-empty
    JOINED line is what gets picked.
    """
    _write_workflow(
        tmp_path,
        dedent(
            """\
            name: CI
            on: [pull_request]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - name: Run tests
                    run: |
                      pytest -xvs \\
                        --tb=short
            """
        ),
    )

    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="")
    just_commands = [c for _, c in cmds]
    matching = [c for c in just_commands if c.startswith("pytest")]
    assert matching, just_commands
    assert "--tb=short" in matching[0], matching[0]
    assert "\\" not in matching[0], f"Backslash leaked into command: {matching[0]!r}"


def test_continuation_with_indentation_variations(tmp_path):
    """Continuations may be followed by spaces, tabs, or both. The fix
    uses [ \\t]* which handles all three.
    """
    # Mix tabs (\t) and spaces in the continuation indentation.
    yaml_text = (
        "name: CI\n"
        "on: [pull_request]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          pytest \\\n"
        "          \t  --cov=src \\\n"
        "            --cov-fail-under=80\n"
    )
    _write_workflow(tmp_path, yaml_text)

    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="")
    just_commands = [c for _, c in cmds]
    matching = [c for c in just_commands if c.startswith("pytest")]
    assert matching, just_commands
    assert "--cov=src" in matching[0]
    assert "--cov-fail-under=80" in matching[0]
    assert "\\" not in matching[0]


def test_no_workflow_dir_returns_only_original_command(tmp_path):
    """If there's no .github/workflows/ at all, the parser must still
    return the original failing command (sanity / no regression).
    """
    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="ruff check .")
    assert cmds == [("original_failing_command", "ruff check .")], cmds


def test_uninteresting_commands_filtered_out(tmp_path):
    """The parser only emits commands matching _INTERESTING_COMMAND_PREFIXES.
    A `run: ls -la` step should be skipped, not run during verify.
    """
    _write_workflow(
        tmp_path,
        dedent(
            """\
            name: CI
            on: [pull_request]
            jobs:
              setup:
                runs-on: ubuntu-latest
                steps:
                  - run: ls -la
                  - run: echo hello
                  - run: ruff check .
            """
        ),
    )
    cmds = _collect_verify_commands_for_test(tmp_path, original_failing_command="")
    just_commands = [c for _, c in cmds]
    assert just_commands == ["ruff check ."], just_commands
