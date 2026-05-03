"""Fixture 09 — humanize tz, LARGE variant.

Same bug as fixture 03 + 08. Difference: full real-world CI verbosity.

Large scale:
  - CI log: ~250 lines (full collection output, warnings, slow tests,
    coverage summary, the failure embedded mid-log)
  - Repo: ~25 files (full humanize layout — 7 submodules + tests for
    each + conftest + docs/ + examples/ + pyproject)

This is the closest thing to "real maintainer CI output" without copying
an actual log byte-for-byte. The failure line is buried in noise; TL
must filter aggressively to find it.

Stress test of c4/c7:
  - c4 grounding — does TL still call read_file on the right files when
    the repo has 25 candidates?
  - c7 error_line_quote — can TL extract the verbatim failure line from
    a 250-line log?

Expected: same fix shape as fixture 03 (architecture invariance under scale).
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
    coverage_summary_block,
    pytest_collection_block,
    pytest_slow_tests_block,
    pytest_warnings_block,
    synthetic_module_stub,
    synthetic_test_stub,
)
from tests.integration.v3_harness.fixtures.v17_tl_corpus.fixture_03_humanize_tz_cascading import (
    HUMANIZE_TIME_PY,
    _root_cause_mentions_timezone_and_naturaldate,
)


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
    pytest_collection_block(test_count=178),
    "",
    _FAILURE,
    "",
    pytest_warnings_block(),
    "",
    pytest_slow_tests_block(),
    "",
    coverage_summary_block(),
    "",
    "2026-05-01T08:14:14.500Z =========== short test summary info ===========",
    "2026-05-01T08:14:14.500Z FAILED tests/test_time.py::test_naturaldate_tz_aware",
    "2026-05-01T08:14:14.501Z =========== 1 failed, 177 passed, 12 warnings in 4.83s ===========",
    "2026-05-01T08:14:14.520Z Error: Process completed with exit code 1.",
])


def _build_repo_files() -> dict[str, str]:
    files: dict[str, str] = {
        # The actual buggy file — same as fixture 03
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
        "src/humanize/__init__.py": (
            "from .time import naturalday, naturaldate  # noqa: F401\n"
            "from .number import intcomma, intword, ordinal  # noqa: F401\n"
            "from .filesize import naturalsize  # noqa: F401\n"
            "from .i18n import activate, deactivate  # noqa: F401\n"
            "from .lists import natural_list  # noqa: F401\n"
            "from .words import apnumber, fractional  # noqa: F401\n"
        ),
        "tests/conftest.py": (
            "import pytest\n"
            "from datetime import datetime, timezone\n\n\n"
            "@pytest.fixture\n"
            "def freezer():\n"
            "    pass\n\n\n"
            "@pytest.fixture\n"
            "def utc_now():\n"
            "    return datetime.now(timezone.utc)\n"
        ),
        "tests/__init__.py": "",
        "pyproject.toml": (
            "[project]\n"
            "name = \"humanize\"\n"
            "version = \"4.10.0\"\n"
            "description = \"Python humanize functions\"\n"
            "dependencies = []\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev = [\"pytest>=8\", \"freezegun\", \"pytest-cov\"]\n"
            "\n"
            "[tool.pytest.ini_options]\n"
            "testpaths = [\"tests\"]\n"
            "addopts = \"-q --strict-markers\"\n"
        ),
        "README.md": "# humanize\n\nPython humanize functions for things like dates, times, sizes.\n",
        "LICENSE": "MIT License — copyright humanize contributors\n",
    }

    # 6 sibling submodules with synthetic content (TL must NOT touch these)
    for mod in ("number", "filesize", "i18n", "lists", "words", "units"):
        files[f"src/humanize/{mod}.py"] = synthetic_module_stub(mod, n_funcs=5)

    # 5 sibling test files (TL must NOT touch these)
    for mod in ("number", "filesize", "i18n", "lists", "words"):
        files[f"tests/test_{mod}.py"] = synthetic_test_stub(mod, n_tests=6)

    # docs/ and examples/ — typical maintainer repo layout
    files["docs/index.md"] = "# humanize docs\n\nSee API reference for details.\n"
    files["docs/api.md"] = "## API\n\n- `naturaldate` — see source\n- `naturalsize` — see source\n"
    files["examples/basic.py"] = (
        "from humanize import naturaldate, naturalsize\n\n"
        "print(naturaldate('2026-01-01'))\n"
        "print(naturalsize(1_000_000))\n"
    )

    return files


REPO_FILES = _build_repo_files()


FIXTURE = CorpusFixture(
    name="09_humanize_tz_large",
    description=(
        "Same humanize tz bug as fixture 03, with FULL real-world CI verbosity: "
        "178-test collection, deprecation/runtime warnings, slow-test report, "
        "coverage summary table — failure line is buried mid-log. Repo has ~25 "
        "files including 6 sibling submodules + their test files + docs/ + "
        "examples/. Stress-tests TL grounding (c4/c7) under realistic scale."
    ),
    source_repo="jmoiron/humanize",
    source_pr_or_commit="commit a47a89e (large-noise variant)",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs",
    failing_job_name="test",
    pr_number=514,
    invariants=[
        _root_cause_mentions_timezone_and_naturaldate(),
        plan_includes_agent("cifix_sre_setup", min_count=1),
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # The KEY scale-invariance test: TL must STILL land on time.py,
        # not on any of the 6 sibling submodules.
        plan_steps_modify("src/humanize/time.py"),
        env_requirements_includes_python_package("pytest"),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.7),
    ],
)
