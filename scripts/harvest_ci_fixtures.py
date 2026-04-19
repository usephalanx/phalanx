#!/usr/bin/env python3
"""CLI wrapper around `phalanx.ci_fixer_v2.simulation.harvester.harvest_from_repo`.

Example usage:

    # Requires GH_TOKEN env var with read access.
    python scripts/harvest_ci_fixtures.py \\
        --repo astral-sh/ruff \\
        --language python \\
        --failure-class lint \\
        --days 14 \\
        --limit 10

Writes redacted fixtures under tests/simulation/fixtures/<language>/<class>/.
Prints a HarvestStats summary at the end.
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
    parser.add_argument("--repo", required=True, help="'owner/repo'")
    parser.add_argument(
        "--language",
        required=True,
        choices=["python", "javascript", "typescript", "java", "csharp"],
    )
    parser.add_argument(
        "--failure-class",
        required=True,
        choices=["lint", "test_fail", "flake", "coverage"],
    )
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--corpus-root",
        default=str(_repo_root() / "tests" / "simulation" / "fixtures"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
        help="GitHub token (falls back to GH_TOKEN or GITHUB_TOKEN env var)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not args.token:
        print(
            "error: no GitHub token — set GH_TOKEN env var or pass --token",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(_repo_root()))
    from phalanx.ci_fixer_v2.simulation.harvester import harvest_from_repo

    stats = await harvest_from_repo(
        repo_full_name=args.repo,
        github_token=args.token,
        corpus_root=Path(args.corpus_root),
        language=args.language,
        failure_class=args.failure_class,
        days=args.days,
        limit=args.limit,
    )
    print(
        f"runs_inspected={stats.total_runs_inspected} "
        f"fixtures_written={stats.fixtures_written} "
        f"skipped_license={stats.skipped_incompatible_license} "
        f"skipped_no_log={stats.skipped_no_log} "
        f"skipped_no_pr={stats.skipped_no_pr} "
        f"skipped_errors={stats.skipped_errors}"
    )
    return 0


def main() -> int:
    args = _parse_args(sys.argv[1:])
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
