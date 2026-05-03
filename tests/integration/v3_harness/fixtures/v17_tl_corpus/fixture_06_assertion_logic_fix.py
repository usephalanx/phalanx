"""Fixture 06 — assertion failure with code-only logic fix (no SRE setup).

Source pattern: typical "off-by-one" / "wrong default" bug. Realistic
shape from many small Python repos where deps are already in pyproject
and the fix is purely a code change.

This fixture exercises the "code-only fix, no env work" path without
the lint shape's quirk (lint is also no-env-work but the "fix" is
purely formatting). Here the fix is BEHAVIORAL — the function returns
a wrong value because of a logic bug.

What v1.7 TL must produce:
  - task_plan: [engineer (replace step + commit + push), sre_verify]
  - env_requirements minimal — sandbox already has pytest from dev deps
  - confidence: ≥ 0.7
  - root_cause mentions the function name and the wrong behavior
  - verify_command may equal failing_command (default DEFAULT shape)

Why this fixture matters:
  - Validates TL doesn't over-provision SRE for purely code-side bugs
    where the failing command's deps are already in pyproject.
  - Distinguishes "code-only" from "code + env" (fixture 02).
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_excludes_agent,
    plan_includes_agent,
    plan_steps_modify,
    root_cause_mentions,
)

CI_LOG = """\
2026-04-30T09:11:21.001Z + python -m pytest tests/test_pricing.py::test_apply_discount -xvs
2026-04-30T09:11:22.412Z =================== test session starts ===================
2026-04-30T09:11:22.412Z platform linux -- Python 3.11.9, pytest-8.2.2
2026-04-30T09:11:22.412Z collected 1 item
2026-04-30T09:11:22.421Z
2026-04-30T09:11:22.421Z tests/test_pricing.py::test_apply_discount FAILED
2026-04-30T09:11:22.421Z
2026-04-30T09:11:22.421Z ============= FAILURES =============
2026-04-30T09:11:22.421Z _________ test_apply_discount _________
2026-04-30T09:11:22.421Z
2026-04-30T09:11:22.421Z     def test_apply_discount():
2026-04-30T09:11:22.421Z         price = 100.0
2026-04-30T09:11:22.421Z         discount = 0.20  # 20% off
2026-04-30T09:11:22.421Z >       assert apply_discount(price, discount) == 80.0
2026-04-30T09:11:22.421Z E       assert 120.0 == 80.0
2026-04-30T09:11:22.421Z E         + where 120.0 = apply_discount(100.0, 0.20)
2026-04-30T09:11:22.421Z
2026-04-30T09:11:22.421Z tests/test_pricing.py:11: AssertionError
2026-04-30T09:11:22.421Z =========== short test summary info ===========
2026-04-30T09:11:22.421Z FAILED tests/test_pricing.py::test_apply_discount
2026-04-30T09:11:22.421Z =========== 1 failed in 0.18s ===========
2026-04-30T09:11:22.430Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/shop/pricing.py": (
        '"""Pricing helpers."""\n\n\n'
        "def apply_discount(price: float, discount_pct: float) -> float:\n"
        "    # BUG: should subtract, not add — flipped sign\n"
        "    return price * (1 + discount_pct)\n"
    ),
    "tests/test_pricing.py": (
        "from shop.pricing import apply_discount\n\n\n"
        "def test_apply_discount():\n"
        "    price = 100.0\n"
        "    discount = 0.20  # 20% off\n"
        "    assert apply_discount(price, discount) == 80.0\n"
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


FIXTURE = CorpusFixture(
    name="06_assertion_logic_fix",
    description=(
        "apply_discount returns 120.0 instead of 80.0 — sign flipped on the "
        "multiplier. Fix: change `(1 + discount_pct)` to `(1 - discount_pct)`. "
        "Code-only fix; pytest is already in dev deps so no SRE provisioning "
        "needed beyond the standard sandbox."
    ),
    source_repo="(synthesized; common 'flipped sign' bug shape)",
    source_pr_or_commit="N/A — generic logic bug",
    complexity="medium",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_pricing.py::test_apply_discount -xvs",
    failing_job_name="test",
    pr_number=23,
    invariants=[
        # Diagnosis must mention the function with the bug
        root_cause_mentions("apply_discount"),
        # v1.7 — pytest verify requires explicit sre_setup (per prompt rule)
        plan_includes_agent("cifix_sre_setup", min_count=1),
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        plan_steps_modify("src/shop/pricing.py"),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.7),
    ],
)
