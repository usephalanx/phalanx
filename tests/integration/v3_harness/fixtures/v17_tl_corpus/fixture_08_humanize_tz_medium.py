"""Fixture 08 — humanize tz, MEDIUM variant.

Same bug as fixture 03 (timezone-naive `dt.date.today()` in naturaldate /
naturalday). Difference: realistic CI noise around the failure.

Medium scale:
  - CI log: ~80 lines (collection + warnings + the failure + slow tests)
  - Repo: ~10 files (humanize-style submodules + tests + conftest)

This tests whether TL stays grounded when the failure isn't the only
thing in the log. Real maintainer logs always have warnings + collection
output around the failure.

Expected: same fix shape as fixture 03 (TL must filter the noise and
land on the correct file/function pair).
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
from tests.integration.v3_harness.fixtures.v17_tl_corpus._variants_helper import (
    pytest_collection_block,
    pytest_warnings_block,
    synthetic_module_stub,
    synthetic_test_stub,
)
from tests.integration.v3_harness.fixtures.v17_tl_corpus.fixture_03_humanize_tz_cascading import (
    HUMANIZE_TIME_PY,
    _root_cause_mentions_timezone_and_naturaldate,
)


# Failure block — same as fixture 03 but embedded later in the log.
_FAILURE = """\
2026-05-01T08:14:12.244Z tests/test_time.py::test_naturaldate_tz_aware FAILED
2026-05-01T08:14:12.244Z
2026-05-01T08:14:12.244Z ============= FAILURES =============
2026-05-01T08:14:12.244Z _________ test_naturaldate_tz_aware _________
2026-05-01T08:14:12.244Z
2026-05-01T08:14:12.244Z     def test_naturaldate_tz_aware():
2026-05-01T08:14:12.244Z         tz = ZoneInfo("Pacific/Auckland")
2026-05-01T08:14:12.244Z         value = datetime.now(tz)
2026-05-01T08:14:12.245Z >       assert humanize.naturaldate(value) == "today"
2026-05-01T08:14:12.245Z E       AssertionError: assert 'tomorrow' == 'today'
2026-05-01T08:14:12.245Z E         + where 'tomorrow' = naturaldate(<datetime in Pacific/Auckland>)
2026-05-01T08:14:12.245Z
2026-05-01T08:14:12.245Z tests/test_time.py:142: AssertionError
"""


CI_LOG = "\n".join([
    pytest_collection_block(test_count=27),
    "",
    _FAILURE,
    pytest_warnings_block(),
    "",
    "2026-05-01T08:14:13.001Z =========== short test summary info ===========",
    "2026-05-01T08:14:13.001Z FAILED tests/test_time.py::test_naturaldate_tz_aware",
    "2026-05-01T08:14:13.001Z =========== 1 failed, 26 passed in 1.12s ===========",
    "2026-05-01T08:14:13.020Z Error: Process completed with exit code 1.",
])


REPO_FILES = {
    # The actual buggy file (same as fixture 03)
    "src/humanize/time.py": HUMANIZE_TIME_PY,
    # The failing test
    "tests/test_time.py": (
        "import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "import humanize\n\n\n"
        "def test_naturaldate_tz_aware():\n"
        "    tz = ZoneInfo(\"Pacific/Auckland\")\n"
        "    value = datetime.datetime.now(tz)\n"
        "    assert humanize.naturaldate(value) == \"today\"\n"
    ),
    # Sibling submodules — TL has to ignore these
    "src/humanize/__init__.py": (
        "from .time import naturalday, naturaldate  # noqa: F401\n"
        "from .number import intcomma, intword  # noqa: F401\n"
        "from .filesize import naturalsize  # noqa: F401\n"
    ),
    "src/humanize/number.py": synthetic_module_stub("number", n_funcs=4),
    "src/humanize/filesize.py": synthetic_module_stub("filesize", n_funcs=3),
    "src/humanize/i18n.py": synthetic_module_stub("i18n", n_funcs=3),
    # Sibling tests — should not be touched
    "tests/test_number.py": synthetic_test_stub("number", n_tests=4),
    "tests/test_filesize.py": synthetic_test_stub("filesize", n_tests=3),
    "tests/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def freezer():\n"
        "    pass  # placeholder\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"humanize\"\n"
        "version = \"4.10.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\", \"freezegun\", \"pytest-cov\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="08_humanize_tz_medium",
    description=(
        "Same humanize tz bug as fixture 03, with REALISTIC CI noise: "
        "pytest collection output (27 tests), 2 unrelated DeprecationWarnings, "
        "short summary line. Repo has 10 files including 3 sibling submodules "
        "(number, filesize, i18n) — TL must ignore these and land on time.py. "
        "Tests TL grounding under realistic input scale."
    ),
    source_repo="jmoiron/humanize",
    source_pr_or_commit="commit a47a89e (medium-noise variant)",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs",
    failing_job_name="test",
    pr_number=513,
    invariants=[
        # Same diagnosis invariant as fixture 03
        _root_cause_mentions_timezone_and_naturaldate(),
        # Same plan shape — sre_setup + engineer + verify
        plan_includes_agent("cifix_sre_setup", min_count=1),
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # MUST land on src/humanize/time.py — not number.py or filesize.py
        plan_steps_modify("src/humanize/time.py"),
        env_requirements_includes_python_package("pytest"),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.7),
    ],
)
