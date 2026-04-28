# CI Fixer v3 — webhook coordination & dedup architecture

This document describes how Phalanx prevents duplicate / racing v3 dispatches against the same PR, and the patterns we evaluated. Background is bug #11 (deep analysis 2026-04-28): when a CI run has multiple failing jobs and v3 commits a partial fix that pushes a new SHA, the old time-window dedup had bypass edges and we ended up with two parallel v3 runs for the same PR.

This is **not** a Phalanx-specific problem. Any system that reacts to events on a long-lived resource (a PR) and produces new events as side-effects (its own commits) hits the same class of issues. We evaluated 7 patterns from production systems before picking 3.

## Patterns we shipped (v1.3.42)

### A1 — bot-author filter via `fix_commit_sha` lookup

**The fix that does most of the work** (Renovate / Dependabot / Sweep pattern).

When v3's engineer commits a fix, it writes the commit SHA to `CIFixRun.fix_commit_sha`. The webhook handler queries `_is_phalanx_fix_commit(repo, head_sha)` at entry. If the failing CI's head_sha matches a fix we just pushed, log + skip dispatch.

Files: [phalanx/agents/cifix_engineer.py](../phalanx/agents/cifix_engineer.py) (writer), [phalanx/api/routes/ci_webhooks.py](../phalanx/api/routes/ci_webhooks.py) (reader). Tests: [tests/unit/test_webhook_bot_loop_guard_unit.py](../tests/unit/test_webhook_bot_loop_guard_unit.py), [tests/integration/v3_harness_t2/test_webhook_bot_loop_guard.py](../tests/integration/v3_harness_t2/test_webhook_bot_loop_guard.py).

**Why not the existing `sender.login` filter?** Because token-based pushes (the only kind v3 does) report `sender.login = PAT owner` in the webhook, NOT the git author. The pre-existing `git_author_name` filter is dead code in our setup.

### A3 — idempotency key via `check_suite.id`

**Stripe / AWS Lambda / Square pattern.** Replace time-window heuristic dedup with a deterministic GitHub-supplied identifier. Multiple `check_run.completed` webhooks of the same workflow run share `check_suite.id`; we use it as the dedup key.

Files: migration [alembic/versions/20260428_0001_ci_check_suite_idem.py](../alembic/versions/20260428_0001_ci_check_suite_idem.py); column on [models.py CIFixRun](../phalanx/db/models.py); event field on [CIFailureEvent](../phalanx/ci_fixer/events.py); dedup query + INSERT in [_dispatch_ci_fix](../phalanx/api/routes/ci_webhooks.py).

Has both the fast-path query (skip if found) and the unique partial index `ci_fix_runs_repo_check_suite_idem` as backstop. Concurrent racers both pass the query — the INSERT side-fires `IntegrityError` on the loser. (Today we let the exception propagate; future could catch and treat as dedup-hit.)

**Why partial index, not full?** Legacy rows have `ci_check_suite_id IS NULL`. NULL == NULL doesn't collide in PG, but a non-partial unique constraint would still permit duplicates if both sides are NULL. Partial constraint targets only rows where the column is set.

### B2 — pg advisory lock per (repo, pr_number)

**Mergify / Atlas / Postgres-native pattern.** Wraps `_dispatch_ci_fix` in `pg_try_advisory_xact_lock(hash(repo, pr))`. Non-blocking; loser logs + returns None. Auto-released at tx commit/rollback.

Files: top of [_dispatch_ci_fix](../phalanx/api/routes/ci_webhooks.py).

**Catches:** two concurrent webhooks for DIFFERENT check_suites of the same PR (which A3 doesn't catch — different keys). Common case: a manual workflow re-run while v3 iter-2 is still in flight.

**Trade-off:** adds one PG round-trip per webhook for repos with PR numbers. PG advisory locks are extremely cheap (microseconds) so the cost is negligible.

## Patterns we evaluated but DIDN'T ship

### A2 — workflow_run event subscription

GitHub fires three event types: `check_run` (per-job), `check_suite` (per-suite), `workflow_run` (per-workflow). Subscribing to `workflow_run.completed` instead of `check_run.completed` gives ONE event per CI build with all jobs aggregated.

**Used by:** Vercel preview-comments, Linear's GitHub integration.

**Why we skipped:** A1 + A3 + B2 cover the main race surface. A2 would be a bigger architectural change (webhook subscription update + payload parsing rewrite) and yields incremental value beyond what we already have. Reconsider if we hit a multi-job race A1+A3 don't catch.

### B1 — cancel-supersede on new dispatch

GitHub Actions / CircleCI built-in pattern: when a new run arrives for a resource, mark old in-flight runs as SUPERSEDED. Their `concurrency.cancel-in-progress` config is one-line.

**Why we skipped:** v3 runs commit real PRs in flight; cancelling mid-iteration risks orphan branches and confused customers. B2 (skip on contention) is safer for our trust model — the original run finishes its job, the new one just doesn't start.

### B3 — single coordinator workflow per PR

Temporal / Airflow `max_active_runs=1` pattern. One long-lived workflow per PR; all events for that PR feed in as signals. Workflow internally decides whether to start a new iteration.

**Why we skipped:** Major refactor. Phalanx doesn't currently have a workflow orchestrator like Temporal in the stack. Revisit if A1+A3+B2 prove insufficient and we're budget for a rearchitecture.

### B4 — cancel-on-new-commit

When a new commit lands while v3 is iterating on stale state, abort the run. CircleCI auto-cancel / GitLab `interruptible:` pattern.

**Why we skipped:** v3's iter-2 mechanism already handles "stale fix doesn't fully resolve CI" — that's what SRE verify is for. Cancel-on-new-commit would race against iter-2 dispatch.

## Architectural defenses summary

| layer | catches | failure mode if it fails |
|---|---|---|
| A1 — bot-author filter | v3's own pushes triggering CI | wasteful 2nd v3 run, harmless if iter-2 mechanism works |
| A3 — idempotency key (DB) | retries / multi-job same-suite race | unique-index INSERT fails; exception currently propagates |
| B2 — advisory lock | concurrent dispatches racing past dedup | only the first webhook wins; loser logs + skips |

Three independent layers. Each has a fail-open mode (best-effort). Bug class doesn't bypass all three at once.

## Bug #11 specifically — what we ARE NOT solving

Bug #11's actual binding constraint was **bug #12** (iter-2 TL stuck IN_PROGRESS for 80+ min). Even with all 7 patterns above, if iter-2 doesn't run, the coverage cell can't ship cleanly. A1+A3+B2 are about **architectural cleanup** — they prevent waste and tighten the model — but the coverage-cell unblock is bug #12.

See [project_v3_bugs_11_12_coverage_TL_hallucination.md](../../.claude/projects/-Users-raj-forge/memory/project_v3_bugs_11_12_coverage_TL_hallucination.md) (memory) for #12 investigation handoff.

## What's next

If we hit a webhook-coordination bug A1+A3+B2 don't catch:

1. First check whether SRE verify caught the underlying CI failure (iter-2 mechanism). If yes, we don't have a coordination bug — we have a verify-correctness bug.
2. If a real coordination bug surfaces, lean toward A2 (workflow_run subscription) before B3 (Temporal). A2 is incremental; B3 is a rebuild.
3. Avoid B1/B4 — cancellation in mid-iteration risks user-visible weirdness on real PRs.

## References (real systems we drew from)

- **Renovate** — bot-author filter (A1)
- **Dependabot** — bot-author filter at GitHub side (A1 native)
- **Stripe API** — idempotency keys (A3)
- **Mergify** — pg advisory locks (B2)
- **Atlas DB migrations** — pg advisory locks (B2)
- **GitHub Actions `concurrency:`** — cancel-supersede (B1)
- **CircleCI auto-cancel-redundant-workflows** — cancel-supersede (B1)
- **Temporal "workflow per business entity"** — single coordinator (B3)
- **Vercel preview comments** — workflow_run subscription (A2)
- **Linear GitHub integration** — check_suite subscription (A2)
