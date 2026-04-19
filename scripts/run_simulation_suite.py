#!/usr/bin/env python3
"""CLI runner for the CI Fixer v2 simulation suite (live mode).

Iterates the corpus at tests/simulation/fixtures (or a path you pass in),
runs the full v2 agent (real GPT + Sonnet + sandbox) against each
fixture, scores the outcome, and writes a scoreboard JSON + markdown to
--output-dir.

Live-mode prerequisites (see docs/ci-fixer-v2-live-run.md):
  - OPENAI_API_KEY + ANTHROPIC_API_KEY in the environment
  - GH_TOKEN for any fixture that needs live GitHub reads
  - Docker daemon reachable (sandbox_enabled=True in settings)
  - Alembic migrations applied (adds MemoryFact.agent_role +
    CIFixRun.cost_breakdown_json)
  - N1 worker split present in docker-compose.prod.yml (Docker socket
    scoped to ci_fixer worker)

For the scripted-LLM integration test (no API keys needed), run
tests/integration/ci_fixer_v2/test_e2e_seed_corpus.py instead.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--corpus-root",
        default=str(_repo_root() / "tests" / "simulation" / "fixtures"),
    )
    parser.add_argument(
        "--language",
        choices=["python", "javascript", "typescript", "java", "csharp"],
        default=None,
        help="Optional language filter",
    )
    parser.add_argument(
        "--failure-class",
        choices=["lint", "test_fail", "flake", "coverage"],
        default=None,
        help="Optional failure-class filter",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_repo_root() / "build" / "simulation"),
        help="Where to write scoreboard.{json,md}",
    )
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="Exit 1 if the MVP gate fails (for CI).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_repo_root()))

    # Import lazily so --help works without the full stack installed.
    from phalanx.ci_fixer_v2.run_bootstrap import execute_v2_run
    from phalanx.ci_fixer_v2.simulation.fixtures import Fixture
    from phalanx.ci_fixer_v2.simulation.scoreboard import render_json, render_markdown
    from phalanx.ci_fixer_v2.simulation.suite import FixtureRunResult, run_suite

    missing_env = []
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get(key):
            missing_env.append(key)
    if missing_env:
        print(
            f"error: missing env vars for live run: {missing_env}. "
            "See docs/ci-fixer-v2-live-run.md",
            file=sys.stderr,
        )
        return 2

    # Live runner: each fixture becomes a CIFixRun + workspace + sandbox
    # via the bootstrap. The bootstrap writes the outcome to DB; we
    # re-load the run row to build the FixtureRunResult.
    async def live_runner(_fixture: Fixture) -> FixtureRunResult:
        raise NotImplementedError(
            "Live-mode runner wiring lands alongside the DB-backed "
            "CIFixRun-per-fixture seeder in v2 phase 2. For now, use the "
            "scripted integration test at tests/integration/ci_fixer_v2/."
        )

    result = await run_suite(
        corpus_root=Path(args.corpus_root),
        runner=live_runner,
        language=args.language,
        failure_class=args.failure_class,
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scoreboard.json").write_text(
        render_json(result.scoreboard), encoding="utf-8"
    )
    (out_dir / "scoreboard.md").write_text(
        render_markdown(result.scoreboard), encoding="utf-8"
    )

    print(render_markdown(result.scoreboard))
    print(f"\nwrote scoreboard to {out_dir}")
    print(
        f"fixtures={result.fixture_count} "
        f"errors={result.error_count} "
        f"scores={len(result.scores)}"
    )

    if args.fail_on_gate and not result.scoreboard.mvp_gates_pass:
        print("MVP gate failed", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    args = _parse_args(sys.argv[1:])
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
