"""Fixture 10 — multi-error log; tests TL's focus discipline.

Two distinct test failures in one CI run, in two different files, caused
by two genuinely separate bugs:
  1. tests/test_pricing.py::test_apply_discount     (logic bug — sign flipped)
  2. tests/test_inventory.py::test_stock_count      (off-by-one)

Both are real bugs (not flakes). TL must:
  - Acknowledge BOTH in root_cause / open_questions
  - Pick a plan that addresses AT LEAST ONE (could be both)
  - NOT hallucinate a unifying root cause they don't share

This stress-tests focus + honesty: real maintainer logs often have multiple
unrelated failures in one CI run because pytest doesn't stop at the first
failing test. The TL must not paper over reality with a single forced
diagnosis.
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_includes_agent,
    plan_steps_modify,
)


CI_LOG = """\
2026-05-02T09:11:21.001Z + python -m pytest tests/ -xvs
2026-05-02T09:11:22.412Z =================== test session starts ===================
2026-05-02T09:11:22.412Z platform linux -- Python 3.11.9, pytest-8.2.2
2026-05-02T09:11:22.420Z collected 14 items
2026-05-02T09:11:22.421Z
2026-05-02T09:11:22.421Z tests/test_pricing.py::test_apply_discount FAILED
2026-05-02T09:11:22.430Z tests/test_pricing.py::test_apply_tax PASSED
2026-05-02T09:11:22.435Z tests/test_pricing.py::test_apply_promo PASSED
2026-05-02T09:11:22.450Z tests/test_inventory.py::test_stock_count FAILED
2026-05-02T09:11:22.460Z tests/test_inventory.py::test_restock PASSED
2026-05-02T09:11:22.471Z tests/test_inventory.py::test_low_stock_alert PASSED
2026-05-02T09:11:22.475Z
2026-05-02T09:11:22.475Z ============= FAILURES =============
2026-05-02T09:11:22.475Z _________ test_apply_discount _________
2026-05-02T09:11:22.475Z
2026-05-02T09:11:22.476Z     def test_apply_discount():
2026-05-02T09:11:22.476Z         price = 100.0
2026-05-02T09:11:22.476Z         discount = 0.20
2026-05-02T09:11:22.476Z >       assert apply_discount(price, discount) == 80.0
2026-05-02T09:11:22.476Z E       assert 120.0 == 80.0
2026-05-02T09:11:22.476Z E         + where 120.0 = apply_discount(100.0, 0.20)
2026-05-02T09:11:22.476Z
2026-05-02T09:11:22.476Z tests/test_pricing.py:11: AssertionError
2026-05-02T09:11:22.476Z _________ test_stock_count _________
2026-05-02T09:11:22.477Z
2026-05-02T09:11:22.477Z     def test_stock_count():
2026-05-02T09:11:22.477Z         items = [{"qty": 5}, {"qty": 3}, {"qty": 0}]
2026-05-02T09:11:22.477Z >       assert count_in_stock(items) == 2
2026-05-02T09:11:22.477Z E       assert 3 == 2
2026-05-02T09:11:22.477Z E         + where 3 = count_in_stock([{'qty': 5}, {'qty': 3}, {'qty': 0}])
2026-05-02T09:11:22.477Z
2026-05-02T09:11:22.477Z tests/test_inventory.py:14: AssertionError
2026-05-02T09:11:22.480Z =========== short test summary info ===========
2026-05-02T09:11:22.480Z FAILED tests/test_pricing.py::test_apply_discount
2026-05-02T09:11:22.480Z FAILED tests/test_inventory.py::test_stock_count
2026-05-02T09:11:22.480Z =========== 2 failed, 12 passed in 0.91s ===========
2026-05-02T09:11:22.490Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/shop/pricing.py": (
        '"""Pricing — apply_discount has flipped sign."""\n\n\n'
        "def apply_discount(price: float, discount_pct: float) -> float:\n"
        "    return price * (1 + discount_pct)  # BUG: should subtract\n"
    ),
    "src/shop/inventory.py": (
        '"""Inventory — count_in_stock includes zero-qty items."""\n\n\n'
        "def count_in_stock(items: list[dict]) -> int:\n"
        "    return sum(1 for item in items)  # BUG: should filter qty > 0\n"
    ),
    "tests/test_pricing.py": (
        "from shop.pricing import apply_discount\n\n\n"
        "def test_apply_discount():\n"
        "    price = 100.0\n"
        "    discount = 0.20\n"
        "    assert apply_discount(price, discount) == 80.0\n"
    ),
    "tests/test_inventory.py": (
        "from shop.inventory import count_in_stock\n\n\n"
        "def test_stock_count():\n"
        '    items = [{"qty": 5}, {"qty": 3}, {"qty": 0}]\n'
        "    assert count_in_stock(items) == 2\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"shop\"\n"
        "version = \"0.5.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\"]\n"
    ),
}


def _plan_modifies_at_least_one_buggy_file():
    """Plan must touch at least ONE of {pricing.py, inventory.py}."""
    targets = {"src/shop/pricing.py", "src/shop/inventory.py"}

    def _check(output: dict) -> None:
        for ts in output.get("task_plan") or []:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                if step.get("file") in targets:
                    return
                if step.get("action") == "apply_diff":
                    diff = step.get("diff") or ""
                    if any(t in diff for t in targets):
                        return
        raise AssertionError(
            f"plan must modify at least one of {sorted(targets)}; got nothing"
        )

    _check.__name__ = "plan_modifies_at_least_one_of(pricing.py|inventory.py)"
    return _check


def _root_cause_acknowledges_two_failures():
    """root_cause OR open_questions must reference BOTH failing tests
    (or both function names). Forces TL to be honest about scope.
    """

    def _check(output: dict) -> None:
        rc = (output.get("root_cause") or "").lower()
        oq = " ".join(output.get("open_questions") or []).lower()
        fix_spec = (output.get("fix_spec") or "").lower()
        haystack = rc + " " + oq + " " + fix_spec
        # Either function names or the test names should appear
        first_signal = any(s in haystack for s in
                           {"apply_discount", "test_apply_discount", "pricing"})
        second_signal = any(s in haystack for s in
                            {"count_in_stock", "test_stock_count", "inventory"})
        if not (first_signal and second_signal):
            raise AssertionError(
                "TL must acknowledge BOTH failures in root_cause / fix_spec / "
                "open_questions; missing at least one. "
                f"root_cause={rc!r}, open_questions={oq!r}"
            )

    _check.__name__ = "root_cause_acknowledges_two_failures"
    return _check


FIXTURE = CorpusFixture(
    name="10_multi_error_focus",
    description=(
        "Two genuine bugs in one CI run: apply_discount has a flipped sign "
        "AND count_in_stock counts zero-qty items. TL must acknowledge BOTH "
        "and emit a plan that addresses AT LEAST ONE without hallucinating "
        "a shared root cause."
    ),
    source_repo="(synthesized; common 'multiple unrelated test failures' shape)",
    source_pr_or_commit="N/A — multi-failure class",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/ -xvs",
    failing_job_name="test",
    pr_number=77,
    invariants=[
        _root_cause_acknowledges_two_failures(),
        plan_includes_agent("cifix_sre_setup", min_count=1),  # pytest-based verify
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        _plan_modifies_at_least_one_buggy_file(),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        # Lower bar — partial fix is acceptable
        confidence_at_least(0.5),
    ],
)


# Compatibility: keep legacy import name for plan_steps_modify reference
_unused = plan_steps_modify
