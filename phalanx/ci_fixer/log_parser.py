"""
CI Log Parser — deterministic structured extraction from raw CI logs.

No LLM involved. Parses the raw log text into structured error objects
that the analyst and planner can reason about precisely.

Supported tools:
  - ruff   (lint)
  - mypy   (type)
  - pytest (test)
  - tsc    (type)
  - eslint (lint)
  - generic build errors

Output: ParsedLog — a structured representation of all failures found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Structured error types ─────────────────────────────────────────────────────


@dataclass
class LintError:
    """A single lint violation (ruff, eslint, pylint)."""

    file: str
    line: int
    col: int
    code: str  # e.g. F401, E501
    message: str


@dataclass
class TypeError:
    """A type checking error (mypy, tsc)."""

    file: str
    line: int
    col: int
    message: str


@dataclass
class TestFailure:
    """A failing test case (pytest, jest)."""

    test_id: str  # e.g. tests/unit/test_foo.py::TestBar::test_baz
    file: str
    message: str  # assertion / exception text


@dataclass
class BuildError:
    """A build/import/syntax error."""

    file: str | None
    message: str


@dataclass
class ParsedLog:
    """
    Structured result of parsing a raw CI log.

    All fields are deterministically extracted — no LLM guessing.
    """

    tool: str  # ruff | mypy | pytest | tsc | eslint | build | unknown
    lint_errors: list[LintError] = field(default_factory=list)
    type_errors: list[TypeError] = field(default_factory=list)
    test_failures: list[TestFailure] = field(default_factory=list)
    build_errors: list[BuildError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.lint_errors or self.type_errors or self.test_failures or self.build_errors)

    @property
    def all_files(self) -> list[str]:
        """All unique files mentioned across all error types."""
        seen: set[str] = set()
        files: list[str] = []
        for le in self.lint_errors:
            if le.file not in seen:
                seen.add(le.file)
                files.append(le.file)
        for te in self.type_errors:
            if te.file not in seen:
                seen.add(te.file)
                files.append(te.file)
        for tf in self.test_failures:
            if tf.file not in seen:
                seen.add(tf.file)
                files.append(tf.file)
        for be in self.build_errors:
            if be.file and be.file not in seen:
                seen.add(be.file)
                files.append(be.file)
        return files

    def summary(self) -> str:
        """One-line human-readable summary of all errors."""
        parts = []
        if self.lint_errors:
            codes = ", ".join(sorted({e.code for e in self.lint_errors[:5]}))
            parts.append(f"{len(self.lint_errors)} lint error(s): {codes}")
        if self.type_errors:
            parts.append(f"{len(self.type_errors)} type error(s)")
        if self.test_failures:
            parts.append(f"{len(self.test_failures)} test failure(s)")
        if self.build_errors:
            parts.append(f"{len(self.build_errors)} build error(s)")
        return "; ".join(parts) if parts else "no structured errors found"

    def as_text(self) -> str:
        """Formatted text representation for LLM prompts."""
        lines: list[str] = [f"TOOL: {self.tool}", f"SUMMARY: {self.summary()}", ""]

        if self.lint_errors:
            lines.append("LINT ERRORS:")
            for e in self.lint_errors[:20]:
                lines.append(f"  {e.file}:{e.line}:{e.col}: {e.code} {e.message}")
            lines.append("")

        if self.type_errors:
            lines.append("TYPE ERRORS:")
            for te in self.type_errors[:10]:
                lines.append(f"  {te.file}:{te.line}: {te.message}")
            lines.append("")

        if self.test_failures:
            lines.append("TEST FAILURES:")
            for tf in self.test_failures[:10]:
                lines.append(f"  {tf.test_id}")
                if tf.message:
                    for msg_line in tf.message.splitlines()[:5]:
                        lines.append(f"    {msg_line}")
            lines.append("")

        if self.build_errors:
            lines.append("BUILD ERRORS:")
            for be in self.build_errors[:5]:
                prefix = f"  {be.file}: " if be.file else "  "
                lines.append(f"{prefix}{be.message}")
            lines.append("")

        return "\n".join(lines)


# ── Regex patterns ─────────────────────────────────────────────────────────────

# ruff standard format: phalanx/agents/foo.py:1:10: F401 'os' imported but unused
_RUFF_RE = re.compile(
    r"^([\w./\-]+\.py):(\d+):(\d+):\s+([A-Z]\d+)\s+(.+)$",
    re.MULTILINE,
)

# ruff rich/diagnostic format (--output-format=full or terminal default):
#   F401 [*] `sys` imported but unused
#      --> tests/test_eval_outcome.py:259:8
_RUFF_RICH_RE = re.compile(
    r"^([A-Z]\d+)\s+(?:\[\*\]\s+)?(.+?)\n\s+-->\s+([\w./\-]+\.py):(\d+):(\d+)",
    re.MULTILINE,
)

# mypy output format: phalanx/agents/foo.py:42: error: Incompatible return value
_MYPY_RE = re.compile(
    r"^([\w./\-]+\.py):(\d+):\s+error:\s+(.+)$",
    re.MULTILINE,
)

# pytest: FAILED tests/unit/test_foo.py::TestBar::test_baz - AssertionError
_PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+([\w./\-]+\.py::[\w:]+)\s*(?:-\s*(.+))?$",
    re.MULTILINE,
)

# pytest assertion block: lines after "FAILED" up to next "FAILED" or "======"
_PYTEST_ASSERT_RE = re.compile(
    r"AssertionError:\s*(.+?)(?=\nFAILED|\n=====|\Z)",
    re.DOTALL,
)

# tsc: src/foo.ts(42,5): error TS2345: Argument of type ...
_TSC_RE = re.compile(
    r"^([\w./\-]+\.[jt]sx?)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.+)$",
    re.MULTILINE,
)

# eslint: /path/to/file.js  42:5  error  no-unused-vars
_ESLINT_RE = re.compile(
    r"^\s+([\w./\-]+\.[jt]sx?)\s+(\d+):(\d+)\s+error\s+(.+)$",
    re.MULTILINE,
)

# build errors: SyntaxError, ImportError, ModuleNotFoundError
_BUILD_RE = re.compile(
    r"(SyntaxError|IndentationError|ModuleNotFoundError|ImportError|Failed to compile"
    r"|Build failed|Cannot find module).*",
    re.IGNORECASE,
)

# GitHub Actions timestamp prefix — strip before parsing
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*", re.MULTILINE)

# ANSI escape codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Known noise lines to remove before parsing
_NOISE_RE = re.compile(
    r"(Node\.js \d+ actions are deprecated"
    r"|FORCE_JAVASCRIPT_ACTIONS_TO_NODE"
    r"|##\[group\]|##\[endgroup\]|##\[debug\]"
    r"|^Ran \d+ test"
    r"|^platform |^rootdir:|^plugins:|^collecting\.\.\."
    r"|^={3,})",
    re.IGNORECASE | re.MULTILINE,
)


# ── Public API ─────────────────────────────────────────────────────────────────


_GH_ANNOTATION_RE = re.compile(r"^##\[(?:error|warning|notice)\]", re.MULTILINE)


def clean_log(raw: str) -> str:
    """Strip timestamps, ANSI codes and noise from a raw CI log."""
    text = _TIMESTAMP_RE.sub("", raw)
    text = _ANSI_RE.sub("", text)
    # Strip GitHub Actions annotation prefixes so tool patterns match normally
    # e.g. "##[error]path/file.py:1:2: F401 ..." → "path/file.py:1:2: F401 ..."
    text = _GH_ANNOTATION_RE.sub("", text)
    # Remove lines matching noise patterns
    lines = [line for line in text.splitlines() if not _NOISE_RE.search(line)]
    return "\n".join(lines)


def parse_log(raw: str) -> ParsedLog:
    """
    Parse a raw CI log into a structured ParsedLog.

    Tries each tool parser in order; a log may have multiple tool outputs
    (e.g. ruff + mypy in the same gate). All errors are collected.
    """
    text = clean_log(raw)

    lint_errors = _parse_ruff(text) + _parse_eslint(text)
    type_errors = _parse_mypy(text) + _parse_tsc(text)
    test_failures = _parse_pytest(text)
    build_errors = _parse_build(text)

    # Determine primary tool
    if lint_errors:
        tool = "ruff" if (_RUFF_RE.search(text) or _RUFF_RICH_RE.search(text)) else "eslint"
    elif type_errors:
        tool = "mypy" if _MYPY_RE.search(text) else "tsc"
    elif test_failures:
        tool = "pytest"
    elif build_errors:
        tool = "build"
    else:
        tool = "unknown"

    return ParsedLog(
        tool=tool,
        lint_errors=lint_errors,
        type_errors=type_errors,
        test_failures=test_failures,
        build_errors=build_errors,
    )


# ── Tool-specific parsers ──────────────────────────────────────────────────────


def _parse_ruff(text: str) -> list[LintError]:
    errors: list[LintError] = []
    seen: set[tuple] = set()

    for m in _RUFF_RE.finditer(text):
        key = (m.group(1), int(m.group(2)), m.group(4))
        if key not in seen:
            seen.add(key)
            errors.append(LintError(
                file=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                code=m.group(4),
                message=m.group(5).strip(),
            ))

    # Also parse rich/diagnostic format (--output-format=full or terminal default):
    #   F401 [*] `sys` imported but unused
    #      --> tests/test_eval_outcome.py:259:8
    for m in _RUFF_RICH_RE.finditer(text):
        key = (m.group(3), int(m.group(4)), m.group(1))
        if key not in seen:
            seen.add(key)
            errors.append(LintError(
                file=m.group(3),
                line=int(m.group(4)),
                col=int(m.group(5)),
                code=m.group(1),
                message=m.group(2).strip(),
            ))

    return errors


def _parse_mypy(text: str) -> list[TypeError]:
    errors: list[TypeError] = []
    for m in _MYPY_RE.finditer(text):
        errors.append(
            TypeError(
                file=m.group(1),
                line=int(m.group(2)),
                col=0,
                message=m.group(3).strip(),
            )
        )
    return errors


def _parse_pytest(text: str) -> list[TestFailure]:
    failures: list[TestFailure] = []
    for m in _PYTEST_FAILED_RE.finditer(text):
        test_id = m.group(1)
        # Extract file path from test_id (everything before ::)
        file_part = test_id.split("::")[0]
        msg = m.group(2) or ""
        failures.append(TestFailure(test_id=test_id, file=file_part, message=msg.strip()))
    return failures


def _parse_tsc(text: str) -> list[TypeError]:
    errors: list[TypeError] = []
    for m in _TSC_RE.finditer(text):
        errors.append(
            TypeError(
                file=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                message=f"{m.group(4)}: {m.group(5).strip()}",
            )
        )
    return errors


def _parse_eslint(text: str) -> list[LintError]:
    errors: list[LintError] = []
    for m in _ESLINT_RE.finditer(text):
        errors.append(
            LintError(
                file=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                code="eslint",
                message=m.group(4).strip(),
            )
        )
    return errors


def _parse_build(text: str) -> list[BuildError]:
    errors: list[BuildError] = []
    seen: set[str] = set()
    for m in _BUILD_RE.finditer(text):
        msg = m.group(0).strip()[:200]
        if msg not in seen:
            seen.add(msg)
            errors.append(BuildError(file=None, message=msg))
    return errors
