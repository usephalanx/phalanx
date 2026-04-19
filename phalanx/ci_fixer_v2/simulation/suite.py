"""Simulation suite orchestrator.

Puts the simulation pieces together: iterate a corpus, run each fixture
through a caller-supplied `FixtureRunner`, score, and aggregate into a
`Scoreboard`.

The runner abstraction keeps this module provider-neutral:
  - Integration tests use a scripted-LLM runner (no API keys).
  - The live CLI (`scripts/run_simulation_suite.py`) uses a runner that
    calls into `run_bootstrap.execute_v2_run` with real LLMs + sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import structlog

from phalanx.ci_fixer_v2.agent import RunOutcome
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.simulation.fixtures import Fixture, iter_fixtures
from phalanx.ci_fixer_v2.simulation.scoreboard import Scoreboard, aggregate
from phalanx.ci_fixer_v2.simulation.scoring import FixtureScore, score_fixture

log = structlog.get_logger(__name__)


@dataclass
class FixtureRunResult:
    """Everything a FixtureRunner must return to the suite orchestrator."""

    fixture: Fixture
    outcome: RunOutcome
    ctx: AgentContext


FixtureRunner = Callable[[Fixture], Awaitable[FixtureRunResult]]


@dataclass
class SuiteResult:
    """Aggregate output of one suite run."""

    scoreboard: Scoreboard
    scores: list[FixtureScore]
    fixture_count: int
    error_count: int = 0


async def run_suite(
    corpus_root: Path,
    runner: FixtureRunner,
    *,
    language: str | None = None,
    failure_class: str | None = None,
) -> SuiteResult:
    """Iterate the corpus and run each fixture through `runner`.

    Failures in a single fixture are captured (not raised) so one bad
    fixture does not abort the whole scoring run. The `error_count` on
    the result surfaces how many fixtures could not be scored.
    """
    scores: list[FixtureScore] = []
    error_count = 0
    fixture_count = 0

    for fixture in iter_fixtures(
        corpus_root, language=language, failure_class=failure_class
    ):
        fixture_count += 1
        try:
            result = await runner(fixture)
        except Exception as exc:  # defensive — never let one bad fixture fail the suite
            error_count += 1
            log.warning(
                "v2.simulation.runner_error",
                fixture_id=fixture.fixture_id,
                error=str(exc),
            )
            continue
        try:
            score = score_fixture(result.fixture, result.outcome, result.ctx)
            scores.append(score)
        except Exception as exc:
            error_count += 1
            log.warning(
                "v2.simulation.score_error",
                fixture_id=fixture.fixture_id,
                error=str(exc),
            )

    scoreboard = aggregate(scores)
    return SuiteResult(
        scoreboard=scoreboard,
        scores=scores,
        fixture_count=fixture_count,
        error_count=error_count,
    )
