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
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
