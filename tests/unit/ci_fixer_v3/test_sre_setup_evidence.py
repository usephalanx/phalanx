"""Tier-1 tests for the SRE setup evidence helper.

Locks in the constraint that LLM-supplied (file, line, package) trios
must actually point to repo content. Phase-0 of the agentic SRE design
doc (gap #2 — tool-level enforcement of "no install without evidence").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phalanx.ci_fixer_v3.sre_setup.evidence import evidence_check

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def workflow_workspace(tmp_path: Path) -> Path:
    """A repo skeleton with a realistic workflow YAML mentioning uv."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "lint.yml").write_text(
        # Lines 1-12; line 11 mentions uv via setup-uv action.
        "name: Lint\n"
        "on: [push]\n"
        "jobs:\n"
        "  mypy:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v6\n"
        "      - uses: actions/setup-python@v6\n"
        "        with:\n"
        "          python-version: '3.x'\n"
        "      - uses: astral-sh/setup-uv@v8.0.0\n"
        "      - run: uvx --with tox-uv tox -e mypy\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\ndependencies = ['ruff>=0.5']\n"
    )
    return tmp_path


def test_evidence_match_succeeds_when_candidate_appears_in_window(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 11, ["uv"])
    assert ok, reason


def test_evidence_match_succeeds_for_compound_action_name(workflow_workspace):
    ok, reason = evidence_check(
        workflow_workspace,
        ".github/workflows/lint.yml",
        11,
        ["astral-sh/setup-uv"],
    )
    assert ok, reason


def test_evidence_match_finds_within_window_above_line(workflow_workspace):
    """Cite line 12 (the `run: uvx ...`); window includes line 11 (setup-uv).
    The candidate `uv` is found at line 11, within the ±5 window."""
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 12, ["uv"])
    assert ok, reason


def test_evidence_fails_when_no_candidate_appears(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 11, ["numpy"])
    assert not ok
    assert "none of" in reason


def test_evidence_fails_for_empty_candidates_list(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 1, [])
    assert not ok
    assert "candidates list is empty" in reason


def test_evidence_rejects_path_traversal(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, "../etc/passwd", 1, ["root"])
    assert not ok
    assert "invalid evidence_file" in reason


def test_evidence_rejects_absolute_path(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, "/etc/passwd", 1, ["root"])
    assert not ok
    assert "invalid evidence_file" in reason


def test_evidence_rejects_file_outside_workspace(tmp_path: Path):
    """Even via symlink games or weird relative paths, the resolved file
    must live inside workspace_path."""
    outside = tmp_path / "outside.txt"
    outside.write_text("uv\n")
    inside = tmp_path / "repo"
    inside.mkdir()

    ok, reason = evidence_check(inside, "../outside.txt", 1, ["uv"])
    assert not ok
    assert "invalid evidence_file" in reason


def test_evidence_fails_for_missing_file(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, "no-such-file.yml", 1, ["uv"])
    assert not ok
    assert "does not exist" in reason


def test_evidence_fails_for_line_out_of_bounds(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 9999, ["uv"])
    assert not ok
    assert "out of bounds" in reason


def test_evidence_fails_for_line_zero(workflow_workspace):
    ok, reason = evidence_check(workflow_workspace, ".github/workflows/lint.yml", 0, ["uv"])
    assert not ok
    assert "line must be" in reason


def test_evidence_word_boundary_does_not_match_uvloop_for_uv(tmp_path: Path):
    """`uv` candidate must NOT match `uvloop` (different tool, common
    Python dep). This is the core word-boundary correctness test —
    trivial-string-contains would mis-fire here."""
    repo = tmp_path
    (repo / "pyproject.toml").write_text("[project]\ndependencies = ['uvloop>=0.19']\n")
    ok, reason = evidence_check(repo, "pyproject.toml", 2, ["uv"])
    assert not ok, "‘uv’ should NOT match inside ‘uvloop’"
    assert "none of" in reason


def test_evidence_word_boundary_matches_uv_in_dashed_action_name(tmp_path: Path):
    """`uv` candidate MUST match in `astral-sh/setup-uv@v8` — preceded by
    `-` which is a permissive boundary character."""
    repo = tmp_path
    (repo / "ci.yml").write_text("- uses: astral-sh/setup-uv@v8.0.0\n")
    ok, reason = evidence_check(repo, "ci.yml", 1, ["uv"])
    assert ok, reason


def test_evidence_case_insensitive_match(tmp_path: Path):
    repo = tmp_path
    (repo / "Dockerfile").write_text("RUN apt-get install -y GETTEXT\n")
    ok, reason = evidence_check(repo, "Dockerfile", 1, ["gettext"])
    assert ok, reason
