"""Fixture 12 — truncated CI log; tests TL's confidence calibration.

Real CI logs sometimes hit a max-size limit and get cut off mid-traceback.
The TL has partial evidence: knows SOMETHING failed but can't see the
full stack frame.

This fixture's log shows pytest collected 1 test, started running, then
the log just CUTS OFF. No exit code. No failure summary. No traceback.

What v1.7 TL must do:
  - LOWER confidence (≤ 0.6) — partial info justifies hedging
  - Mention truncation in open_questions
  - Still attempt a plan if the partial evidence narrows it down
  - NOT confidently emit a fix that papers over the missing data

This is c7's "anchor in real evidence" rule under stress. The error_line_quote
should be the LAST visible line, not a fabricated one.
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_most,
    plan_includes_agent,
)


# A pytest run that collected, started running, then got cut off mid-test.
# No exit code line. No failure summary. No traceback completion.
CI_LOG = """\
2026-05-02T11:33:01.001Z + python -m pytest tests/test_parser.py -xvs
2026-05-02T11:33:02.412Z =================== test session starts ===================
2026-05-02T11:33:02.412Z platform linux -- Python 3.11.9, pytest-8.2.2
2026-05-02T11:33:02.420Z collected 1 item
2026-05-02T11:33:02.421Z
2026-05-02T11:33:02.421Z tests/test_parser.py::test_parse_complex_input
2026-05-02T11:33:03.501Z [LOG TRUNCATED — runner-side log size limit (10 MB) reached]
2026-05-02T11:33:03.520Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/parser/__init__.py": "",
    "src/parser/core.py": (
        '"""Parser core — has a recursion bug somewhere we can\'t see in the log."""\n\n\n'
        "def parse(text: str, depth: int = 0) -> dict:\n"
        "    if depth > 1000:\n"
        "        raise RecursionError(\"too deep\")\n"
        "    # ... real parsing logic that may infinite-loop on certain inputs ...\n"
        "    return {\"text\": text, \"depth\": depth}\n"
    ),
    "tests/test_parser.py": (
        "import pytest\n"
        "from parser.core import parse\n\n\n"
        "def test_parse_complex_input():\n"
        "    result = parse(\"((nested(((deeply))))\")\n"
        "    assert result[\"depth\"] >= 4\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"parser\"\n"
        "version = \"0.1.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\"]\n"
    ),
}


def _open_questions_mentions_truncation():
    """open_questions OR root_cause should reference the log truncation
    (or the missing evidence). Confirms TL acknowledges the gap rather
    than confabulating a confident fix.
    """

    def _check(output: dict) -> None:
        oq = " ".join(output.get("open_questions") or []).lower()
        rc = (output.get("root_cause") or "").lower()
        fix_spec = (output.get("fix_spec") or "").lower()
        haystack = oq + " " + rc + " " + fix_spec
        signals = {"truncat", "incomplete", "missing", "cut off", "size limit",
                   "partial", "no traceback", "log limit", "10 mb"}
        if not any(s in haystack for s in signals):
            raise AssertionError(
                f"TL must acknowledge log truncation in root_cause / "
                f"fix_spec / open_questions; got root_cause={rc!r}, "
                f"open_questions={oq!r}"
            )

    _check.__name__ = "open_questions_mentions_truncation"
    return _check


def _error_line_quote_is_last_visible_line():
    """If error_line_quote is set, it must come from the (limited)
    visible part of the log — NOT fabricated content TL invented to
    fill the gap.
    """

    def _check(output: dict) -> None:
        quote = output.get("error_line_quote")
        if not quote:
            # Acceptable to omit if log is too partial; c7 will soft-skip.
            return
        if quote not in CI_LOG:
            raise AssertionError(
                f"error_line_quote {quote[:80]!r} not in the visible log — "
                f"TL fabricated content to fill the truncation gap"
            )

    _check.__name__ = "error_line_quote_is_last_visible_line"
    return _check


FIXTURE = CorpusFixture(
    name="12_truncated_log",
    description=(
        "CI log was truncated by the runner mid-test. TL only sees that "
        "test_parse_complex_input STARTED running before the log cuts off. "
        "No exit code, no traceback, no failure summary. TL must (a) "
        "lower confidence, (b) acknowledge the truncation in "
        "open_questions, and (c) NOT fabricate evidence to fill the gap."
    ),
    source_repo="(synthesized; common shape when CI log size limits hit)",
    source_pr_or_commit="N/A — partial-evidence class",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_parser.py -xvs",
    failing_job_name="test",
    pr_number=99,
    invariants=[
        plan_includes_agent("cifix_sre_verify", min_count=1),
        _open_questions_mentions_truncation(),
        _error_line_quote_is_last_visible_line(),
        # KEY: confidence MUST be ≤ 0.6 — partial evidence
        confidence_at_most(0.6),
    ],
)
