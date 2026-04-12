# Builder Git Conflict Strategy

## Overview

When DAG orchestration is enabled (`phalanx_enable_dag_orchestration=True`), multiple builder tasks run in **parallel waves**. They share the same git workspace (`{workspace_root}/{project_id}/{run_id}/`), which creates race conditions during the git push step. This document covers the problem, the fix implemented, risk analysis, and future enhancements.

---

## Problem: Parallel Builder Push Race

**Root cause (two compounding gaps):**

1. `_setup_git_workspace()` did `fetch` + `checkout` but **no pull** — so Builder B starts from the same base commit as Builder A.
2. `_commit_changes()` silently caught all push exceptions — so when Builder B's push fails with `non-fast-forward`, the task was still marked `COMPLETED` and files never reached the remote.

**Result:** The second parallel builder's files were lost from the showcase/GitHub branch, even though the task showed COMPLETED.

---

## Fix Implemented

### `_setup_git_workspace()` — pull before work starts

```python
# After checkout:
try:
    repo.git.pull("--rebase", "origin", branch)
except Exception:
    pass  # branch may not exist on remote yet — that's fine
```

Also: detect and abort stale rebase state left by a crashed parallel builder:

```python
rebase_merge = workspace / ".git" / "rebase-merge"
rebase_apply = workspace / ".git" / "rebase-apply"
if rebase_merge.exists() or rebase_apply.exists():
    try:
        repo.git.rebase("--abort")
    except Exception:
        pass
```

### `_commit_changes()` — rebase + retry on push failure

```python
try:
    repo.git.push("origin", branch, "--set-upstream")
except Exception as push_exc:
    # Non-fast-forward: fetch remote, rebase, retry once
    try:
        repo.remotes.origin.fetch()
        repo.git.rebase(f"origin/{branch}")
        sha = repo.head.commit.hexsha[:8]   # SHA changes after rebase
        repo.git.push("origin", branch, "--set-upstream")
    except Exception as rebase_exc:
        # True merge conflict — files on disk, push skipped
        try:
            repo.git.rebase("--abort")
        except Exception:
            pass
```

---

## Conflict Types and Handling

| Type | Description | Detection | Resolution |
|------|-------------|-----------|------------|
| **Non-fast-forward** | Remote has commits this builder doesn't have (most common in parallel DAG) | Push exception | `fetch → rebase → retry` |
| **Non-conflicting overlap** | Two builders modified same file at different lines | Rebase succeeds silently | Auto-resolved by git rebase |
| **True merge conflict** | Two builders modified same lines in same file | Rebase fails with CONFLICT | Abort rebase, log warning, files remain on disk |
| **Stale rebase state** | Previous parallel builder crashed mid-rebase | `.git/rebase-merge` exists | Abort before proceeding |

---

## Risk Analysis

### Risk 1: Rebase rewrites commit SHA
**Impact:** Low. The SHA returned from `_commit_changes()` is used only for the DB artifact record (display-only in run reports). Nobody validates it post-run.
**Mitigation:** After a successful rebase push, re-read `repo.head.commit.hexsha[:8]` before returning instead of the pre-rebase SHA. ✅ Implemented.

### Risk 2: Stale rebase state from crashed parallel builder
**Impact:** Medium. If a builder process is killed mid-rebase (OOM, SIGKILL), `.git/rebase-merge/` is left on disk. The next builder to open the same workspace fails on checkout.
**Mitigation:** Detect `.git/rebase-merge` or `.git/rebase-apply` at the start of `_setup_git_workspace()` and run `git rebase --abort` before proceeding. ✅ Implemented.

### Risk 3: `pull --rebase` stomps uncommitted work
**Impact:** None. `_setup_git_workspace()` is called at the start of a task before any files are written. No uncommitted state exists at that point.
**Mitigation:** N/A.

### Risk 4: Retry loop on non-transient errors
**Impact:** Low. If push fails for a reason other than non-fast-forward (bad token, no network, repo deleted), rebase succeeds but second push also fails. Wastes one extra fetch+rebase attempt.
**Mitigation:** We only retry once — not a loop. Failure path is the same as before the fix (warning log, task still COMPLETED with files on disk).

### Risk 5: Test mocks break on push retry
**Impact:** Medium. Tests that mock `repo.git.push` to raise will now also trigger `remotes.origin.fetch()` and `repo.git.rebase()` calls — those mocks must be present.
**Mitigation:** Updated test class `TestBuilderGitPushConflict` in `tests/unit/test_epic_branch_unit.py` with full mock coverage for all retry/conflict scenarios. ✅ Implemented.

---

## Rollback Plan

The entire fix is contained within two private methods in `phalanx/agents/builder.py`:
- `_setup_git_workspace()` lines ~241–275
- `_commit_changes()` lines ~576–600

**Rollback steps:**
1. Revert those methods to the pre-fix version
2. Run `./deploy.sh` (one command, ~2 min)
3. No DB migration, no infra change, no compose change needed
4. **Worst-case regression:** Push silently fails — same as before the fix

---

## Future Enhancements

### Enhancement 1: Per-builder isolated workspaces (eliminates the race entirely)
Instead of sharing `{workspace_root}/{project_id}/{run_id}/`, give each builder its own clone:
```
{workspace_root}/{project_id}/{run_id}/{task_seq}/
```
Each builder clones fresh, pushes independently. No shared state, no race condition. Tradeoff: more disk I/O and clone time per task.

**When to do:** If true merge conflicts become common (e.g., many tasks touching the same files), this is the cleaner long-term fix. For now, the rebase retry handles the 99% case.

### Enhancement 2: Classify push errors before retrying
Instead of retrying all push failures, check the error message:
```python
if "non-fast-forward" in str(push_exc) or "rejected" in str(push_exc):
    # retry with rebase
else:
    self._log.warning("builder.git.push_auth_or_network_error", ...)
```
Avoids the extra fetch+rebase attempt on auth errors or network failures.

### Enhancement 3: Surface push conflicts in Slack run report
Currently, a true conflict is only visible in Celery worker logs. The run report shows `COMPLETED` with no indication that the push was skipped.

Proposed: Add `push_status` to the artifact record (`pushed | rebased | conflict_skipped`) so the Slack run summary can show:
```
✅ seq=03 builder COMPLETED  Build Frontend  ⚠️ push conflict — files on disk, not in remote
```

### Enhancement 4: Conflict resolution via Claude
When a true merge conflict occurs, instead of aborting, feed the conflicted diff to a Claude call:
```
CONFLICT in src/App.tsx:
<<<<<<< HEAD (builder A's version)
...
=======
...
>>>>>>> origin/branch (builder B's version)
```
Claude resolves the conflict, the builder commits the resolved file and pushes. This turns Type 2 (true conflict) into auto-resolved. High value for large parallel DAG runs.

---

## Research: Why Rebase Over Merge

- **Rebase** replays local commits on top of remote HEAD → linear history, no merge commits, clean showcase branch
- **Merge** creates a merge commit → clutters the branch with `Merge branch 'phalanx/run-xyz' into phalanx/run-xyz` messages
- For AI-generated code in a showcase repo, linear history is strongly preferred for readability
- Rebase is safe here because the builders never share their local commits with users who might have checked out the branch

## Research: The Celery Fork Pool + asyncio.sleep Non-Release Issue

Related context: the concurrency deadlock that triggered the need for this analysis.

Celery's default fork pool does NOT release a worker slot during `asyncio.sleep()`. The orchestrator's poll loop (`asyncio.sleep(15)`) blocks the worker for the entire run duration. With `--concurrency=4` and 4 concurrent commander tasks, all 4 slots are occupied → dispatched sub-tasks (builders, planners) can never start → deadlock.

**Fix applied:** Bumped `--concurrency` from 4 to 8 in `docker-compose.prod.yml`.

**Long-term fix:** Move the orchestrator poll loop to a non-blocking mechanism (Celery beat + DB polling, or Redis pub/sub for task completion signals) so commander tasks don't hold a worker slot for their full duration.
