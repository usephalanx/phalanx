"""Direct v2 agent simulation — bypass the GitHub webhook.

Why this exists: the webhook-triggered path is 2-5 min of round-trip
(GitHub CI → check_run webhook → Celery dispatch → worker → agent) for
every iteration. When we're debugging the agent itself, that latency
drowns the feedback loop. `simulate` invokes `execute_v2_run` directly
in-process with a pre-populated CIFixRun row, so iterations are
bounded only by how long the agent takes to run against real LLMs +
sandbox.

Usage (on prod, inside the ci-fixer-worker container):

    docker exec phalanx-prod-phalanx-ci-fixer-worker-1 \\
      python -m phalanx.ci_fixer_v2.simulate \\
        --repo usephalanx/phalanx-ci-fixer-testbed \\
        --pr 1 \\
        --branch fail/lint-e501 \\
        --sha a5083d5 \\
        --job-id 72044739081 \\
        --failing-command "ruff check ."

What it does:
  1. Look up CIIntegration for --repo (must already exist)
  2. Create a fresh CIFixRun row with status=PENDING (or --reuse an
     existing one)
  3. Call `run_bootstrap.execute_v2_run` synchronously
  4. Print the outcome + the DB snapshot + cost breakdown

Exit code: 0 if verdict=committed, 1 otherwise.

Exceptions surface directly here — no Celery, no burying in worker logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid


async def _lookup_integration_id(repo_full_name: str) -> str:
    from sqlalchemy import select

    from phalanx.db.models import CIIntegration
    from phalanx.db.session import get_db

    async with get_db() as session:
        result = await session.execute(
            select(CIIntegration).where(
                CIIntegration.repo_full_name == repo_full_name
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise RuntimeError(
                f"no CIIntegration row for {repo_full_name} — insert one first"
            )
        if not row.enabled:
            raise RuntimeError(
                f"CIIntegration for {repo_full_name} is enabled=False"
            )
        return row.id


async def _create_ci_fix_run(args: argparse.Namespace, integration_id: str) -> str:
    from phalanx.db.models import CIFixRun
    from phalanx.db.session import get_db

    ci_fix_run_id = str(uuid.uuid4())
    async with get_db() as session:
        run = CIFixRun(
            id=ci_fix_run_id,
            integration_id=integration_id,
            repo_full_name=args.repo,
            branch=args.branch,
            pr_number=args.pr,
            commit_sha=args.sha,
            ci_provider="github_actions",
            ci_build_id=args.job_id,
            build_url=f"https://github.com/{args.repo}/actions/runs/{args.job_id}",
            failed_jobs=[args.failing_job_name] if args.failing_job_name else [],
            failure_summary=args.failing_command or "",
            failure_category=None,
            status="PENDING",
            attempt=1,
            tokens_used=0,
        )
        session.add(run)
        await session.commit()
    return ci_fix_run_id


async def _print_outcome(ci_fix_run_id: str, outcome) -> None:
    """Pretty-print the run outcome + DB snapshot to stdout."""
    from sqlalchemy import select

    from phalanx.db.models import CIFixRun
    from phalanx.db.session import get_db

    print()
    print("=" * 60)
    print(f"Verdict:      {outcome.verdict.value}")
    if outcome.escalation_reason is not None:
        print(f"Escalation:   {outcome.escalation_reason.value}")
    if outcome.committed_sha:
        print(f"Commit SHA:   {outcome.committed_sha}")
        print(f"Branch:       {outcome.committed_branch}")
    if outcome.explanation:
        print(f"Explanation:  {outcome.explanation[:300]}")
    print("=" * 60)

    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun).where(CIFixRun.id == ci_fix_run_id)
        )
        row = result.scalar_one()

    print()
    print(f"CIFixRun row ({ci_fix_run_id}):")
    print(f"  status:             {row.status}")
    print(f"  tokens_used:        {row.tokens_used}")
    print(f"  fingerprint_hash:   {row.fingerprint_hash or '(none)'}")
    print(f"  fix_strategy:       {row.fix_strategy or '(none)'}")
    print(f"  fix_commit_sha:     {row.fix_commit_sha or '(none)'}")
    print(f"  fix_branch:         {row.fix_branch or '(none)'}")
    if row.cost_breakdown_json:
        try:
            cost = json.loads(row.cost_breakdown_json)
            print(f"  total_cost_usd:     ${cost.get('total_cost_usd', 0):.4f}")
            gpt = cost.get("gpt_reasoning", {})
            sonnet = cost.get("sonnet_coder", {})
            print(
                f"  gpt_reasoning:      "
                f"in={gpt.get('input_tokens', 0)} "
                f"out={gpt.get('output_tokens', 0)} "
                f"reasoning={gpt.get('reasoning_tokens', 0)} "
                f"${gpt.get('cost_usd', 0):.4f}"
            )
            print(
                f"  sonnet_coder:       "
                f"in={sonnet.get('input_tokens', 0)} "
                f"out={sonnet.get('output_tokens', 0)} "
                f"thinking={sonnet.get('thinking_tokens', 0)} "
                f"${sonnet.get('cost_usd', 0):.4f}"
            )
            print(
                f"  sandbox_runtime_s:  "
                f"{cost.get('sandbox_runtime_seconds', 0):.2f}"
            )
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            print(f"  cost_breakdown_json parse error: {exc}")
    if row.error:
        print(f"  error:              {row.error[:500]}")


async def main_async(args: argparse.Namespace) -> int:
    from phalanx.ci_fixer_v2.run_bootstrap import execute_v2_run

    # Resolve integration
    try:
        integration_id = await _lookup_integration_id(args.repo)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Create or reuse the CIFixRun row
    if args.reuse:
        ci_fix_run_id = args.reuse
        print(f"[simulate] reusing existing CIFixRun {ci_fix_run_id}")
    else:
        ci_fix_run_id = await _create_ci_fix_run(args, integration_id)
        print(f"[simulate] created CIFixRun {ci_fix_run_id}")

    # Record-mode scaffolding. We keep one LLMRecorder per role (main,
    # coder) and pass a wrapper hook to execute_v2_run. The hook wraps
    # each LLM seam with the recorder before the agent loop starts.
    main_recorder = None
    coder_recorder = None
    if args.record:
        from phalanx.ci_fixer_v2.replay import LLMRecorder

        main_recorder = LLMRecorder(role="main")
        coder_recorder = LLMRecorder(role="coder")
        print(f"[simulate] RECORD mode: will write fixture to {args.record}")

    def _wrap_llm(role: str, inner):
        if role == "main" and main_recorder is not None:
            return main_recorder.wrap(inner)
        if role == "coder" and coder_recorder is not None:
            return coder_recorder.wrap(inner)
        return inner

    # Capture the final ctx so we can serialize tool_invocations into
    # the fixture. Closure over a list so the callback can mutate it.
    captured_ctx = {"ctx": None}

    def _capture_ctx(ctx):
        captured_ctx["ctx"] = ctx

    # Run the agent
    print(
        f"[simulate] executing execute_v2_run({ci_fix_run_id}) — "
        f"repo={args.repo} pr={args.pr} branch={args.branch}"
    )
    try:
        outcome = await execute_v2_run(
            ci_fix_run_id,
            llm_wrapper=_wrap_llm if args.record else None,
            ctx_sink=_capture_ctx if args.record else None,
        )
    except Exception as exc:  # surface directly, don't bury in Celery
        print(f"[simulate] run raised: {exc!r}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 3

    await _print_outcome(ci_fix_run_id, outcome)

    # Write the fixture after a successful run. We only record runs
    # that committed cleanly — a fixture of a broken run is not a
    # useful pin. Operator can override with --record-on-any-outcome
    # if they want to pin an escalation case.
    if args.record and (
        outcome.verdict.value == "committed" or args.record_on_any_outcome
    ):
        await _write_fixture(
            args.record,
            cell=args.cell_name or f"{args.repo.replace('/', '_')}_{args.branch.replace('/', '_')}",
            ci_fix_run_id=ci_fix_run_id,
            main_recorder=main_recorder,
            coder_recorder=coder_recorder,
            outcome=outcome,
            args=args,
            ctx=captured_ctx["ctx"],
        )
        print(f"[simulate] wrote fixture → {args.record}")
    elif args.record:
        print(
            f"[simulate] NOT writing fixture (verdict={outcome.verdict.value}); "
            "use --record-on-any-outcome to override.",
            file=sys.stderr,
        )

    return 0 if outcome.verdict.value == "committed" else 1


async def _write_fixture(
    path: str,
    *,
    cell: str,
    ci_fix_run_id: str,
    main_recorder,
    coder_recorder,
    outcome,
    args: argparse.Namespace,
    ctx,
) -> None:
    """Dump a replay fixture capturing the LLM traffic + tool trace +
    final outcome for one recorded run."""
    from pathlib import Path

    from phalanx.ci_fixer_v2.replay import Fixture, ToolCallRecord

    # Pull the tool_invocations trace from the final AgentContext
    # captured via the ctx_sink hook on execute_v2_run.
    tool_calls: list[ToolCallRecord] = []
    if ctx is not None:
        for inv in ctx.tool_invocations:
            tool_calls.append(
                ToolCallRecord(
                    turn=inv.turn,
                    tool_name=inv.tool_name,
                    tool_input=inv.tool_input,
                    tool_result=inv.tool_result,
                    error=inv.error,
                )
            )

    fx = Fixture(
        cell=cell,
        initial_context={
            "repo": args.repo,
            "pr": args.pr,
            "branch": args.branch,
            "sha": args.sha,
            "job_id": args.job_id,
            "failing_command": args.failing_command,
            "failing_job_name": args.failing_job_name,
        },
        llm_calls=(
            (main_recorder.calls if main_recorder else [])
            + (coder_recorder.calls if coder_recorder else [])
        ),
        tool_calls=tool_calls,
        expected_outcome={
            "verdict": outcome.verdict.value,
            "escalation_reason": (
                outcome.escalation_reason.value
                if outcome.escalation_reason
                else None
            ),
            "committed_sha": outcome.committed_sha,
            "committed_branch": outcome.committed_branch,
            "ci_fix_run_id": ci_fix_run_id,
        },
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(fx.to_json())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Directly simulate a v2 CI Fixer run against a real CIFixRun "
        "+ real LLMs + real sandbox — bypasses the GitHub webhook dispatch."
    )
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--branch", required=True, help="PR head branch")
    parser.add_argument("--sha", required=True, help="failing commit SHA")
    parser.add_argument(
        "--job-id",
        required=True,
        help="GitHub Actions job id (the agent calls fetch_ci_log with this)",
    )
    parser.add_argument(
        "--failing-command",
        default="",
        help="Seed hint for the agent's original_failing_command",
    )
    parser.add_argument(
        "--failing-job-name",
        default="Lint",
        help="Human-readable job name (for CIFixRun.failed_jobs)",
    )
    parser.add_argument(
        "--reuse",
        default=None,
        help="Reuse an existing CIFixRun id instead of creating a new row",
    )
    parser.add_argument(
        "--record",
        default=None,
        help=(
            "Path to write a replay fixture JSON (e.g. "
            "tests/fixtures/scorecard/python/test_fail.json). "
            "Captures every main+coder LLM round-trip + the final outcome. "
            "Re-run deterministically offline via the replay test harness."
        ),
    )
    parser.add_argument(
        "--record-on-any-outcome",
        action="store_true",
        help=(
            "Write fixture even if the run escalated/failed (default: only "
            "record committed runs — broken runs are not useful to pin)."
        ),
    )
    parser.add_argument(
        "--cell-name",
        default=None,
        help=(
            "Label for the fixture's cell field (e.g. 'python_test_fail'). "
            "Defaults to a sanitized combination of repo+branch."
        ),
    )
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
