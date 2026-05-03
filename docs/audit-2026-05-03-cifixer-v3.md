# CI Fixer v3 — Architecture & State Audit

**Date**: 2026-05-03
**Author**: Raj (with Claude)
**Purpose**: Self-contained brief for external review (ChatGPT / advisor). Explains what we're building, the documented architecture, the deployed architecture, where they diverge, what's working, what's broken, and the open questions.

---

## 1. Product context

**Phalanx CI Fixer** auto-fixes broken CI checks on GitHub PRs. When a workflow fails (lint, tests, build), the bot reads the failure log, diagnoses, applies a patch, pushes, and re-runs. Goal: ship to GitHub Marketplace.

**Definition of Done for v3 (the "DoD")**: one external open-source Python repo green end-to-end. Picked: `humanize` repo, "Path 1" — a known wild bug in datetime/timezone handling. Internal testbed (`usephalanx/phalanx-ci-fixer-testbed`) has 4 cells (lint, test_fail, flake, coverage) we use as a regression suite.

---

## 2. The 5-agent architecture

### 2.1 Roles (per spec `docs/v17-tl-as-planner.md`)

| Agent | LLM | Cost cap | Job |
|---|---|---|---|
| **Commander** | None — deterministic | n/a | TPM. Webhook precheck, persist tasks, dispatch, close run. No engineering judgment. |
| **Tech Lead (TL)** | GPT-5.4 | $5 | SOLE strategic LLM. Reads CI log + repo state. Emits `task_plan` (a DAG of subtasks). Three modes: `PLAN`, `REVIEW`, `REPLAN`. |
| **Challenger** | Claude Sonnet 4.6 | $5 | Cross-model adversarial reviewer of TL's plan. Has a `dry_run_verify` tool. Default-accept; objections must cite evidence. |
| **SRE Setup** | Claude Sonnet (agentic loop) | $4 | Provisions a Docker sandbox. Tier 0 (workflow YAML extraction) → Tier 1 (lockfile detection) → Tier 2 (agentic LLM fallback). |
| **Engineer** | None (deterministic step interpreter) OR Sonnet (v1.6 fallback) | $1 | Executes TL's `Step` actions verbatim: `read`, `replace`, `insert`, `delete_lines`, `apply_diff`, `run`, `commit`, `push`. No LLM judgment. |
| **SRE Verify** | None — deterministic | n/a | Runs `verify_command` in the sandbox. Reports `all_green` or `new_failures`. |

Total run cap: $25 (was $25, briefly $30, currently $25 in deployed config — see 4.3).

### 2.2 Documented dispatch flow (spec)

```
GitHub webhook
      │
      ▼
  Commander
      │
      ├─ precheck (dedup, repo-enabled, version=v3)
      ├─ open Run row
      └─ persist task #1: TL plan          ← ONE TASK
              │
              ▼
        ┌────TL plan mode────┐
        │ • fetch_ci_log     │
        │ • read_file (ground)│
        │ • emit task_plan   │              ← TL is the planner
        └─────────┬──────────┘
                  │
                  ▼
         Commander reads task_plan
         → persists tasks #2..#N
         → dispatches them
                  │
                  ▼
           [SRE setup if needed]
                  │
                  ▼
              Engineer
                  │
                  ▼
            SRE Verify
                  │
                  ▼
        ┌── TL review mode ──┐
        │ ship / replan?      │
        └──────┬──────────────┘
               │
       ┌───────┴────────┐
       │                │
   SHIP signal     REPLAN signal
                        │
                        ▼
                 TL replan → emit
                 delta task_plan →
                 commander extends DAG
```

Key spec excerpts (verbatim from `docs/v17-tl-as-planner.md`):

- Line 48: *"Commander | None | n/a | Webhook precheck, persist tasks, dispatch, close. No engineering judgment."*
- Line 68: *"persist task #1: TL plan"*
- Line 158: *"task_plan: list[TaskSpec] — ordered DAG; commander persists these"*
- Line 204: *"You [TL] are the SOLE source of LLM judgment in this system. Engineer and SRE-verify do not think — they execute your instructions."*
- Line 489–512: spec includes pseudocode for `_persist_initial_dag` (creates ONLY the TL plan task) + `_extend_dag_from_tl_plan` (reads `task_plan` and persists tasks #2..#N).

### 2.3 Deployed dispatch flow (today's code)

```
GitHub webhook
      │
      ▼
  Commander
      │
      └─ persist 5 tasks UPFRONT, hard-coded:    ← FIVE TASKS
            seq=1: cifix_sre_setup
            seq=2: cifix_techlead
            seq=3: cifix_challenger
            seq=4: cifix_engineer
            seq=5: cifix_sre_verify
              │
              ▼
        Dispatch in sequence,
        each agent reads upstream output from DB

If sre_verify reports new_failures:
  Commander appends 3 more tasks (no SRE setup, no Challenger):
            seq=N+1: cifix_techlead
            seq=N+2: cifix_engineer
            seq=N+3: cifix_sre_verify
  Loops until all_green / cost cap / engineer-low-confidence-abort.
```

Source: `phalanx/agents/cifix_commander.py:_persist_initial_dag` (line 367–442) and `_persist_iteration_dag` (line 478–520).

### 2.4 Spec ↔ code deviations

| Spec says | Deployed code does |
|---|---|
| Commander persists ONE task (TL plan) | Persists 5 tasks |
| `_extend_dag_from_tl_plan` reads `task_plan` and dispatches | This method **does not exist** |
| Final task is TL in REVIEW mode (decides SHIP/REPLAN/ESCALATE) | No REVIEW mode — Commander reads `sre_verify.verdict` directly |
| Iteration via TL REPLAN emitting delta `task_plan` | Iteration via Commander hard-coding `[techlead, engineer, sre_verify]` |
| Challenger BLOCKS dispatch on P0 objection | Shadow mode — verdict logged but not consulted |
| TL's `task_plan` drives subsequent dispatch | `task_plan` is persisted as data; Commander ignores it for dispatch decisions |

**Net effect**: today's deployed system is "v1.7-shaped v1.6". The agents and data shapes match the spec; the dispatch logic is still v1.6's pre-baked DAG.

---

## 3. Data flow per agent

Each agent receives a small Celery message (the "notification" — just bookkeeping IDs):

```json
{
  "run_id": "<uuid>",
  "ci_fix_run_id": "<uuid>",
  "repo": "owner/name",
  "branch": "...",
  "sha": "...",
  "pr_number": 34,
  "failing_job_id": "<github_id>",
  "failing_job_name": "Lint",
  "ci_provider": "github_actions",
  "build_url": "...",
  "sre_mode": "setup" | "verify"   // SRE only
  "iteration": 2                    // iter 2+ only
  "prior_sre_failures": [...]      // iter 2+ only
}
```

The actual *work data* is read from the Postgres `tasks` table — each agent queries upstream task outputs by `run_id` + `agent_role` + `status='COMPLETED'`.

### 3.1 What each agent reads from upstream (DB queries)

| Reader | Reads | From | Purpose |
|---|---|---|---|
| TL | `workspace_path`, `container_id` | `cifix_sre_setup` output | grep/read_file in workspace |
| Challenger | full `fix_spec` | `cifix_techlead` output | review TL's plan |
| Challenger | `workspace_path` | `cifix_sre_setup` output | dry_run_verify in sandbox |
| Engineer | full `fix_spec` (incl. `task_plan`) | `cifix_techlead` output | execute TL's steps |
| Engineer | `workspace_path` | `cifix_sre_setup` output | apply edits, commit |
| SRE Verify | `container_id`, `workspace_path` | `cifix_sre_setup` output | docker exec |
| SRE Verify | `verify_command`, `verify_success` | `cifix_techlead` output | what to run + how to interpret success |
| TL iter-2 | `prior_sre_failures` | passed in `description` by Commander | replan input |

Engineer never reads Challenger's verdict. Challenger is observability-only today.

---

## 4. Tonight's lint cell run (real prod data)

### 4.1 Setup

- Pushed PR #34 to testbed with patch `01-lint-e501.patch` — adds a 129-char string to `src/calc/formatting.py` (deliberate E501 violation).
- CI fails on `Lint` job. Webhook fires.
- Commander dispatches the 5-task DAG.

### 4.2 Iter 1 trace (run_id `6639bc9f-abec-44fd-93fa-8216e38ed0e3`)

| seq | agent | status | duration | output (excerpt) |
|---|---|---|---|---|
| 1 | sre_setup | COMPLETED | 30s | `container_id=b89d65bf9dc8`, `workspace_path=/tmp/forge-repos/v3-...-sre`, env_spec from Tier 0 (workflow YAML extraction) |
| 2 | techlead | COMPLETED | 57s | `verify_command="ruff check src/calc/formatting.py"`, `confidence=0.94`, task_plan with `replace`+`commit`+`push`+`run` steps |
| 3 | challenger | COMPLETED | 12s | `verdict=accept`, dry_run_verify confirms exit=1 pre-fix |
| 4 | engineer | COMPLETED | 1s | commit `4283033d662d9f...` — wraps the long line correctly (verified on GitHub) |
| 5 | sre_verify | COMPLETED | 0s | `verdict=new_failures`, `verify_scope=narrow_from_tl`, `exit_code=1`, `stderr_tail=""` |

Engineer's iter-1 commit (verified by API):

```diff
-    return "This is a very long descriptive message intended to trip ruff's E501 line-length check deliberately for the testbed."
+    return (
+        "This is a very long descriptive message intended to trip ruff's "
+        "E501 line-length check deliberately for the testbed."
+    )
```

This is **lint-clean**: every line ≤ 75 chars; testbed config is `line-length = 100`. Confirmed locally with `uvx ruff check` — exit 0.

### 4.3 Iter 2 + iter 3

- Iter 2: TL re-investigates with `prior_sre_failures`, produces an even shorter wrap. Engineer pushes commit `a432e3cb...` (also lint-clean). SRE verify still reports exit_code=1 with empty stderr.
- Iter 3 (227s, 8 LLM turns): TL **correctly diagnoses the bug** — confidence drops to 0.4. Quote from its output:

  > *"The remaining reported failure is a stale Ruff E501 against an older one-line version of src/calc/formatting.py; the current workspace already contains the wrapped-string fix, so there is no new code defect to patch."*

  TL's `open_questions`:
  > *"The fetched GitHub Actions log shows the pre-fix one-line string, but the current workspace and PR diff already show verbose_description split across two literals; I could not fetch a tl_verify_command-specific log to prove why SRE still observed exit 1."*

- Engineer refuses to act on confidence < 0.5 → run terminates `FAILED`.

### 4.4 Why the verify keeps failing (root cause we identified)

I ran ruff manually inside a fresh sandbox against the post-engineer-edit workspace. Output:

```
E902 No such file or directory (os error 2)
 --> src/calc/formatting.py:1:1
Found 1 error.
exit=1
```

The file *exists* (cat works). But ruff reports E902. We strongly suspect a workspace-sync mismatch: provisioner uses `docker cp <workspace>/. <container>:/workspace` at setup time (one-shot snapshot copy, not bind-mount). Engineer's edits land in the host workspace at `/tmp/forge-repos/...`, but the sandbox's `/workspace` was frozen at initial-clone state. Need to confirm — provisioner code is at `phalanx/ci_fixer_v3/provisioner.py:_docker_cp_workspace`.

Two distinct bugs surfaced:

1. **Engineer's edits don't propagate into the sandbox.** Sandbox tests stale content. (Architectural — needs decision: bind-mount, re-cp after each edit, edit-inside-sandbox, or re-clone-at-verify.)
2. **`_exec_in_container` only captures stderr.** Ruff writes violations to stdout. We've been blind to *why* verify failed across all iterations. (One-line fix to `ExecResult` dataclass.)

---

## 5. What v1.7.2.2 fixed (deployed today)

Prior bug: SRE verify ran the broad workflow enumeration (`ruff check .`) instead of TL's narrow `verify_command` (`ruff check src/calc/formatting.py`). Found unrelated lint elsewhere in the repo, masked correct fixes, looped to turn cap.

Fix (committed as `f433a59`): in `cifix_sre.py:_execute_verify`, read TL's `fix_spec.verify_command` first; if present, route to a new `_execute_verify_narrow` that runs that single command and applies `verify_success.exit_codes` + `stderr_excludes` matcher. Falls back to enumeration only when TL omitted `verify_command`.

15 tier-1 tests + 7 adversarial seam tests pin the contract (`tests/unit/ci_fixer_v3/test_v1722_verify_scope.py`, `tests/integration/v3_harness/test_mini_lint_simulation.py`).

**Verified live in prod**: every `cifix_sre_verify` row in the lint run shows `verify_scope=narrow_from_tl`. The seam fix is working. The run still failed because of bugs 1+2 in §4.4, which were **hidden behind the v1.7.2.2 bug** and surfaced once it was closed.

---

## 6. Test coverage

### Tier-1 (unit, isolated, no Docker/Postgres)

- `tests/unit/ci_fixer_v3/` — 314 tests, ~13 sec, all passing
- Major surfaces: TL self-critique, plan validator, engineer step interpreter, SRE setup loop/cache/evidence/tools, provisioner argv, sanitization (v1.7.2), routing (challenger queue, SRE split), TL corpus (12 fixtures), challenger regression, lockfile detection, workflow extraction, Tier 0/1 selection, **v1.7.2.2 verify scope (15 tests)**.

### Tier-2 (integration harness)

- `tests/integration/v3_harness/` — 70+ tests across DAG persist shape, fix_spec parser, env detector per-language, engineer guard order, TL self-critique, sre_verify YAML parser, **mini-lint coordination simulation (7 adversarial scenarios + replay slot)**, TL corpus harness, Challenger regression.
- Real-run replay slot: `fixtures/real_runs/*.json` auto-discovered. Empty for now — drop a captured prod run JSON dump to add coverage.

### What's NOT covered

- **End-to-end** with real Docker + real Postgres + real GitHub. We have the regression script (`scripts/v3_python_regression.sh <cell>`) but it's a single-cell happy-path; no broader chaos coverage.
- **Workspace-sync seam** (the bug we hit). No test would have caught Engineer's edits not propagating to sandbox. We need either a real-Docker tier-3 test or a contract test against the provisioner's mount semantics.
- **Stdout/stderr capture symmetry**. No test catches "verify failed but stderr is empty."

---

## 7. Open questions for review

These are the questions I want a second opinion on:

### Q1. Spec-vs-deployed mismatch

The deployed Commander hard-codes a 5-task DAG. The spec says Commander persists ONE TL plan task and dispatches from `task_plan`. **Should we (a) catch the code up to the spec, or (b) update the spec to match the code, or (c) live with the divergence given Phase 2 isn't passing yet?**

Tradeoffs:
- (a) is the right architectural posture but adds 1–2 days of real refactoring before we can resume Phase 2 validation. Affects `cifix_commander.py`, plan validator wiring, REVIEW-mode TL prompt.
- (b) is honest but undermines the "TL is the planner, Commander is a TPM" thesis that justified v1.7 in the first place.
- (c) accepts tech debt to reach DoD faster. Risk: every future bug in this seam compounds.

### Q2. Where does TL's `read_file` source content from?

The spec lists `read_file`/`grep`/`glob` in TL's tool registry but doesn't specify where they read from when no SRE setup has run yet (i.e., when Commander only persists the TL plan task per the spec).

Options:
- TL reads via GitHub API (`GET /repos/{owner}/{repo}/contents/{path}?ref={sha}`). No local clone needed for TL's investigation.
- Commander pre-creates SRE setup as plumbing-before-TL even in the spec-aligned flow (initial DAG = 2 tasks, not 1).
- TL handles its own clone (returns to "TL touches git" — abandons the SRE/TL boundary).

### Q3. Workspace-sync architecture

Engineer edits land in the host workspace; sandbox has a snapshot from setup time. Four options:

- **(A) Bind-mount** the host workspace into the sandbox (`-v <ws>:/workspace`). Cleanest. But: the worker container itself is sandboxed, and the named volume `forge-repos` is what's actually shared. Need to verify the path resolves correctly across worker-container boundary.
- **(B) Re-cp after each engineer edit**. Hacky band-aid; easy to forget; doesn't compose with multi-step plans.
- **(C) Engineer edits *inside* the sandbox** via `docker exec`, commits from inside the sandbox. Cleanest semantically. Largest refactor — engineer step interpreter currently uses host file paths.
- **(D) SRE verify re-clones from GitHub** at verify time. Engineer pushes; verify pulls. Simple. Adds clone latency per iteration.

### Q4. Cost matrix

Today's deployed cost cap is $25/run. The spec's per-task caps are $5 (TL) + $5 (Challenger) + $4 (SRE setup) + $1 (engineer). Sum = $15, leaving $10 of headroom. Realistic? Cheap? Should the Challenger budget actually be $5 if it's running in shadow mode?

### Q5. Challenger gating

Challenger runs in shadow mode today. Verdict logged but not consulted. The spec says it should BLOCK dispatch on P0 objections. **When do we flip the gate on?** After Phase 2 4/4? After a calibration period?

### Q6. Iter-3 TL diagnosed correctly but couldn't act

In §4.3, iter-3 TL produced this diagnosis verbatim:

> *"The remaining reported failure is a stale Ruff E501 against an older one-line version... I could not fetch a tl_verify_command-specific log to prove why SRE still observed exit 1."*

TL **correctly identified the workspace-sync bug** but had no tool to confirm it (no way to read the sandbox's view of the file from inside TL). Should TL get a `read_sandbox_file(container_id, path)` tool? Or is that scope creep? The deeper question: how much of system-level debugging should TL be empowered to do vs. delegate to a human?

### Q7. Architecture rating

If you were rating this v3 architecture against "ship to GitHub Marketplace" today, what would you score it /10? What's the single biggest gap holding it back?

---

## 8. Repo pointers (for the reviewer)

- Spec: `docs/v17-tl-as-planner.md`, `docs/v171-provisioning-tiers.md`, `docs/v172-sandbox-hardening.md`, `docs/v17-architecture-gaps.md`
- Commander: `phalanx/agents/cifix_commander.py`
- TL: `phalanx/agents/cifix_techlead.py`
- Challenger: `phalanx/agents/cifix_challenger.py`
- Engineer: `phalanx/agents/cifix_engineer.py` + `_engineer_step_interpreter.py`
- SRE: `phalanx/agents/cifix_sre.py` + `phalanx/ci_fixer_v3/sre_setup/`
- Provisioner: `phalanx/ci_fixer_v3/provisioner.py`
- Plan validator: `phalanx/agents/_plan_validator.py`
- v17 types: `phalanx/agents/_v17_types.py`
- Workflow extractor (Tier 0): `phalanx/agents/_v171_workflow_extractor.py`
- Lockfile detect (Tier 1): `phalanx/agents/_v171_lockfile_detect.py`
- Setup cache: `phalanx/agents/_v171_setup_cache.py`
- Sandbox sanitization: `phalanx/agents/_v172_sanitize.py`
- Tests: `tests/unit/ci_fixer_v3/`, `tests/integration/v3_harness/`
- Recent commits: `git log --oneline -15` shows the v1.7.x ladder

GitHub: `https://github.com/usephalanx/phalanx` (private).

---

## 9. Commit log (recent)

```
359393b chore: track uv.lock
fd21eb6 test(ci-fixer-v3): mini-lint coordination simulation — adversarial seam tests
f433a59 fix(ci-fixer-v3): v1.7.2.2 — SRE verify reads TL's narrow verify_command
5315e8b fix(v3): v1.7.1.1 — exclude test runners from Tier 0 setup commands
5261488 fix(v3): v1.7.2.1 — rollback cap-drop+no-new-privileges (apt incompat)
ea52a04 chore(prod): add cifix_challenger queue to Celery worker
e39da69 feat(ci-fixer-v3): v1.7.2 — sandbox hardening (sanitize + caps + framing)
f6a1a9e feat(ci-fixer-v3): v1.7.1 — provisioning tiers (cache + Tier 0/1)
b56cbb4 feat(ci-fixer-v3): v1.7.0 — TL/Challenger/Engineer/SRE redesign
```

End of audit.
