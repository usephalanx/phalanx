"""v1.7 TL output corpus harness — load fixtures + validate TL output.

Two entry points:

  discover_corpus() -> list[CorpusFixture]
    Scan this directory for `fixture_*.py` modules; return their FIXTURE
    constants. Stable order for deterministic test runs.

  validate_tl_output(fixture, output) -> ValidationReport
    Run plan_validator first (structural), then each fixture-specific
    invariant. Returns a structured report so test failures can show
    EVERY problem, not just the first one (saves prompt-eng iterations).

The harness is LLM-agnostic — pass it any output dict shape (real TL,
canned, hand-crafted). Tier-1 tests use canned outputs to verify the
invariant logic. Tier-2 (separate file) wires real TL invocation.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field

from phalanx.agents._plan_validator import validate_plan
from phalanx.agents._v17_types import PlanValidationError
from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import CorpusFixture


@dataclass
class ValidationReport:
    """Per-fixture result. Aggregates structural + semantic check outcomes."""

    fixture_name: str
    plan_validator_passed: bool
    plan_validator_error: str | None = None
    invariants_passed: list[str] = field(default_factory=list)
    invariants_failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.plan_validator_passed and not self.invariants_failed

    def render(self) -> str:
        """Multi-line pretty render — one line per check, marker on failures."""
        lines = [f"== {self.fixture_name} =="]
        if self.plan_validator_passed:
            lines.append("  ✓ plan_validator")
        else:
            lines.append(f"  ✗ plan_validator — {self.plan_validator_error}")
        for name in self.invariants_passed:
            lines.append(f"  ✓ {name}")
        for name, err in self.invariants_failed:
            lines.append(f"  ✗ {name} — {err}")
        return "\n".join(lines)


def discover_corpus() -> list[CorpusFixture]:
    """Find every `fixture_*.py` module in this package and return its
    FIXTURE constant. Stable order by module name (numeric prefix).
    """
    import tests.integration.v3_harness.fixtures.v17_tl_corpus as pkg

    fixtures: list[CorpusFixture] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.name.startswith("fixture_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        fixture = getattr(mod, "FIXTURE", None)
        if isinstance(fixture, CorpusFixture):
            fixtures.append(fixture)
    fixtures.sort(key=lambda f: f.name)
    return fixtures


def validate_tl_output(
    fixture: CorpusFixture, output: dict
) -> ValidationReport:
    """Run plan_validator + each fixture invariant; collect ALL outcomes.

    Does NOT short-circuit on first failure — surfaces every issue so
    the prompt-eng loop can fix multiple at once.
    """
    report = ValidationReport(fixture_name=fixture.name, plan_validator_passed=False)

    # Step 1: structural validation via plan_validator. If this fails,
    # invariants would mostly be moot (most assume well-formed plan), but
    # we run a SUBSET that doesn't depend on plan shape (root_cause checks).
    plan = output.get("task_plan")
    if plan is None and fixture.must_pass_plan_validator:
        report.plan_validator_error = "TL output missing task_plan"
    else:
        try:
            if fixture.must_pass_plan_validator:
                validate_plan(plan or [])
            report.plan_validator_passed = True
        except PlanValidationError as exc:
            report.plan_validator_error = str(exc)

    # Step 2: each invariant. Catch AssertionError from check fns.
    for inv in fixture.invariants:
        name = getattr(inv, "__name__", repr(inv))
        try:
            inv(output)
            report.invariants_passed.append(name)
        except AssertionError as exc:
            report.invariants_failed.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001
            report.invariants_failed.append(
                (name, f"unexpected {type(exc).__name__}: {exc}")
            )

    return report
