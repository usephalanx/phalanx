# Phalanx CI Fixer v2 — Implementation Spec

**Status:** DRAFT — pending Raj review
**Date:** 2026-04-18
**Authors:** Raj (founder/CTO) + principal-engineer review
**Supersedes:** the current `phalanx/ci_fixer/` deterministic pipeline (to be deleted after MVP exit — no parallel operation beyond the cutover window)

This document defines the target architecture, tool set, memory integration, testing discipline, and MVP milestones for CI Fixer v2. It is the single source of truth; any proposed deviation requires updating this spec first.

---

## 0. What changed from v1 (and why)

The v1 CI Fixer is a deterministic multi-phase pipeline (`log_parser → classifier → sandbox → analyst → validator → commit`). Each phase is a single LLM call producing a JSON schema; Python code executes the flow. It spent a week in development without proving a single end-to-end use case.

v2 replaces the inner engine with **one agent, tools, and a loop** (the pattern used by Claude Code, Cursor Composer, Aider, Cline, and all top SWE-bench entrants). The LLM decides each turn which tool to call; the path through tools is emergent, not predetermined. Existing v1 components (`log_parser`, `sandbox`, `validator`) are re-purposed as *tools* the agent calls — not stages it traverses.

The product framing also sharpens: CI Fixer v2's goal is **close the PR with author approval**, not "patch the code." Most CI failures are not pure code problems (flakes, CI config, deps, test isolation); the agent diagnoses first and decides what kind of fix applies.

---

## 1. Non-goals (MVP)

To prevent scope creep — these are explicitly **out of scope** for MVP and will not be built during the 4-week window:

- Multi-agent choreography (peer agents debating, voting). Single-agent-with-subagent only.
- `.github/workflows/` YAML editing as a fix action.
- Lockfile regeneration (`package-lock.json`, `poetry.lock`, `Gemfile.lock`).
- Cross-repo pattern promotion (the current Phase 5 in `phalanx/ci_fixer/pattern_promoter.py` stays dark).
- Proactive scanning of repos for potential failures.
- Tier 2 semantic memory retrieval (pgvector). Deferred to v2.1 once enough fix history accumulates (target: ~200 real outcomes).
- Shadow-run mode (observing live PRs without committing). Scheduled for post-MVP.

---

## 2. Architecture overview

```
┌──────────────────────────────────────────────────────────────────┐
│  CI webhook → CIFixRun row → phalanx-ci-fixer-worker (N1 split)  │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                      CIFixerV2Agent (main)                       │
│                         Model: GPT-5.4                           │
│               Reasoning effort: medium (default)                 │
│                     Max turns per run: 25                        │
│                                                                   │
│   Loop: while not done and turn < MAX_TURNS:                     │
│       resp = llm_call(system, messages, tools)                   │
│       for tool_use in resp.tool_uses:                            │
│           result = execute_tool(tool_use)                        │
│           messages.append(result)                                │
│                                                                   │
│   Hard gates:                                                    │
│     • commit_and_push → requires last_sandbox_verified == True   │
│     • low-confidence self-assessment → must call escalate        │
│     • turn cap reached → auto-escalate (no silent fail)          │
└─────────────────────────┬────────────────────────────────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
┌──────────────────────────┐   ┌────────────────────────────────┐
│  Diagnosis / Read tools  │   │  delegate_to_coder (subagent)  │
│  (deterministic, fast)   │   │  Model: Claude Sonnet 4.6      │
│                          │   │  Extended thinking: 4k budget  │
│  fetch_ci_log            │   │  Max turns: 10                 │
│  get_pr_context          │   │  Tool scope: limited           │
│  get_pr_diff             │   │    (read_file, grep,           │
│  get_ci_history          │   │     apply_patch,               │
│  git_blame               │   │     run_in_sandbox only)       │
│  query_fingerprint       │   │                                │
│  read_file / grep / glob │   │  Returns verified diff +       │
│                          │   │  sandbox result to main agent  │
└──────────────────────────┘   └────────────────────────────────┘
              │                       │
              ▼                       ▼
┌──────────────────────────────────────────────────────────────────┐
│   Action tools (main agent only):                                │
│     commit_and_push, comment_on_pr, open_fix_pr_against_          │
│     author_branch, escalate                                      │
└──────────────────────────────────────────────────────────────────┘
```

**Flavor B rationale (GPT main + Sonnet coder subagent):**
- CI Fixer's hardest part is diagnosis, not code writing. GPT-5.4 leads.
- Patch application + sandbox verification is bounded scope; Sonnet's tool-use strength + cost profile fits.
- Single main loop preserves single-agent-with-subagent pattern. Not a dual pipeline.
- Either provider can act as solo fallback if the other is unavailable.

---

## 3. Agent system prompt (GPT-5.4 main agent)

Draft text (subject to refinement during week 1 calibration):

```
You are Phalanx CI Fixer, an autonomous senior engineer whose single job is
to close a pull request whose CI failed. You operate unattended after a
GitHub / CircleCI webhook — the PR author is not watching you in real time.

Your goal is NOT to write a patch. Your goal is to close the PR with the
author's approval. Many CI failures do not need a code change at all:
flakes need a rerun or an isolation fix, missing coverage may need a test
or a threshold tweak, timeouts may need a slower-test fix, broken CI YAML
is not this PR's problem, and preexisting failures on main should be
noted and returned to the author rather than silently patched.

Workflow (not a rigid script — you decide each step):
  1. Diagnose. Read the failing log. Understand what kind of failure it is.
     Check whether this test has been flaking on main (get_ci_history).
     Look at git blame on the failing code. Read the team's conventions
     (CLAUDE.md, CONTRIBUTING.md, style guides). Query prior fixes for this
     same failure pattern (query_fingerprint).
  2. Decide. Patch? Rerun? Mark-flake? Escalate to author? Do nothing
     because it's a main-branch problem?
  3. Act. For code changes, call delegate_to_coder with a precise plan; it
     will apply the patch and verify in sandbox, returning a verified diff.
     For non-code fixes, use the appropriate tool or escalate.
  4. Verify. You may not call commit_and_push unless your last tool result
     confirms the sandbox run of the ORIGINAL FAILING COMMAND passed. If
     verification fails, iterate or escalate.
  5. Coordinate. Every commit must be paired with a PR comment explaining
     diagnosis + fix + reasoning. The author should understand, not just
     see a commit.

Hard rules (non-negotiable):
  • Sandbox verification is the only trusted signal. Never commit on the
    basis of a local run, a visual diff check, or your own reasoning alone.
  • If you are not confident, call escalate with a clear reason and a
    draft patch. Never silently fail, never invent a fix to satisfy the
    turn budget.
  • Do not edit .github/workflows/ files.
  • Do not regenerate lockfiles.
  • Do not touch files outside the scope of the failing job.
  • Max 25 turns per run. If you approach the limit without a verified fix,
    escalate.

You have access to memory of prior fixes in this repo. Consult it early
(query_fingerprint after reading the log). Prefer the pattern that worked
last time over inventing a new one — unless the context has changed.

You have two commit strategies, chosen by has_write_permission:
  • has_write_permission == True:  commit directly to the author's PR
    branch via commit_and_push(strategy="author_branch").
  • has_write_permission == False: open a PR against the author's PR
    branch via open_fix_pr_against_author_branch.
  • Always call comment_on_pr on the original PR to explain.
```

**Implementation notes:**
- System prompt stored as Python string constant in `phalanx/ci_fixer_v2/agent.py`, not a `.txt` file — colocation with the loop makes review easier.
- Prompt iteration during week 1 is expected; each change gets rerun against the full simulation corpus to measure regression.

---

## 4. Tool catalog

All tool schemas are JSON Schema (compatible with OpenAI tool-use and Anthropic Messages API). Every tool is a deterministic Python function; no tool contains an LLM call except `delegate_to_coder`.

### 4.1 Diagnosis tools

#### `fetch_ci_log`
**Purpose:** Fetch the raw log of a failing CI job (GitHub Actions, CircleCI).
**Schema:**
```json
{
  "type": "object",
  "properties": {
    "provider": {"type": "string", "enum": ["github_actions", "circleci"]},
    "run_id": {"type": "string"},
    "job_id": {"type": "string"}
  },
  "required": ["provider", "run_id", "job_id"]
}
```
**Returns:** `{"log_text": str, "job_name": str, "duration_seconds": int, "exit_code": int}`
**Reuses:** existing log-fetching code in `phalanx/ci_fixer/` (refactored into a tool wrapper).

#### `get_pr_context`
**Purpose:** Fetch PR metadata (title, description, labels, author, head branch, base branch, has_write_permission).
**Schema:** `{"pr_number": int, "repo_full_name": str}`
**Returns:** `{pr: {...}, has_write_permission: bool}`.
**Notes:** `has_write_permission` determines commit strategy (author branch vs fix PR).

#### `get_pr_diff`
**Purpose:** Return the unified diff of the PR's head branch vs base.
**Schema:** `{"pr_number": int, "repo_full_name": str}`
**Returns:** `{"diff": str, "files_changed": [{"path": str, "additions": int, "deletions": int}]}`.

#### `get_ci_history`
**Purpose:** Flake detection. Returns last N CI runs touching the given test/file on the default branch with pass/fail outcomes.
**Schema:** `{"repo_full_name": str, "test_identifier": str, "days": int}`
**Returns:** `{"runs": [{"sha": str, "status": "passed"|"failed", "ran_at": iso8601}], "flake_rate": float}`.
**Notes:** `flake_rate >= 0.2` is a strong signal the failure is flaky rather than regression.

#### `git_blame`
**Purpose:** Standard blame for a file + line range.
**Schema:** `{"repo_path": str, "file": str, "line_start": int, "line_end": int}`
**Returns:** `{"lines": [{"line": int, "sha": str, "author": str, "date": iso8601, "summary": str}]}`.

#### `query_fingerprint`
**Purpose:** Tier-1 memory retrieval. Look up prior fixes for the same error pattern.
**Schema:** `{"repo_full_name": str, "tool": str, "sample_errors": [str]}`
**Returns:**
```json
{
  "found": true,
  "seen_count": 12,
  "success_count": 10,
  "failure_count": 2,
  "last_good_patch": "<unified diff>",
  "last_good_tool_version": "ruff 0.4.1",
  "last_outcome": "merged"
}
```
**Reuses:** `CIFailureFingerprint` table at [phalanx/db/models.py:841](../phalanx/db/models.py#L841). Existing code computes fingerprint hash from normalized errors.

### 4.2 Reading tools

#### `read_file`
`{"path": str, "line_start": int?, "line_end": int?}` → `{"content": str, "line_count": int}`
Safety: path must be inside the repo workspace; `..` rejected.

#### `grep`
`{"pattern": str, "path": str?, "glob": str?}` → `{"matches": [{"file": str, "line": int, "text": str}]}`.
Uses ripgrep under the hood. Results capped at 200 matches.

#### `glob`
`{"pattern": str, "path": str?}` → `{"files": [str]}`. Capped at 500.

### 4.3 Action tools

#### `delegate_to_coder` — see §5 for full contract

#### `run_in_sandbox`
**Purpose:** Execute a command in the sandbox container. Sandbox-only validation path (per N3).
**Schema:** `{"command": str, "timeout_seconds": int, "working_dir": str?}`
**Returns:** `{"exit_code": int, "stdout": str, "stderr": str, "duration_seconds": int, "timed_out": bool}`.
**Side effects:** tracks `context.last_sandbox_verified` — flips to True only if `command` matches the original failing CI command AND `exit_code == 0`.
**Reuses:** existing `phalanx/ci_fixer/sandbox.py` + `sandbox_pool.py` (N2 research will refine sandbox hardening).

#### `commit_and_push`
**Purpose:** Commit + push. Chooses strategy from `has_write_permission`.
**Schema:**
```json
{
  "branch_strategy": {"type": "string", "enum": ["author_branch", "fix_branch"]},
  "commit_message": {"type": "string"},
  "files": {"type": "array", "items": {"type": "string"}}
}
```
**Returns:** `{"sha": str, "branch": str, "pushed": bool}`.
**Hard gate:** fails with an explicit error if `context.last_sandbox_verified != True` at call time. Agent must re-verify after the final patch.

#### `open_fix_pr_against_author_branch`
**Purpose:** When we do not have write permission, open a PR whose base is the author's PR branch. Used instead of `commit_and_push(author_branch)`.
**Schema:** `{"title": str, "body": str, "base_branch": str, "head_branch": str}`
**Returns:** `{"pr_number": int, "pr_url": str}`.

### 4.4 Coordination tools

#### `comment_on_pr`
**Purpose:** Post a markdown comment on the original PR explaining diagnosis + fix + rationale.
**Schema:** `{"pr_number": int, "body": str}`
**Returns:** `{"comment_id": int, "url": str}`.

### 4.5 Escalation

#### `escalate`
**Purpose:** Clean exit when the agent is not confident. Posts a PR comment with diagnosis + draft patch (if any) + explicit reason for human attention. Records the run as `status=ESCALATED`.
**Schema:**
```json
{
  "reason": {"type": "string", "enum": [
    "low_confidence", "turn_cap_reached", "ambiguous_fix",
    "preexisting_main_failure", "infra_failure_out_of_scope",
    "destructive_change_required"
  ]},
  "draft_patch": {"type": "string"},
  "explanation": {"type": "string"}
}
```
**Returns:** `{"acknowledged": true}`.
**Side effect:** terminates the run.

### 4.6 Tool-access matrix

| Tool | Main agent | Coder subagent |
|---|---|---|
| fetch_ci_log | ✓ | — |
| get_pr_context | ✓ | — |
| get_pr_diff | ✓ | — |
| get_ci_history | ✓ | — |
| git_blame | ✓ | — |
| query_fingerprint | ✓ | — |
| read_file | ✓ | ✓ |
| grep | ✓ | ✓ |
| glob | ✓ | ✓ |
| delegate_to_coder | ✓ | — |
| run_in_sandbox | ✓ | ✓ |
| apply_patch | — | ✓ |
| commit_and_push | ✓ | — |
| open_fix_pr_against_author_branch | ✓ | — |
| comment_on_pr | ✓ | — |
| escalate | ✓ | — |

Coder subagent is **strictly sandboxed** — it cannot commit, comment, or escalate. It does one thing: apply a patch and verify it in sandbox.

---

## 5. Subagent contract: `delegate_to_coder`

### Input schema
```json
{
  "task_description": "string",
  "target_files": ["string"],
  "diagnosis_summary": "string",
  "failing_command": "string",
  "repo_workspace_path": "string",
  "max_attempts": 3
}
```
- `task_description` — specific, bounded patch plan from the main agent ("replace the unused import on line 12 of app/api.py")
- `target_files` — the narrow set the subagent may edit
- `failing_command` — the exact command to re-run in sandbox for verification (e.g., `ruff check app/`)

### Output schema
```json
{
  "success": true,
  "unified_diff": "string",
  "sandbox_exit_code": 0,
  "sandbox_stdout_tail": "string",
  "sandbox_stderr_tail": "string",
  "attempts_used": 1,
  "tokens_used": {"input": 0, "output": 0, "thinking": 0},
  "notes": "string"
}
```

### Scope and constraints
- Model: Claude Sonnet 4.6. Extended thinking budget: 4000 thinking tokens per turn.
- Max 10 turns inside the subagent.
- Tool access: `read_file`, `grep`, `apply_patch`, `run_in_sandbox` only.
- Must attempt the `failing_command` in sandbox before returning `success: true`.
- If all attempts fail, return `success: false` with the last sandbox output; the main agent decides whether to re-plan or escalate.
- Subagent cannot stage files outside `target_files`; enforced server-side.

### Why this shape
The subagent is not a peer agent — it is a scoped execution capability. Main agent retains all decision authority (commit, escalate, comment). This keeps the architecture single-agent-with-subagent, not multi-agent.

---

## 6. Loop pseudocode

```python
# phalanx/ci_fixer_v2/agent.py
MAX_TURNS = 25

async def run_ci_fix_v2(ci_fix_run: CIFixRun) -> CIFixOutcome:
    context = await _build_initial_context(ci_fix_run)
    # Tier-1 memory preload: inject top-3 prior fingerprint matches as
    # a system message so the agent sees them without spending a turn
    prior_fixes = await fingerprint_lookup(
        repo=ci_fix_run.repo_full_name,
        fingerprint_hash=ci_fix_run.fingerprint_hash,
    )
    context.seed_with_prior_fixes(prior_fixes)

    for turn in range(MAX_TURNS):
        response = await openai_reasoning_call(
            model=settings.openai_model_reasoning,  # gpt-5.4
            system=CI_FIXER_SYSTEM_PROMPT,
            messages=context.messages,
            tools=CI_FIXER_TOOLS,
            reasoning_effort="medium",
        )
        await record_agent_trace(ci_fix_run.id, turn, response)

        if response.stop_reason == "end_turn":
            # Agent terminated itself without calling escalate or commit.
            # Treat as implicit escalation — we never commit silently.
            return await escalate(ci_fix_run, reason="implicit_stop",
                                  explanation=response.text)

        for tool_use in response.tool_uses:
            if tool_use.name == "commit_and_push":
                if not context.last_sandbox_verified:
                    return await escalate(
                        ci_fix_run,
                        reason="verification_gate_violation",
                        explanation="commit_and_push called without "
                                    "sandbox verification",
                    )
                result = await execute_commit(tool_use.input, context)
                context.append_tool_result(tool_use.id, result)
                # Also require a comment_on_pr to have been posted —
                # enforced by a post-commit check in execute_commit.
                return await finalize_success(ci_fix_run, context)

            elif tool_use.name == "escalate":
                return await escalate(ci_fix_run, **tool_use.input)

            elif tool_use.name == "delegate_to_coder":
                result = await run_coder_subagent(
                    tool_use.input, context,
                )
                context.append_tool_result(tool_use.id, result)
                if (result["success"]
                        and result["sandbox_exit_code"] == 0
                        and result["failing_command_matched"]):
                    context.last_sandbox_verified = True

            elif tool_use.name == "run_in_sandbox":
                result = await execute_tool(tool_use.name, tool_use.input)
                context.append_tool_result(tool_use.id, result)
                # Verification flag only flips if the sandbox run covered
                # the original failing command with exit 0
                if (result["exit_code"] == 0
                        and command_covers(tool_use.input["command"],
                                           context.original_failing_command)):
                    context.last_sandbox_verified = True

            else:
                result = await execute_tool(tool_use.name, tool_use.input)
                context.append_tool_result(tool_use.id, result)

    # Turn cap hit without commit or escalate
    return await escalate(
        ci_fix_run,
        reason="turn_cap_reached",
        draft_patch=context.last_attempted_diff or "",
        explanation=f"Reached {MAX_TURNS} turns without verified fix.",
    )
```

Key invariants:
- No path commits without `last_sandbox_verified == True` (hard gate).
- No path exits silently. Every exit is one of: `finalize_success`, `escalate`. No `return None`.
- Every turn writes an `AgentTrace` row. Every tool call writes an `AgentTrace` row. Full replay capability.

---

## 7. Memory integration

### Preload (turn-zero)
Before turn 1, the loop computes the failure's fingerprint hash (re-using the existing fingerprinting code) and queries `CIFailureFingerprint` for prior matches. If found, the top-3 successful fixes are injected into `context.messages` as a system message:

```
PRIOR FIXES FOR THIS ERROR PATTERN IN THIS REPO:
  1. [2026-03-14, merged, author @alice] Fix E501 by wrapping line 42 with parentheses.
  2. [2026-03-08, merged, author @alice] Fix E501 by breaking string across multiple lines.
  3. [2026-02-27, reverted] Tried to add noqa — team prefers actual fixes.

Default to the pattern that has worked before unless the context has changed.
```

### Runtime queries
`query_fingerprint` tool allows the agent to re-query if it discovers a different fingerprint class mid-run (e.g., after reading the log it realizes the failure category is different from the webhook's initial classification).

### Outcome writes (at run end)
Every `CIFixRun` row already has `pipeline_context_json` (existing column). v2 writes:
- Decision timeline (ordered list of tool calls + decisions)
- Final verdict (`merged` / `escalated` / `failed`)
- Cost breakdown (§9)

After the poll window (existing `CIFixOutcome` table, 4h/24h/72h), the fingerprint is updated: `success_count++` if merged without revert, `failure_count++` if reverted or closed unmerged. This is existing code at `phalanx/ci_fixer/outcome_tracker.py` — reused unchanged.

### Migration required
Add `MemoryFact.agent_role: String(50)` nullable column + index on `(project_id, agent_role)`. Without it, CI Fixer's memory writes contaminate engineering-agent memory and vice versa. Migration file: `alembic/versions/20260419_0001_memory_fact_agent_role.py`.

---

## 8. Telemetry & observability

### Per-turn trace
Every LLM call and every tool call writes an `AgentTrace` row ([phalanx/db/models.py:442](../phalanx/db/models.py#L442)). The existing model supports this; no schema change required. Trace types used: `reflection`, `decision`, `tool_call`, `uncertainty`, `handoff` (to coder subagent), `self_check`.

### Cost breakdown
New column on `CIFixRun`:
```python
cost_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
```
JSON shape:
```json
{
  "gpt_reasoning": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0},
  "sonnet_coder": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.0},
  "sandbox_runtime_seconds": 0.0,
  "total_cost_usd": 0.0
}
```
Migration: `alembic/versions/20260419_0002_ci_fix_run_cost_breakdown.py`.

### Decision path (derived, read-only)
A view (or SQL function) that joins `AgentTrace` rows for a run into an ordered human-readable timeline: `turn 1: decided to grep for 'E501' → turn 2: read_file app/api.py:40-50 → turn 3: delegated to coder with patch plan → ...`. Used by the ops portal to audit decisions after the fact.

### Metrics exposed to the scoring harness
- Turns used per run (median, p95)
- Sandbox time used (median, p95)
- Escalation rate (% of runs that escalate vs commit)
- Tier-1 cache hit rate (% of runs where a matching fingerprint existed at turn-zero)
- Cost per merged PR

---

## 9. Data model changes (migration inventory)

All migrations are additive and nullable. None drop, alter types, or require data backfill.

| Migration | Purpose | Risk |
|---|---|---|
| `20260419_0001_memory_fact_agent_role.py` | Add `MemoryFact.agent_role: String(50)` nullable + index `(project_id, agent_role)` | None — additive |
| `20260419_0002_ci_fix_run_cost_breakdown.py` | Add `CIFixRun.cost_breakdown_json: Text` nullable | None — additive |

Plus a non-migration codebase change that is **also a pending audit fix** (should be paired with migration `20260419_0001` as a commit):
- First-ever migration (`20260317_0001_initial_schema`) gets `op.execute("CREATE EXTENSION IF NOT EXISTS vector")` as the first operation. Fixes audit item B. Idempotent — no-op if already installed.

---

## 10. Coverage policy

### Scope
- `phalanx/ci_fixer_v2/**` — 80% statement coverage floor.
- Critical tools (sandbox invocation, commit/push, escalation, verification gate): 90% floor.
- Legacy `phalanx/ci_fixer/**` remains at the existing 70% repo-wide floor — no forced cleanup during MVP.

### Enforcement
Separate pytest invocation in CI:
```bash
pytest tests/unit/ci_fixer_v2 tests/agent_harness \
  --cov=phalanx/ci_fixer_v2 \
  --cov-fail-under=80 \
  --cov-config=.coveragerc.v2
```
Critical-tool 90% gate enforced via per-module section in `.coveragerc.v2`.

### CI integration
New job in `.github/workflows/ci.yml`: `v2-coverage`, required for merge.

---

## 11. Simulation corpus

### Source repositories (per language)
Popular, actively maintained, large CI histories available:

- **Python:** `astral-sh/ruff`, `pytest-dev/pytest`, `tiangolo/fastapi`, `pallets/flask`, `pandas-dev/pandas`
- **JavaScript:** `facebook/react`, `vercel/next.js`, `vuejs/core`, `angular/angular`
- **TypeScript:** `microsoft/vscode`, `microsoft/TypeScript`, `prisma/prisma`
- **Java:** `spring-projects/spring-boot`, `elastic/elasticsearch`, `apache/kafka`, `google/guava`
- **C#:** `dotnet/aspnetcore`, `dotnet/runtime`, `dotnet/efcore`

### Harvest pipeline
`scripts/harvest_ci_fixtures.py` (new):
1. For each repo, query GitHub Actions API: `GET /repos/{owner}/{repo}/actions/runs?status=failure&per_page=100`.
2. For each failed run, fetch logs: `GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs` (zip).
3. Detect failure class from log (reuse existing `log_parser.py`).
4. Clone repo at the failing SHA (shallow, depth=1).
5. Capture PR context (title/body/diff/author) via `GET /repos/{owner}/{repo}/pulls/{pr_number}`.
6. Look up the author's resolution commit (the next commit on the PR branch that made CI green) — used as ground truth.
7. Run redaction: `detect-secrets` + regex scrub (GH tokens, AWS keys, emails, URL query params).
8. Write to `tests/simulation/fixtures/<lang>/<class>/<fixture_id>/`.

### Fixture structure
```
tests/simulation/fixtures/python/lint/ruff-fastapi-001/
  raw_log.txt
  clone_instructions.json       # {repo, sha, branch}
  pr_context.json               # PR metadata + diff
  ground_truth.json             # author's actual resolution commit
  meta.json                     # {language, failure_class, origin_repo, license, redaction_report}
```

### Targets
Minimum per language × failure class: **20 fixtures**.
Total MVP target: 5 langs × 4 classes × 20 = **400 fixtures**.
Distribution weighted to real-world frequency: lint ~50%, test_fail ~30%, flake ~15%, coverage ~5%.

### Rate limits
GitHub API authenticated limit: 5000 req/hr. Budget: ~3000 req per language-day of harvesting. Spread over 2-3 calendar days per language. Harvest script supports resume-from-cursor.

### Secrets & licensing
- **Redaction:** mandatory `detect-secrets` + regex sweep before any fixture is committed. Redaction report written to `meta.json`. Zero tolerance for committed secrets.
- **Licensing:** `meta.json` tags origin repo + license (MIT / Apache-2.0 / BSD / etc.). GPL-only sources excluded — we keep Phalanx MIT-compatible.
- **Size management:** if fixtures exceed ~500MB, move to git-lfs or a separate `phalanx-fixtures` repo pulled by tests.

---

## 12. Scoring harness

### Script
`scripts/run_simulation_suite.py` (new). Iterates fixtures, runs CI Fixer v2 end-to-end against each, captures outcomes.

### Per-fixture scoring

| Tier | Definition | Gating? |
|---|---|---|
| **Strict** | Agent's diff ≈ author's actual resolution (normalized-whitespace cosine similarity ≥ 0.85). | Informational only. Authors' styles vary. |
| **Lenient** | Original failing command passes in sandbox after the agent's fix. | **Gating** for MVP exit. |
| **Behavioral** | Agent reaches the correct decision class (patch / rerun / mark-flake / escalate / decline-as-preexisting). | **Gating** for MVP exit. |

### MVP exit gates (per language)
- **Lenient ≥ 95%** AND **Behavioral ≥ 99%** on the language's corpus.
- If either bar is missed at end of language-week, root-cause the failures and iterate the tool set; do not advance to the next language.

### Secondary metrics (not gating, tracked)
- Median + p95 turn count
- Median + p95 cost per fix
- Escalation rate
- Tier-1 cache hit rate
- Sandbox time

### CI integration
- Nightly run of the full simulation suite on a dedicated job.
- Per-PR runs on a **sampled subset** (20 fixtures stratified across languages/classes) — keeps per-PR wall time under 10 minutes while catching regressions early.
- Scoreboard published to `https://demo.usephalanx.com/ci-fixer-scoreboard` (future) and PR comments on release PRs.

### Enforcement timeline
- **Weeks 1–2:** scoring is informational only; we iterate the tool set based on scoreboard reads.
- **Week 3 onwards:** exit gates enforced — release PRs cannot merge if gates not met for any language already declared shipped.

---

## 13. Test tier mapping

| Tier | Location | Scope | Speed | When run |
|---|---|---|---|---|
| Unit | `tests/unit/ci_fixer_v2/` | Individual tool functions, pure-Python; LLM fully mocked | < 5s per test | Every PR |
| Agent harness | `tests/agent_harness/` | Scripted LLM responses; full loop; control-flow verification (turn cap, verification gate, escalation triggers) | < 30s per test | Every PR |
| Simulation | `tests/simulation/` | Real LLM, real sandbox, corpus fixtures, scored against the harness | Seconds per fixture; minutes total | Per-PR sample; nightly full run |
| Shadow runs | N/A (runtime feature) | CI Fixer observes live failing PRs on this repo without committing; logs would-be diffs for review | Live | Post-MVP |

---

## 14. Rollout & cutover

### Feature flag
`PHALANX_CI_FIXER_V2` env var (boolean), default `false`. When `true`, the webhook dispatches to `CIFixerV2Agent`; when `false`, to the legacy `CIFixerAgent`.

### Cutover plan
- **Week 0 (this spec accepted):** flag exists, default off. Legacy path unchanged.
- **Weeks 1–4:** v2 developed + measured behind the flag. Legacy still handles production CI failures.
- **MVP exit (all 4 languages pass gates):** flip default to `true`. Legacy remains as fallback.
- **+2 weeks stable (no regressions on scoreboard):** delete legacy `phalanx/ci_fixer/` code and flag. **No permanent dual-pipeline operation.**

### Dependency on other audit items
- **N1 (split ci_fixer worker):** must land before v2 starts running in prod. v2 relies on the dedicated worker having Docker socket access.
- **Memory migration (`agent_role`):** must land before v2 turn-zero preload is enabled. Non-blocking for pure harness tests.
- **B (pgvector extension):** bundled with memory migration — paired commit.
- **N2 (sandbox hardening research):** ongoing. MVP uses current sandbox posture; N2 proposal will land as a separate initiative and may tighten v2's sandbox call.

---

## 15. Milestones

| Week | Deliverables | Exit criteria |
|---|---|---|
| **Week 0** (pre) | Spec accepted; migration `memory_fact_agent_role` + `ci_fix_run_cost_breakdown` + pgvector-ensure land (paired commit); N1 worker split lands | Migrations green on staging; spec signed off |
| **Week 1 — Python** | Agent + all 12 tools + coder subagent; Python sandbox sufficient; corpus harvest complete for Python (80+ fixtures); scoring harness runnable | Lenient ≥ 95% & Behavioral ≥ 99% on Python corpus |
| **Week 2 — JavaScript + TypeScript** | ESLint + tsc log patterns; Node sandbox image validated; JS + TS corpus (40+ fixtures each) | Lenient ≥ 95% & Behavioral ≥ 99% on each |
| **Week 3 — Java** | JUnit + Maven + Gradle log patterns; JDK sandbox image; Java corpus (80+ fixtures); gates enforced on release PRs from this point | Lenient ≥ 95% & Behavioral ≥ 99% on Java |
| **Week 4 — C#** | dotnet test TRX parser; .NET SDK sandbox; C# corpus (80+ fixtures) | Lenient ≥ 95% & Behavioral ≥ 99% on C#; feature flag flipped on |

Each week also ships: coverage ≥ 80% on v2 code touched, 90% on critical tools; scoreboard artifact on the release PR; AgentTrace sampling exports for spot-check review.

---

## 16. Risks & open items

| # | Risk | Mitigation |
|---|---|---|
| R1 | GPT-5.4 identifier / availability uncertain at implementation time | Routed through `settings.openai_model_reasoning`; fall back to GPT-5 / o-series if 5.4 unavailable. |
| R2 | Simulation corpus rate-limits / license issues | Harvest over multiple days; exclude GPL-only; redact secrets; manual license audit before open-sourcing fixtures. |
| R3 | Coder subagent gets into a retry loop | Hard turn cap of 10 inside subagent; main agent decides after subagent returns `success: false`. |
| R4 | Author force-pushes over a Phalanx lint commit | Detect via webhook re-delivery; treat as implicit rejection; record outcome as `reverted`; feeds fingerprint failure_count. |
| R5 | Sandbox provisioning failure (GPT env setup returns empty, Docker daemon slow) | Sandbox is mandatory (N3). If provision fails: escalate `infra_failure_out_of_scope`. No local fallback. |
| R6 | Public repo CI logs exceed 10MB (huge pytest outputs) | Truncate to first + last 5000 lines with explicit "...truncated..." marker; existing log_parser handles truncation markers. |
| R7 | Some failures have no clean ground-truth resolution (PR closed without fix) | Corpus tagged `ground_truth: null`; those fixtures score only Lenient + Behavioral, not Strict. |
| R8 | Cost per fix exceeds acceptable threshold | Budgets tracked in `cost_breakdown_json`; alert if median > $1 per merged PR; tune reasoning_effort or model choice. |

### Open items to resolve during week 0

- **O1.** Exact GitHub App permissions needed for `has_write_permission` detection (repo:write vs. PR-level collaborator check).
- **O2.** Concrete cost target per fixed PR ($0.10? $0.50? $2.00?) — set during week 1, tuned.
- **O3.** Retention policy for fixture repos' shallow clones in `tests/simulation/fixtures/` (1GB cap? LFS threshold?).
- **O4.** Slack channel for escalations in OSS deployment (do we want a generic `#phalanx-escalations` channel or per-project routing?). Not MVP-blocking but affects UX.

---

## 17. Acceptance checklist (for spec sign-off)

Before any code is written, confirm:

- [ ] Architecture: single agent + tools + loop, Flavor B (GPT-5.4 main + Sonnet 4.6 coder subagent)
- [ ] Scope: top 5 languages (Python, JS, TS, Java, C#); 4 failure classes (lint, test_fail, flake, coverage)
- [ ] Memory: Tier 1 only; `MemoryFact.agent_role` migration; Tier 2 deferred
- [ ] Verification: sandbox-only, no local fallback; hard gate on `commit_and_push`
- [ ] Coordination: PR-against-author-PR default; direct commit with write-perm; always comment on original PR
- [ ] Loop discipline: max 25 main turns, max 10 subagent turns, no silent exits
- [ ] Coverage: 80% on v2 path, 90% on critical tools; new CI job `v2-coverage`
- [ ] Corpus: ~400 fixtures from listed public repos; redaction + licensing mandatory
- [ ] Scoring: Lenient ≥ 95% AND Behavioral ≥ 99% per language for MVP exit
- [ ] Rollout: flag-gated, legacy deletes after 2 weeks stable, no permanent dual pipeline
- [ ] Dependencies: N1 worker split + migration bundle land in week 0

Sign-off: **Raj (founder/CTO):** ____raj_________ **Date:** ___today__________
