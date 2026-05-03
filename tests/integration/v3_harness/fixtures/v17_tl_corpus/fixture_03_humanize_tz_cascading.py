"""Fixture 03 — humanize tz-aware datetime bug (the Phase 3 wild bug).

Source: real maintainer commit jmoiron/humanize@a47a89e
("fix: handle tz-aware datetimes in naturalday and naturaldate").

This is the EXACT shape that broke v3 in Phase 3 Path 1 (2026-05-01).
TL diagnosed it perfectly; engineer's coder loop hit MAX_SUBAGENT_TURNS.
v1.7 architecture is built specifically to fix this — this fixture is
the regression-test-from-the-future for humanize Path 1's re-attempt
which is the v1.7 DoD.

The bug: `naturaldate(value)` and `naturalday(value)` compute today's
date with `dt.date.today()` (timezone-NAIVE — uses system local TZ),
then compare against `value` which can be timezone-AWARE. For dates
that straddle midnight in the value's TZ vs system TZ, results
misclassify "today" as "tomorrow" or vice versa.

The fix: compute today using the value's tzinfo when available.

Why this fixture matters for v1.7:
  - Validates TL emits a CASCADING plan: fix naturaldate AND naturalday
    in the SAME engineer task (both use the same broken pattern).
  - Validates env_requirements includes pytest (needed to run the
    failing test).
  - Validates engineer steps use line-level instructions OR a unified
    diff — both are valid in v1.7.
  - Confirms TL grounds in the ACTUAL repo file before naming line
    numbers (caught by self-critique c4 in real run; here we check
    that the steps target the right file).
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    env_requirements_includes_python_package,
    plan_includes_agent,
    plan_steps_modify,
)


def _root_cause_mentions_timezone_and_naturaldate():
    """Accept either 'tz' or 'timezone' (TL may use either; both correct)."""
    def _check(output: dict) -> None:
        rc = (output.get("root_cause") or "").lower()
        if "tz" not in rc and "timezone" not in rc:
            raise AssertionError(
                f"root_cause must mention 'tz' or 'timezone'; got: {rc!r}"
            )
        if "naturaldate" not in rc:
            raise AssertionError(
                f"root_cause must mention 'naturaldate'; got: {rc!r}"
            )
    _check.__name__ = "root_cause_mentions(tz|timezone, naturaldate)"
    return _check

CI_LOG = """\
2026-05-01T08:14:11.001Z + python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs
2026-05-01T08:14:12.234Z =================== test session starts ===================
2026-05-01T08:14:12.234Z platform linux -- Python 3.11.9, pytest-8.2.2
2026-05-01T08:14:12.234Z collected 1 item
2026-05-01T08:14:12.245Z
2026-05-01T08:14:12.245Z tests/test_time.py::test_naturaldate_tz_aware FAILED
2026-05-01T08:14:12.245Z
2026-05-01T08:14:12.245Z ============= FAILURES =============
2026-05-01T08:14:12.245Z _________ test_naturaldate_tz_aware _________
2026-05-01T08:14:12.245Z
2026-05-01T08:14:12.245Z     def test_naturaldate_tz_aware():
2026-05-01T08:14:12.245Z         tz = ZoneInfo("Pacific/Auckland")
2026-05-01T08:14:12.245Z         value = datetime.now(tz)
2026-05-01T08:14:12.245Z >       assert humanize.naturaldate(value) == "today"
2026-05-01T08:14:12.245Z E       AssertionError: assert 'tomorrow' == 'today'
2026-05-01T08:14:12.245Z E         + where 'tomorrow' = naturaldate(<datetime in Pacific/Auckland>)
2026-05-01T08:14:12.245Z
2026-05-01T08:14:12.245Z tests/test_time.py:142: AssertionError
2026-05-01T08:14:12.245Z =========== short test summary info ===========
2026-05-01T08:14:12.245Z FAILED tests/test_time.py::test_naturaldate_tz_aware
2026-05-01T08:14:12.245Z =========== 1 failed in 0.34s ===========
2026-05-01T08:14:12.260Z Error: Process completed with exit code 1.
"""


# Realistic shape of src/humanize/time.py around the affected lines.
# Pulled from the structure of jmoiron/humanize at the parent of a47a89e.
HUMANIZE_TIME_PY = '''\
"""Time-related humanizing functions."""

from __future__ import annotations

import datetime as dt
from typing import Any


def naturalday(value: Any, format_str: str = "%b %d") -> str:
    """For date values that are tomorrow, today or yesterday compared
    to the present day return a representing string. Otherwise, return
    a string formatted according to `format_str`.
    """
    try:
        value = dt.date(value.year, value.month, value.day)
    except AttributeError:
        return str(value)
    today = dt.date.today()  # ← BUG: tz-naive
    delta = value - today
    if delta.days == 0:
        return "today"
    if delta.days == 1:
        return "tomorrow"
    if delta.days == -1:
        return "yesterday"
    return value.strftime(format_str)


def naturaldate(value: Any) -> str:
    """Like `naturalday`, but will append a year for dates that are
    a year or more away.
    """
    try:
        value = dt.date(value.year, value.month, value.day)
    except AttributeError:
        return str(value)
    today = dt.date.today()  # ← BUG: same tz-naive issue (cascade)
    delta = value - today
    if delta.days == 0:
        return "today"
    if delta.days == 1:
        return "tomorrow"
    if delta.days == -1:
        return "yesterday"
    if abs(delta.days) >= 365:
        return value.strftime("%b %d %Y")
    return value.strftime("%b %d")
'''


REPO_FILES = {
    "src/humanize/time.py": HUMANIZE_TIME_PY,
    "tests/test_time.py": (
        "import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "import humanize\n\n\n"
        "def test_naturaldate_tz_aware():\n"
        "    tz = ZoneInfo(\"Pacific/Auckland\")\n"
        "    value = datetime.datetime.now(tz)\n"
        "    assert humanize.naturaldate(value) == \"today\"\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"humanize\"\n"
        "version = \"4.10.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\", \"freezegun\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="03_humanize_tz_cascading",
    description=(
        "Real bug from jmoiron/humanize@a47a89e. naturaldate uses "
        "dt.date.today() (tz-naive) which misclassifies dates near "
        "midnight in non-system TZs. Cascading bug: naturalday has the "
        "SAME pattern and needs the SAME fix — TL must anticipate this "
        "and include it in the plan, not let the engineer discover it "
        "after fixing only naturaldate."
    ),
    source_repo="jmoiron/humanize",
    source_pr_or_commit="commit a47a89e",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs",
    failing_job_name="test",
    pr_number=512,
    invariants=[
        # Diagnosis must mention the timezone concept (TL may say "tz" OR "timezone")
        _root_cause_mentions_timezone_and_naturaldate(),
        # SRE setup needed — pytest in env, possibly Python version
        plan_includes_agent("cifix_sre_setup", min_count=1),
        # Engineer task that modifies the actual file
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_steps_modify("src/humanize/time.py"),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # SRE setup must include pytest (needed for verify)
        env_requirements_includes_python_package("pytest"),
        # Engineer must commit + push
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.7),
    ],
)
