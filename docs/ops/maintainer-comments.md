# Maintainer-facing PR comments (Path B — 2026-05-20)

Operator runbook for the per-repo opt-in PR-comment delivery that
ships with Path B. For the maintainer-facing explanation see
[docs/ops/permissions.md](permissions.md).

## What landed

A single new code module + one new column. No new orchestration layer,
no new agent, no new queue, no architecture changes.

- New: [phalanx/shadow/maintainer_comments.py](../../phalanx/shadow/maintainer_comments.py) — pure renderer + best-effort GitHub poster
- New: `ci_integrations.maintainer_comments_enabled` (BOOLEAN, default FALSE; migration `20260520_0001`)
- Modified: [phalanx/shadow/ledger.py](../../phalanx/shadow/ledger.py) — `update_with_results` now invokes the poster after the terminal commit
- Modified: [phalanx/maintenance/ledger_reconciler.py](../../phalanx/maintenance/ledger_reconciler.py) — same hook on the reconciliation path
- New: 35 unit tests in [tests/unit/shadow/test_maintainer_comments.py](../../tests/unit/shadow/test_maintainer_comments.py)
- New: [docs/ops/permissions.md](permissions.md) — maintainer-facing permission doc

## Lifecycle

```
shadow run finalizes
    ↓
ledger row terminal-written (update_with_results OR reconciler)
    ↓
session.commit() — row is durable, dispatch is safe
    ↓
append_ledger_row_async(...) — JSONL durability (P0-2)
    ↓
post_maintainer_comment_async(row, integration_enabled, integration_token)
    ├── opt-in flag false on this repo? → return None (default behavior)
    ├── token missing? → return None
    ├── verdict suppressible (infra failure, synth fallback, no PR)? → return None
    ├── prior comment for this ledger_id exists (sentinel marker)? → return None
    └── POST to /repos/{repo}/issues/{pr}/comments
            ├── 2xx → comment posted, log "maintainer_comment.posted"
            └── any error → log "maintainer_comment.post_failed", return None
```

The poster **never raises**. Ledger durability comes first.

## Suppress matrix

| Verdict | provenance.tl_task_status | rc synthesized? | failure_class | Posts? |
| ------- | ------------------------- | --------------- | ------------- | ------ |
| `PENDING` | any | any | any | ❌ (not terminal) |
| `SHIPPED_PROPOSED` | any | any | any | ✅ |
| `SAFE_ESCALATE` | `COMPLETED` | false | any | ✅ |
| `SAFE_ESCALATE` | `COMPLETED` | **true** | any | ❌ (synthesized — uninformative) |
| `SAFE_ESCALATE` | `FAILED`/`CANCELLED`/`PENDING` | any | any | ❌ (TL didn't really run) |
| `FAILED` | any | any | `FAILED_SANDBOX_SETUP_*` | ❌ (Phalanx-internal infra noise) |
| `FAILED` | any | any | `FAILED_INFRA_*` | ❌ (same) |
| `FAILED` | `COMPLETED` | false | (other) | ✅ (engineer tried, couldn't verify) |
| any | any | any | any (when `pr_number IS NULL`) | ❌ |

## Enrollment recipe

```sql
-- Enable maintainer comments on a repo Phalanx is already enrolled on.
UPDATE ci_integrations
   SET maintainer_comments_enabled = true, updated_at = NOW()
 WHERE repo_full_name = 'rnagulapalle/sandbox';
```

To turn it off without un-enrolling the repo (analysis continues
internally; just no maintainer-visible comment):

```sql
UPDATE ci_integrations
   SET maintainer_comments_enabled = false, updated_at = NOW()
 WHERE repo_full_name = 'rnagulapalle/sandbox';
```

To check the current state of all enrolled repos:

```sql
SELECT repo_full_name, enabled, maintainer_comments_enabled, cifixer_version
  FROM ci_integrations
 ORDER BY repo_full_name;
```

## Token requirements

The PAT for any repo with `maintainer_comments_enabled = true` MUST
include the `Pull requests: read AND write` fine-grained permission (or
the classic `repo` scope; not recommended — see permissions.md).

If `maintainer_comments_enabled = false`, the read-only token shown in
permissions.md is sufficient.

The token is stored in `ci_integrations.github_token` and is read at
comment-post time. No new credential plumbing needed.

## Verification

After enrolling a repo and enabling comments:

1. **Dispatch a shadow run that produces a meaningful terminal verdict**
   (a SAFE_ESCALATE with `tl_task_status=COMPLETED` is the easiest to
   reproduce — pick a PR where the failing CI log is available).
2. **Check the PR for the new comment** via the GitHub UI.
3. **Confirm the sentinel marker** is present:
   ```bash
   gh pr view <pr-number> --repo <repo> --comments | grep "phalanx-shadow-ledger-id"
   ```
4. **Re-dispatch the same workflow** (creates a new ledger row with
   `attempt_number=2`). Verify a SECOND comment is posted — the
   marker check is per-ledger_id, not per-PR.
5. **Reconcile a stale row** (manually trigger
   `phalanx.maintenance.ledger_reconciler.reconcile_shadow_ledger`).
   Verify NO additional comment is posted if the terminal verdict didn't
   change (idempotency).

## Failure modes + observability

All comment-related logs use the `maintainer_comment.*` namespace:

- `maintainer_comment.posted` — success; carries `comment_id` from GitHub
- `maintainer_comment.idempotent_skip` — sentinel marker found; no double-post
- `maintainer_comment.suppressed.opt_out` — integration flag is false
- `maintainer_comment.suppressed.no_token` — `github_token` is null/empty
- `maintainer_comment.suppressed.missing_fields` — no PR number or no ledger_id
- `maintainer_comment.suppressed.verdict_rule` — verdict shape doesn't qualify (reason in payload)
- `maintainer_comment.post_failed` — GitHub API error; ledger row stays durable; carries error details

Operator query for recent comment activity:

```bash
docker logs --since 1h forge-worker 2>&1 | grep maintainer_comment
docker logs --since 1h forge-api 2>&1 | grep maintainer_comment
```

## Rollback

If the comment delivery starts behaving badly:

1. **Per-repo off-switch** — set `maintainer_comments_enabled=false`
   on the affected repos. Immediate; no restart needed.
2. **Global off-switch** — set the flag false on every row:
   ```sql
   UPDATE ci_integrations SET maintainer_comments_enabled=false;
   ```
3. **Code rollback** — `git revert` the Path B commit. Reverts the hook
   in `update_with_results` + the reconciler. The new column survives
   the revert as NULL (default false) — safe.

The migration's downgrade (`alembic downgrade 20260513_0001`) drops the
column. Optional; leaving the column as default-false is also safe.

## What this does NOT change

- Shadow-mode safety on opt-OUT repos: **unchanged**. Zero side effects.
- Provenance schema: **unchanged** (still v2; no new fields).
- Reconciler decision logic: **unchanged**. The maintainer-comment hook
  fires AFTER reconciliation, only as a side effect.
- Verdict semantics: **unchanged**. The same classifier produces the
  same verdicts; the new code is purely a delivery layer.
- Stuck-task detection or beat scheduler: **unchanged**.

## Known limitations

1. **No way to edit / delete a posted comment from inside Phalanx.** If
   a posted comment is wrong, the maintainer must reply on it or the
   operator must delete it manually via the GitHub UI. A maintainer-
   facing "report wrong diagnosis" feedback loop is on the external-beta
   roadmap, not this milestone.
2. **No batching.** One comment per ledger row terminal. For repos that
   produce many dispatches per hour, this could become noisy. Not
   expected to bite in the pilot (max ~10 dispatches/day per repo);
   would need rate limiting before external beta.
3. **Comment posts use the operator's PAT**, so the comment shows up
   as authored by `rnagulapalle`. A GitHub App would let comments be
   authored by `phalanx[bot]` — that's the right next milestone for
   trust signaling.
4. **The sentinel marker is best-effort idempotent**: if a maintainer
   manually edits or deletes a posted comment, the marker disappears,
   and a subsequent reconciler-driven update could re-post. Acceptable
   for the pilot; would need a DB-backed comment_id store for stronger
   guarantees.
