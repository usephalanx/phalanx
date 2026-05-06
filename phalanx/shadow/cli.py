"""`phalanx shadow ...` CLI for v1.7.3-ledger MVP.

Usage:
  python -m phalanx.shadow run --repo OWNER/NAME --workflow-run-id N
  python -m phalanx.shadow show <ledger_id>
  python -m phalanx.shadow export out.json [--repo OWNER/NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from phalanx.db.session import get_db
from phalanx.shadow import ledger as ledger_crud
from phalanx.shadow.runner import (
    ShadowRunnerError,
    run_shadow_for_workflow,
)


# ── Subcommand handlers ──────────────────────────────────────────────────


def _print_banner(result: dict) -> None:
    """Surface ledger_id + run_id + verdict at the top of the output so the
    operator sees them without scanning the JSON body. Stable shape — the
    runner CLI is the public entry point for the MVP."""
    verdict = result.get("phalanx_verdict") or "?"
    icon = {
        "SHIPPED_PROPOSED": "✅",
        "SAFE_ESCALATE": "⚠️ ",
        "FAILED": "❌",
        "PENDING": "⏳",
    }.get(verdict, "•")
    print("=" * 60)
    print(f"{icon} Shadow run complete — verdict: {verdict}")
    print("-" * 60)
    print(f"  ledger_id : {result.get('id')}")
    print(f"  attempt   : #{result.get('attempt_number')}")
    print(f"  run_id    : {result.get('phalanx_run_id')}")
    print(f"  repo      : {result.get('repo')}")
    print(f"  workflow  : {result.get('workflow_run_id')}")
    if result.get("pr_number") is not None:
        print(f"  pr_number : #{result.get('pr_number')}")
    if result.get("phalanx_confidence") is not None:
        print(f"  confidence: {result.get('phalanx_confidence')}")
    if result.get("phalanx_run_seconds") is not None:
        print(f"  run_secs  : {result.get('phalanx_run_seconds')}")
    if result.get("phalanx_cost_usd") is not None:
        print(f"  cost_usd  : ${result.get('phalanx_cost_usd')}")
    print("=" * 60)


async def _cmd_run(args: argparse.Namespace) -> int:
    try:
        result = await run_shadow_for_workflow(
            repo=args.repo,
            workflow_run_id=args.workflow_run_id,
            poll_interval_s=args.poll_interval,
            poll_timeout_s=args.poll_timeout,
        )
    except ShadowRunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    _print_banner(result)
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _cmd_show(args: argparse.Namespace) -> int:
    async with get_db() as session:
        row = await ledger_crud.get(session, args.ledger_id)
    if row is None:
        print(f"ERROR: ledger row {args.ledger_id!r} not found", file=sys.stderr)
        return 1
    print(json.dumps(ledger_crud.to_dict(row), indent=2, default=str))
    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    async with get_db() as session:
        rows = await ledger_crud.list_all(session, repo=args.repo, limit=args.limit)
    payload = [ledger_crud.to_dict(r) for r in rows]
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"wrote {len(payload)} ledger rows to {args.out}")
    return 0


def _verdict_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        v = r.phalanx_verdict or "?"
        counts[v] = counts.get(v, 0) + 1
    return counts


async def _cmd_metrics(args: argparse.Namespace) -> int:
    """Print ledger metrics. Default: count every attempt as separate.
    --by-workflow: dedup to the LATEST attempt per (repo, workflow_run_id),
    so retries don't inflate the verdict counts."""
    async with get_db() as session:
        all_rows = await ledger_crud.list_all(
            session, repo=args.repo, limit=10_000
        )
        latest_rows = await ledger_crud.latest_per_workflow(
            session, repo=args.repo
        )

    n_attempts = len(all_rows)
    n_workflows = len(latest_rows)
    n_repos = len({r.repo for r in latest_rows})

    by_attempt = _verdict_counts(all_rows)
    by_workflow = _verdict_counts(latest_rows)

    cost_total = sum(float(r.phalanx_cost_usd or 0.0) for r in all_rows)
    time_total = sum(int(r.phalanx_run_seconds or 0) for r in all_rows)

    print("=" * 60)
    print(
        f"Shadow Ledger Metrics — {n_attempts} attempts across "
        f"{n_workflows} unique workflow_run_ids ({n_repos} repos)"
    )
    if args.repo:
        print(f"  filter: repo={args.repo}")
    print("-" * 60)
    print("By verdict (every attempt counted):")
    for v, n in sorted(by_attempt.items(), key=lambda kv: -kv[1]):
        pct = (n / n_attempts * 100) if n_attempts else 0.0
        print(f"  {v:18} {n:4} ({pct:.0f}%)")

    if args.by_workflow:
        print()
        print("By verdict (latest attempt per workflow):")
        for v, n in sorted(by_workflow.items(), key=lambda kv: -kv[1]):
            pct = (n / n_workflows * 100) if n_workflows else 0.0
            print(f"  {v:18} {n:4} ({pct:.0f}%)")

    print()
    print(f"Total cost: ${cost_total:.2f}")
    print(f"Total time: {time_total}s ({time_total/60:.1f} min)")
    if n_attempts:
        avg_cost = cost_total / n_attempts
        avg_time = time_total / n_attempts
        print(f"Avg per attempt: ${avg_cost:.2f} / {avg_time/60:.1f} min")
    print("=" * 60)
    return 0


# ── Argparse wiring ──────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phalanx shadow",
        description="Phalanx v1.7.3-ledger MVP — shadow-mode dispatch + ledger.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Dispatch shadow run on a GHA workflow failure")
    p_run.add_argument("--repo", required=True, help="owner/name (e.g. encode/httpx)")
    p_run.add_argument(
        "--workflow-run-id",
        type=int,
        required=True,
        help="GitHub Actions workflow run id (the failed run to shadow).",
    )
    p_run.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds between status polls (default 10).",
    )
    p_run.add_argument(
        "--poll-timeout",
        type=int,
        default=1800,
        help="Total seconds to wait for terminal (default 1800).",
    )

    p_show = sub.add_parser("show", help="Pretty-print one ledger row by id")
    p_show.add_argument("ledger_id")

    p_exp = sub.add_parser("export", help="Dump the ledger as JSON")
    p_exp.add_argument("out", help="Output file path (e.g. ledger.json)")
    p_exp.add_argument("--repo", help="Filter to one repo")
    p_exp.add_argument("--limit", type=int, default=500)

    p_met = sub.add_parser(
        "metrics",
        help="Print ledger verdict counts. Counts every attempt by default.",
    )
    p_met.add_argument("--repo", help="Filter to one repo")
    p_met.add_argument(
        "--by-workflow",
        action="store_true",
        help=(
            "Also print counts deduplicated to LATEST attempt per "
            "(repo, workflow_run_id), so retries don't inflate verdicts."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "show":
        return asyncio.run(_cmd_show(args))
    if args.cmd == "export":
        return asyncio.run(_cmd_export(args))
    if args.cmd == "metrics":
        return asyncio.run(_cmd_metrics(args))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
