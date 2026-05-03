"""Tier-1 tests for the v1.7 TL output corpus harness.

These tests do NOT call any LLM. They exercise the harness logic with
canned TL outputs to verify:

  1. The harness correctly accepts a "good" output that satisfies every
     invariant for each fixture.
  2. The harness correctly rejects "bad" outputs with specific failure
     reasons (so prompt-eng iterations get clear signal).
  3. discover_corpus picks up every fixture_*.py file.

Tier-2 tests (separate file, gated by env var) will run a REAL TL agent
against the same fixtures and assert the harness reports `.ok = True`
for each. That's the prompt-eng feedback loop: red until prompt is good
enough.
"""

from __future__ import annotations

import pytest

from tests.integration.v3_harness.fixtures.v17_tl_corpus.harness import (
    discover_corpus,
    validate_tl_output,
)


# ─── Discovery ────────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_corpus_is_non_empty(self):
        corpus = discover_corpus()
        assert len(corpus) >= 3, (
            f"corpus should have ≥ 3 fixtures; got {len(corpus)}"
        )

    def test_each_fixture_has_required_fields(self):
        for fx in discover_corpus():
            assert fx.name and isinstance(fx.name, str)
            assert fx.ci_log_text, f"fixture {fx.name} has empty ci_log_text"
            assert fx.repo_files, f"fixture {fx.name} has no repo_files"
            assert fx.failing_command, f"fixture {fx.name} has no failing_command"
            assert fx.invariants, f"fixture {fx.name} has no invariants"
            assert fx.complexity in {"simple", "medium", "complex"}, (
                f"fixture {fx.name} has unknown complexity {fx.complexity!r}"
            )

    def test_each_fixture_name_matches_module(self):
        """Naming convention catches accidental copy-paste with stale name."""
        import importlib
        import pkgutil

        import tests.integration.v3_harness.fixtures.v17_tl_corpus as pkg

        for info in pkgutil.iter_modules(pkg.__path__):
            if not info.name.startswith("fixture_"):
                continue
            mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
            fixture = getattr(mod, "FIXTURE", None)
            if fixture is not None:
                # Module: "fixture_01_lint_e501_simple" → name: "01_lint_e501_simple"
                expected = info.name.removeprefix("fixture_")
                assert fixture.name == expected, (
                    f"module {info.name} declares FIXTURE.name={fixture.name!r}; "
                    f"expected {expected!r} (filename mismatch)"
                )


# ─── Canned-output assertions per fixture ─────────────────────────────────────
#
# Each fixture is paired with a hand-crafted "good" TL output that satisfies
# EVERY invariant. If a fixture's invariants change, the canned output here
# should be updated to track. This pairs the fixture and its "what-good-
# looks-like" reference in one place — easy to read both side by side.


def _good_output_01_lint_e501() -> dict:
    """Canned good output for fixture 01_lint_e501_simple."""
    return {
        "root_cause": (
            "ruff E501 — line 42 in src/widgetlib/builders.py is 118 chars "
            "(line-length limit is 100). Function signature must be wrapped."
        ),
        "fix_spec": "Wrap the function signature across multiple lines.",
        "affected_files": ["src/widgetlib/builders.py"],
        "failing_command": "ruff check src/widgetlib/builders.py",
        "verify_command": "ruff check src/widgetlib/builders.py",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.95,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": "ruff is in dev deps; verify is direct ruff invocation.",
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "wrap function signature across lines",
                "steps": [
                    {
                        "id": 1,
                        "action": "replace",
                        "file": "src/widgetlib/builders.py",
                        "old": (
                            "def build_widget_with_overlong_signature(\n"
                            "    name: str, label: str, color: str, size: int, "
                            "padding: int = 0, margin: int = 0, debug: bool = "
                            "False) -> Widget:"
                        ),
                        "new": (
                            "def build_widget_with_overlong_signature(\n"
                            "    name: str,\n"
                            "    label: str,\n"
                            "    color: str,\n"
                            "    size: int,\n"
                            "    padding: int = 0,\n"
                            "    margin: int = 0,\n"
                            "    debug: bool = False,\n"
                            ") -> Widget:"
                        ),
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": "fix(lint): wrap overlong build_widget signature",
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T3",
                "agent": "cifix_sre_verify",
                "depends_on": ["T2"],
                "purpose": "verify ruff E501 cleared",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "ruff check src/widgetlib/builders.py",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


def _good_output_02_importerror_missing_dep() -> dict:
    return {
        "root_cause": (
            "ModuleNotFoundError: tests/conftest.py imports httpx but httpx "
            "is not in pyproject.toml dependencies. CI fresh-install fails."
        ),
        "fix_spec": "Add httpx to [project].dependencies in pyproject.toml.",
        "affected_files": ["pyproject.toml"],
        "failing_command": "python -m pytest tests/ -q",
        "verify_command": "python -m pytest tests/ -q",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.85,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": "fix is config-only; sandbox needs httpx pre-installed.",
        },
        "env_requirements": {
            "python": "3.11",
            "python_packages": ["httpx", "pytest>=7"],
            "reproduce_command": "python -m pytest tests/ -q",
            "reproduce_expected": (
                "fails with ModuleNotFoundError: No module named 'httpx'"
            ),
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "purpose": "install httpx + pytest in sandbox",
                "env_requirements": {
                    "python": "3.11",
                    "python_packages": ["httpx", "pytest>=7"],
                    "reproduce_command": "python -m pytest tests/ -q",
                    "reproduce_expected": (
                        "fails with ModuleNotFoundError: No module named 'httpx'"
                    ),
                },
            },
            {
                "task_id": "T3",
                "agent": "cifix_engineer",
                "depends_on": ["T2"],
                "purpose": "add httpx to pyproject dependencies",
                "steps": [
                    {
                        "id": 1,
                        "action": "replace",
                        "file": "pyproject.toml",
                        "old": (
                            "dependencies = [\n"
                            "  \"requests>=2.28\",\n"
                            "]"
                        ),
                        "new": (
                            "dependencies = [\n"
                            "  \"requests>=2.28\",\n"
                            "  \"httpx>=0.27\",\n"
                            "]"
                        ),
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": "fix(deps): add httpx to project dependencies",
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T4",
                "agent": "cifix_sre_verify",
                "depends_on": ["T3"],
                "purpose": "verify pytest passes with httpx available",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "python -m pytest tests/ -q",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


def _good_output_03_humanize_tz_cascading() -> dict:
    return {
        "root_cause": (
            "naturaldate uses dt.date.today() (tz-naive) for the reference "
            "date, which mismatches tz-aware values near midnight in non-system "
            "timezones — returns 'tomorrow' instead of 'today'. naturalday has "
            "the same pattern (cascading bug)."
        ),
        "fix_spec": (
            "Replace dt.date.today() with a tz-aware reference date when value "
            "carries a tzinfo, in BOTH naturalday and naturaldate."
        ),
        "affected_files": ["src/humanize/time.py"],
        "failing_command": (
            "python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs"
        ),
        "verify_command": (
            "python -m pytest tests/test_time.py -xvs"
        ),  # broaden to catch cascade
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.85,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": "applying same fix to both fns avoids a follow-up red.",
        },
        "env_requirements": {
            "python": "3.11",
            "python_packages": ["pytest>=8", "freezegun"],
            "reproduce_command": (
                "python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs"
            ),
            "reproduce_expected": (
                "fails with AssertionError: 'tomorrow' == 'today'"
            ),
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "purpose": "provision Python 3.11 + pytest in sandbox",
                "env_requirements": {
                    "python": "3.11",
                    "python_packages": ["pytest>=8", "freezegun"],
                    "reproduce_command": (
                        "python -m pytest tests/test_time.py::test_naturaldate_tz_aware -xvs"
                    ),
                    "reproduce_expected": (
                        "fails with AssertionError: 'tomorrow' == 'today'"
                    ),
                },
            },
            {
                "task_id": "T3",
                "agent": "cifix_engineer",
                "depends_on": ["T2"],
                "purpose": (
                    "fix tz-aware reference date in BOTH naturalday + "
                    "naturaldate (cascade)"
                ),
                "steps": [
                    {
                        "id": 1,
                        "action": "apply_diff",
                        "diff": (
                            "--- a/src/humanize/time.py\n"
                            "+++ b/src/humanize/time.py\n"
                            "@@ -16,7 +16,7 @@ def naturalday(value, format_str=\"%b %d\"):\n"
                            "         value = dt.date(value.year, value.month, value.day)\n"
                            "     except AttributeError:\n"
                            "         return str(value)\n"
                            "-    today = dt.date.today()\n"
                            "+    today = _today_for(value)\n"
                            "     delta = value - today\n"
                            "@@ -34,7 +34,7 @@ def naturaldate(value):\n"
                            "         value = dt.date(value.year, value.month, value.day)\n"
                            "     except AttributeError:\n"
                            "         return str(value)\n"
                            "-    today = dt.date.today()\n"
                            "+    today = _today_for(value)\n"
                            "     delta = value - today\n"
                            "@@ -50,0 +50,7 @@\n"
                            "+\n"
                            "+def _today_for(value):\n"
                            "+    \"\"\"Compute today's date in value's timezone if tz-aware.\"\"\"\n"
                            "+    tzinfo = getattr(value, 'tzinfo', None)\n"
                            "+    if tzinfo is not None:\n"
                            "+        return dt.datetime.now(tzinfo).date()\n"
                            "+    return dt.date.today()\n"
                        ),
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": (
                            "fix: tz-aware reference date in naturalday/naturaldate"
                        ),
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T4",
                "agent": "cifix_sre_verify",
                "depends_on": ["T3"],
                "purpose": "verify the failing test (and others in test_time.py) pass",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "python -m pytest tests/test_time.py -xvs",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


def _good_output_04_pytest_delete_test_exit_4() -> dict:
    return {
        "root_cause": (
            "tests/test_legacy.py imports old_get from samplelib.legacy, but "
            "old_get was removed in PR #200. The test file is obsolete and "
            "should be deleted; a parallel modern test already covers the "
            "replacement API."
        ),
        "fix_spec": (
            "Delete tests/test_legacy.py entirely. Verify by running the "
            "whole suite (parent dir) so pytest exit 4 doesn't trigger."
        ),
        "affected_files": ["tests/test_legacy.py"],
        "failing_command": "python -m pytest tests/test_legacy.py::test_old_api -xvs",
        # CRITICAL: verify_command broader than failing_command — exit-4 trap
        "verify_command": "python -m pytest tests/ -q",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.9,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": (
                "verify_command broadened to tests/ to avoid pytest exit-4 "
                "after deletion."
            ),
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "delete obsolete test file",
                "steps": [
                    {
                        "id": 1,
                        "action": "apply_diff",
                        "diff": (
                            "diff --git a/tests/test_legacy.py b/tests/test_legacy.py\n"
                            "deleted file mode 100644\n"
                            "--- a/tests/test_legacy.py\n"
                            "+++ /dev/null\n"
                            "@@ -1,5 +0,0 @@\n"
                            "-from samplelib.legacy import old_get\n"
                            "-\n"
                            "-\n"
                            "-def test_old_api():\n"
                            "-    result = old_get(\"/widgets\")\n"
                        ),
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": "test: remove obsolete test_legacy.py (old_get removed in #200)",
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T3",
                "agent": "cifix_sre_verify",
                "depends_on": ["T2"],
                "purpose": "verify whole suite passes (broadened from failing_command)",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "python -m pytest tests/ -q",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


def _good_output_05_gha_workflow_escalate() -> dict:
    return {
        "root_cause": (
            "CI fails with exit 127 because the sandbox lacks `uv`. The "
            "repo's workflow uses astral-sh/setup-uv@v3 which provisions uv "
            "on GitHub Actions runners; our sandbox does not mirror this. "
            "This is an env-side gap, not a code bug — the maintainer's "
            "workflow is correct."
        ),
        "fix_spec": "No source code change. Sandbox must install uv before SRE setup.",
        "affected_files": [],  # empty — no code change
        "failing_command": "uv sync --frozen",
        "verify_command": "uv sync --frozen",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.0,
        "open_questions": [
            "sandbox lacks uv; upstream workflow uses astral-sh/setup-uv@v3",
            "fix lives in our sandbox provisioner, not the customer's repo",
        ],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,  # vacuously — list is empty
            "verify_command_will_distinguish_success": True,
            "notes": "env mismatch — escalating per architectural rule.",
        },
        # Minimal verify-only plan so plan_validator passes; commander reads
        # review_decision="ESCALATE" and skips dispatch.
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_sre_verify",
                "depends_on": [],
                "purpose": "no-op verify; plan exists for structural validity",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "echo escalated",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
        "review_decision": "ESCALATE",
    }


def _good_output_06_assertion_logic_fix() -> dict:
    return {
        "root_cause": (
            "apply_discount in src/shop/pricing.py multiplies by (1 + "
            "discount_pct) instead of (1 - discount_pct). Returns 120.0 "
            "for a 20% discount on 100.0 instead of 80.0."
        ),
        "fix_spec": "Flip the sign in apply_discount: `(1 + discount_pct)` → `(1 - discount_pct)`.",
        "affected_files": ["src/shop/pricing.py"],
        "failing_command": "python -m pytest tests/test_pricing.py::test_apply_discount -xvs",
        "verify_command": "python -m pytest tests/test_pricing.py::test_apply_discount -xvs",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.95,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": "verify_command equals failing_command — clean DEFAULT shape.",
        },
        # v1.7 prompt rule: pytest verify requires explicit sre_setup.
        "env_requirements": {
            "python": "3.11",
            "python_packages": ["pytest>=8"],
            "reproduce_command": "python -m pytest tests/test_pricing.py::test_apply_discount -xvs",
            "reproduce_expected": "fails with AssertionError: 120.0 == 80.0",
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "purpose": "ensure pytest available for verify",
                "env_requirements": {
                    "python": "3.11",
                    "python_packages": ["pytest>=8"],
                    "reproduce_command": "python -m pytest tests/test_pricing.py::test_apply_discount -xvs",
                    "reproduce_expected": "fails with AssertionError: 120.0 == 80.0",
                },
            },
            {
                "task_id": "T3",
                "agent": "cifix_engineer",
                "depends_on": ["T2"],
                "purpose": "flip discount sign",
                "steps": [
                    {
                        "id": 1,
                        "action": "replace",
                        "file": "src/shop/pricing.py",
                        "old": "    return price * (1 + discount_pct)",
                        "new": "    return price * (1 - discount_pct)",
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": "fix(pricing): apply_discount must subtract, not add",
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T4",
                "agent": "cifix_sre_verify",
                "depends_on": ["T3"],
                "purpose": "re-run the failing test",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": (
                            "python -m pytest tests/test_pricing.py::test_apply_discount -xvs"
                        ),
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


def _good_output_07_coverage_threshold_add_test() -> dict:
    new_test_content = (
        "import pytest\n"
        "from calc.statistics import mean, variance, stddev\n\n\n"
        "def test_mean_basic():\n"
        "    assert mean([1.0, 2.0, 3.0]) == 2.0\n\n\n"
        "def test_mean_empty_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        mean([])\n\n\n"
        "def test_variance_basic():\n"
        "    assert variance([1.0, 2.0, 3.0]) == pytest.approx(1.0)\n\n\n"
        "def test_variance_too_few_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        variance([1.0])\n\n\n"
        "def test_stddev_basic():\n"
        "    assert stddev([1.0, 2.0, 3.0]) == pytest.approx(1.0)\n"
    )
    return {
        "root_cause": (
            "pytest-cov reports 56% coverage (threshold 80%) because "
            "src/calc/statistics.py has zero tests. The basic module is "
            "fully covered; statistics needs tests for mean/variance/stddev."
        ),
        "fix_spec": (
            "Create tests/test_calc_statistics.py with happy + edge-case "
            "tests for mean, variance, and stddev (empty + too-few inputs)."
        ),
        "affected_files": ["tests/test_calc_statistics.py"],
        "failing_command": "python -m pytest tests/ --cov=src/calc --cov-fail-under=80",
        "verify_command": "python -m pytest tests/ --cov=src/calc --cov-fail-under=80",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.8,
        "open_questions": [],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "notes": "creating new test file; fix is structural, not behavioral.",
        },
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "add tests for statistics module to clear cov gate",
                "steps": [
                    {
                        "id": 1,
                        "action": "apply_diff",
                        "diff": (
                            "diff --git a/tests/test_calc_statistics.py b/tests/test_calc_statistics.py\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/tests/test_calc_statistics.py\n"
                            "@@ -0,0 +1,16 @@\n"
                            + "".join("+" + line for line in new_test_content.splitlines(keepends=True))
                        ),
                    },
                    {
                        "id": 2,
                        "action": "commit",
                        "message": "test: cover calc.statistics (mean/variance/stddev)",
                    },
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T3",
                "agent": "cifix_sre_verify",
                "depends_on": ["T2"],
                "purpose": "re-run pytest with cov gate",
                "steps": [
                    {
                        "id": 1,
                        "action": "run",
                        "command": "python -m pytest tests/ --cov=src/calc --cov-fail-under=80",
                        "expect_exit": 0,
                    },
                ],
            },
        ],
    }


_GOOD_OUTPUTS = {
    "01_lint_e501_simple": _good_output_01_lint_e501,
    "02_importerror_missing_dep": _good_output_02_importerror_missing_dep,
    "03_humanize_tz_cascading": _good_output_03_humanize_tz_cascading,
    "04_pytest_delete_test_exit_4": _good_output_04_pytest_delete_test_exit_4,
    "05_gha_workflow_escalate": _good_output_05_gha_workflow_escalate,
    "06_assertion_logic_fix": _good_output_06_assertion_logic_fix,
    "07_coverage_threshold_add_test": _good_output_07_coverage_threshold_add_test,
}


# ─── Happy path: each canned good output passes its fixture's invariants ─────


@pytest.mark.parametrize("fixture_name", sorted(_GOOD_OUTPUTS.keys()))
def test_canned_good_output_satisfies_invariants(fixture_name: str):
    """For each fixture, the hand-crafted good output passes every check.

    This is the LIVING DOCUMENTATION of "what TL is supposed to emit."
    If a fixture's invariants ever change, this test catches the drift.
    """
    corpus = {f.name: f for f in discover_corpus()}
    fixture = corpus[fixture_name]
    output = _GOOD_OUTPUTS[fixture_name]()
    report = validate_tl_output(fixture, output)
    assert report.ok, (
        f"canned good output for {fixture_name} should pass; report:\n"
        f"{report.render()}"
    )


# ─── Negative tests: each invariant catches its specific bad shape ────────────


class TestInvariantRejections:
    """Each test mutates a canned good output to violate ONE invariant
    and asserts the harness reports the SPECIFIC violation. This proves
    the invariant logic catches what it claims to catch.
    """

    def test_root_cause_missing_keyword_caught(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        # Mutate: drop the E501 keyword from root_cause
        output["root_cause"] = "lint failure on line 42"
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("root_cause_mentions" in n for n in failed_names), (
            f"expected root_cause_mentions failure; got {failed_names}"
        )

    def test_missing_sre_setup_caught_in_importerror(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["02_importerror_missing_dep"]
        output = _good_output_02_importerror_missing_dep()
        # Mutate: remove the sre_setup task
        output["task_plan"] = [
            t for t in output["task_plan"] if t["agent"] != "cifix_sre_setup"
        ]
        # Re-wire engineer's depends_on to not reference the removed T2
        for t in output["task_plan"]:
            t["depends_on"] = [d for d in t.get("depends_on") or [] if d != "T2"]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("plan_includes_agent(cifix_sre_setup" in n for n in failed_names), (
            f"expected sre_setup-missing failure; got {failed_names}"
        )

    def test_extra_sre_setup_caught_in_simple_lint(self):
        """Lint fixture explicitly forbids sre_setup; over-engineered TL is rejected."""
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        # Mutate: insert an unnecessary sre_setup task
        output["task_plan"].insert(
            0,
            {
                "task_id": "T0",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "purpose": "unnecessary",
                "env_requirements": {
                    "reproduce_command": "ruff check .",
                },
            },
        )
        # Re-wire engineer's depends_on
        for t in output["task_plan"]:
            if t.get("agent") == "cifix_engineer":
                t["depends_on"] = ["T0"]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("plan_excludes_agent" in n for n in failed_names), (
            f"expected plan_excludes_agent failure; got {failed_names}"
        )

    def test_missing_pyproject_modification_caught_in_importerror(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["02_importerror_missing_dep"]
        output = _good_output_02_importerror_missing_dep()
        # Mutate: remove engineer's pyproject modification step
        for t in output["task_plan"]:
            if t.get("agent") == "cifix_engineer":
                t["steps"] = [
                    s for s in t["steps"] if s.get("file") != "pyproject.toml"
                ]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("plan_steps_modify(pyproject.toml)" in n for n in failed_names)

    def test_missing_commit_action_caught(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        # Mutate: remove commit step
        for t in output["task_plan"]:
            if t.get("agent") == "cifix_engineer":
                t["steps"] = [s for s in t["steps"] if s.get("action") != "commit"]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("engineer_task_includes_action(commit)" in n for n in failed_names)

    def test_low_confidence_caught(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        # Mutate: low confidence on a clear-cut bug
        output["confidence"] = 0.4
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("confidence_at_least" in n for n in failed_names)

    def test_missing_env_requirements_caught(self):
        """Catch: TL forgot to make httpx reachable — neither in env_requirements
        nor in any pyproject.toml edit. (v1.7 invariant accepts EITHER path.)
        """
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["02_importerror_missing_dep"]
        output = _good_output_02_importerror_missing_dep()
        # Mutate: drop httpx from env_requirements AND from pyproject edits
        output["env_requirements"]["python_packages"] = []
        for t in output["task_plan"]:
            if t.get("agent") == "cifix_sre_setup":
                t["env_requirements"]["python_packages"] = []
            if t.get("agent") == "cifix_engineer":
                # Remove the pyproject step — leaves only commit + push
                t["steps"] = [s for s in t["steps"] if s.get("file") != "pyproject.toml"]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any(
            "httpx_reachable_at_verify" in n for n in failed_names
        )

    def test_narrow_verify_caught_in_delete_test(self):
        """Bug #16 reproducer: TL emits verify_command == failing_command for
        a delete-test fix. exit-4 trap. Must be caught."""
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["04_pytest_delete_test_exit_4"]
        output = _good_output_04_pytest_delete_test_exit_4()
        # Mutate: collapse verify_command to failing_command — the bug
        output["verify_command"] = output["failing_command"]
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any(
            "verify_command_targets_broader_than_failing" in n for n in failed_names
        )

    def test_workflow_modification_caught_in_escalate(self):
        """If TL tries to edit .github/workflows/, escalate fixture catches it."""
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["05_gha_workflow_escalate"]
        output = _good_output_05_gha_workflow_escalate()
        # Mutate: TL incorrectly tries to fix the workflow YAML
        output["affected_files"] = [".github/workflows/test.yml"]
        output["task_plan"] = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "edit workflow",
                "steps": [
                    {
                        "id": 1,
                        "action": "replace",
                        "file": ".github/workflows/test.yml",
                        "old": "uv sync --frozen",
                        "new": "pip install -e .",
                    },
                    {"id": 2, "action": "commit", "message": "BAD"},
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T3",
                "agent": "cifix_sre_verify",
                "depends_on": ["T2"],
                "purpose": "verify",
                "steps": [{"id": 1, "action": "run", "command": "echo bad"}],
            },
        ]
        output["confidence"] = 0.9
        output["review_decision"] = None
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("plan_does_not_modify_path_prefix(.github/)" in n for n in failed_names)
        assert any("affected_files_is_empty" in n for n in failed_names)
        assert any("confidence_at_most" in n for n in failed_names)
        assert any("review_decision_equals(ESCALATE)" in n for n in failed_names)

    def test_missing_review_decision_in_escalate_caught(self):
        """If TL forgets to set review_decision=ESCALATE on env-mismatch
        fixture, the test catches it (commander needs the explicit signal)."""
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["05_gha_workflow_escalate"]
        output = _good_output_05_gha_workflow_escalate()
        output["review_decision"] = None  # forgot to escalate
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        assert any("review_decision_equals(ESCALATE)" in n for n in failed_names)

    def test_missing_new_test_file_caught_in_coverage(self):
        """If TL tries to fix coverage by modifying source instead of
        adding tests, the coverage fixture catches it."""
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["07_coverage_threshold_add_test"]
        output = _good_output_07_coverage_threshold_add_test()
        # Mutate: replace the apply_diff with an irrelevant edit on src
        for t in output["task_plan"]:
            if t.get("agent") == "cifix_engineer":
                t["steps"][0] = {
                    "id": 1,
                    "action": "replace",
                    "file": "src/calc/statistics.py",
                    "old": "def mean(values: list[float]) -> float:",
                    "new": "def mean(values: list[float]) -> float:  # noqa",
                }
        report = validate_tl_output(fixture, output)
        assert not report.ok
        failed_names = [name for name, _ in report.invariants_failed]
        # v1.7 — invariant relaxed from plan_creates_file to accept extending
        # an existing tests/ file. The mutation above only touches src/, so
        # the new "any test additions for statistics helpers" check fires.
        assert any(
            "engineer_adds_tests_for_statistics_helpers" in n
            for n in failed_names
        )


# ─── Plan validator integration ───────────────────────────────────────────────


class TestStructuralValidation:
    def test_malformed_plan_caught_by_validator(self):
        """If task_plan has a cycle, plan_validator fails BEFORE invariants run.

        Confirms harness reports the structural error clearly.
        """
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        # Mutate: introduce a cycle T2 ↔ T3
        output["task_plan"][0]["depends_on"] = ["T3"]
        output["task_plan"][1]["depends_on"] = ["T2"]
        report = validate_tl_output(fixture, output)
        assert not report.plan_validator_passed
        assert "cycle" in (report.plan_validator_error or "")

    def test_missing_task_plan_caught(self):
        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus["01_lint_e501_simple"]
        output = _good_output_01_lint_e501()
        del output["task_plan"]
        report = validate_tl_output(fixture, output)
        assert not report.plan_validator_passed
        assert "missing task_plan" in (report.plan_validator_error or "")
