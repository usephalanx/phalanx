# Multi-Agent CI Fixer — Architecture & Phased Plan

> **Status:** Design doc — pre-implementation  
> **Author:** FORGE Tech Lead  
> **Date:** 2026-04-15

---

## 1. Problem Statement

The current CI fixer is a single-agent loop. It works for simple lint violations but has fundamental gaps:

1. **No real environment** — it runs linters in a cloned workspace but never actually runs the app or tests
2. **Opens new PRs every run** — instead of committing to the existing failing PR
3. **Scoped to the CI log only** — doesn't know if the base branch is already broken
4. **No reproduction step** — fixes are applied without confirming the failure first
5. **One agent does everything** — no separation of concerns, hard to scale, hard to trust

The fix isn't to patch these one at a time. The fix is a coordinated multi-agent pipeline.

---

## 2. The Mental Model — How a Sr. Staff Engineer Actually Works

When a senior engineer sees a red CI build:

1. **Read the log** — understand exactly what failed and why
2. **Reproduce it locally** — run the exact same command CI ran, confirm it fails
3. **Fix it** — make the targeted change
4. **Validate in the same environment** — run the command again, confirm it passes
5. **Push to the same PR** — new commit, not a new PR
6. **Done** — CI goes green on the next run

This is the workflow the multi-agent system must replicate. Every agent maps to one of these steps.

---

## 3. Agent Roster & Responsibilities

```
CI Failure Event
      │
      ▼
┌─────────────────┐
│  Log Analyst    │  — parse CI logs → StructuredFailure + reproducer_cmd
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Root Cause     │  — classify tier, stack, confidence, escalation decision
│  Agent          │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
[L1: Auto]  [L2: Escalate → comment on PR, done]
    │
    ▼
┌─────────────────┐
│  Sandbox        │  — detect stack, spin container from pre-warmed image
│  Provisioner    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Reproducer     │  — run reproducer_cmd in sandbox, confirm failure
│  Agent          │
└────────┬────────┘
         │
    ┌────┴──────────┐
    │               │
    ▼               ▼
[Confirmed]    [Not reproduced → flaky/env issue → comment, done]
    │
    ▼
┌─────────────────┐
│  Fix Agent      │  — apply fix, run validation in same sandbox
│  (Claude Opus)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Verifier       │  — smoke test the app, confirm nothing else broke
│  Agent          │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Commit Agent   │  — push commit to EXISTING PR (not a new one)
└─────────────────┘
```

---

## 4. Agent Specifications

### 4.1 Log Analyst
- **Model:** GPT-4.1 (fast, cheap, structured extraction)
- **Input:** raw CI log text
- **Output:** `StructuredFailure`
  ```python
  @dataclass
  class StructuredFailure:
      tool: str               # "ruff", "pytest", "mypy", "tsc", etc.
      failure_type: str       # "lint", "test_regression", "build", "type_error"
      errors: list[ParsedError]
      reproducer_cmd: str     # exact command CI ran: "ruff check phalanx/ tests/"
      failing_files: list[str]
      log_excerpt: str
      confidence: float
  ```
- **Existing code:** largely maps to current `LogParser` + `LLMClassifier` — refactor, don't rewrite

### 4.2 Root Cause Agent
- **Model:** GPT-4.1
- **Input:** `StructuredFailure` + file contents of failing files
- **Output:** `ClassifiedFailure`
  ```python
  @dataclass
  class ClassifiedFailure:
      tier: Literal["L1_auto", "L2_escalate"]
      root_cause: str
      hypothesis: str
      stack: str              # "python", "node", "go", "java", "rust", "unknown"
      confidence: float
      escalation_reason: str  # populated if tier == L2
  ```
- **L1 criteria:** lint violations, unused imports, formatting, simple type annotation fixes
- **L2 criteria:** test regression, logic bug, unknown stack, low confidence (<0.7)

### 4.3 Sandbox Provisioner
- **Model:** None — fully deterministic
- **Input:** repo path + `ClassifiedFailure.stack`
- **Output:** running Docker container ID + workspace path
- **Stack detection order:**
  1. File existence: `pyproject.toml` → python, `package.json` → node, `go.mod` → go, etc.
  2. CI log hints: if detection fails, parse CI log for install commands
  3. LLM fallback: give GPT-4.1 the root dir listing + CI log
- **Pre-warmed images on prod:**
  - `phalanx-sandbox:python` — python 3.12, pip, ruff, mypy, pytest
  - `phalanx-sandbox:node` — node 22, npm, yarn, eslint, tsc
  - `phalanx-sandbox:go` — go 1.22+
  - `phalanx-sandbox:multi` — python + node combined
- **Fallback:** if stack unknown after LLM → skip to Escalate path

### 4.4 Reproducer Agent
- **Model:** Claude Opus 4.6 (tool use: `run_command`)
- **Input:** sandbox container, `reproducer_cmd`
- **Output:** `ReproductionResult`
  ```python
  @dataclass
  class ReproductionResult:
      confirmed: bool
      exit_code: int
      output: str
      verdict: Literal["confirmed", "flaky", "env_mismatch", "timeout"]
  ```
- **Logic:**
  - Run `reproducer_cmd` in sandbox
  - If it fails with same error → `confirmed`
  - If it passes → `flaky` (env issue, not code bug)
  - If it fails with a *different* error → `env_mismatch` (wrong stack/deps)
  - If timeout → escalate

### 4.5 Fix Agent
- **Model:** Claude Opus 4.6 (tool use: `read_file`, `write_file`, `run_command`, `finish`)
- **Input:** `StructuredFailure`, `ReproductionResult`, sandbox container
- **Output:** `VerifiedPatch`
  ```python
  @dataclass
  class VerifiedPatch:
      files_modified: list[str]
      validation_cmd: str
      validation_output: str
      success: bool
  ```
- **Constraints (unchanged from current design):**
  - write_file: empty-content guard, 70% shrink guard
  - sed/awk always available for large files
  - Full-repo validation before declaring success
  - Max 12 turns

### 4.6 Verifier Agent
- **Model:** Claude Opus 4.6
- **Input:** sandbox container, `VerifiedPatch`, stack type
- **Output:** `VerificationResult`
- **What it does:**
  - For Python: `pytest --tb=short -q` (no coverage, just pass/fail)
  - For Node: `npm test` or `npm run lint`
  - For unknown: skip (don't block on what we can't verify)
- **Phase 1:** optional/best-effort — don't block commit if verifier times out
- **Phase 2:** mandatory gate for test regression failures

### 4.7 Commit Agent
- **Model:** None — deterministic git ops
- **Input:** `VerifiedPatch`, original PR info
- **Output:** commit SHA pushed to existing PR branch
- **Key behavior:**
  - Look up open Phalanx fix PRs for this branch — if one exists, push to it
  - If none exists, open one (draft, targeting the failing branch)
  - Never open a second fix PR for the same branch
  - Commit message: structured, references the original CI run ID

---

## 5. Shared Context Object

All agents read from and write to a single `CIFixContext` object, persisted in DB:

```python
@dataclass
class CIFixContext:
    # Identity
    ci_fix_run_id: UUID
    repo: str
    branch: str
    commit_sha: str
    original_build_id: str

    # Agent outputs (written as pipeline progresses)
    structured_failure: StructuredFailure | None
    classified_failure: ClassifiedFailure | None
    sandbox_id: str | None
    reproduction_result: ReproductionResult | None
    verified_patch: VerifiedPatch | None
    verification_result: VerificationResult | None
    commit_sha_fix: str | None

    # Metadata
    started_at: datetime
    completed_at: datetime | None
    final_status: Literal["fixed", "escalated", "flaky", "env_mismatch", "failed"]
    pr_comment_posted: bool
```

This object is inspectable at any point. `GET /ci-fix-runs/{id}/context` returns the full pipeline state. No black boxes.

---

## 6. Fallback Ladder

Every exit path produces a useful artifact. No silent failures.

| Situation | Action |
|-----------|--------|
| Can reproduce + can fix | Verified patch committed to existing PR |
| Can reproduce + can't fix (max turns) | Root cause comment on PR, engineer knows exactly what to look at |
| Cannot reproduce (passes in sandbox) | "Looks flaky — reproduced cleanly locally. Recommend re-running CI." |
| Unknown stack (after LLM fallback) | Structured failure analysis comment + stack hypothesis |
| Base branch already broken | "Base branch has pre-existing failures. Fix those first." |
| Sandbox provision fails | Fall back to current workspace-only mode (no env validation) |
| Any agent timeout | Escalate with partial context, never hang |

---

## 7. Sandbox Architecture

### Pre-warmed images
Built once, stored on prod server. Rebuilt weekly via cron.

```dockerfile
# phalanx-sandbox:python
FROM python:3.12-slim
RUN pip install ruff mypy pytest pytest-asyncio pytest-cov
# No app code — that gets mounted at runtime
```

### Container lifecycle
```
provision()  → docker run -d --rm -v {workspace}:/app -w /app phalanx-sandbox:python
install()    → docker exec {id} pip install -e ".[dev]"   (~30s, cached layer)
run(cmd)     → docker exec {id} {cmd}                     (~1-5s per command)
teardown()   → docker stop {id}  (--rm handles cleanup)
```

### Dep caching
Cache the installed dep layer per `requirements hash`. If `pyproject.toml` hasn't changed since last run, skip `pip install` — use cached layer. Reduces install time from ~30s to ~2s on repeat runs.

### Security
- Network isolated: `--network none` after dep install
- No write access outside `/app`
- Hard CPU/memory limits: `--cpus 1 --memory 2g`
- Hard timeout: 5 minutes total per sandbox lifecycle

---

## 8. Quality Gates

Every agent has unit tests. Pipeline has integration tests. Coverage target: **≥80%**.

| Test type | What it covers |
|-----------|----------------|
| Unit — Log Analyst | Parses known log formats correctly, structured output |
| Unit — Root Cause Agent | Classification tiers, confidence thresholds, escalation |
| Unit — Sandbox Provisioner | Stack detection logic, all fallback paths |
| Unit — Reproducer Agent | Confirmed/flaky/env_mismatch verdicts |
| Unit — Fix Agent | Existing agentic loop tests + new sandbox integration |
| Unit — Verifier Agent | Pass/fail/skip verdicts per stack type |
| Unit — Commit Agent | PR continuity (no duplicate PRs), commit format |
| Integration — full pipeline | End-to-end with a real ruff failure, real sandbox, real commit |
| E2E — MESMD | No open CI failures after pipeline runs |

---

## 9. Phased Plan

### Phase 1 — Solid Foundation (current sprint)
**Goal:** Clean up what exists, establish the context object, add PR continuity.

- [ ] Refactor `CIFixerAgent` into the DAG agent pattern (Log Analyst + Root Cause already exist, formalize them)
- [ ] Introduce `CIFixContext` as the shared state object (DB-backed)
- [ ] Commit Agent: check for existing fix PRs before opening new ones
- [ ] Fix CI workflow triggers (PR #8 — in flight)
- [ ] ≥80% unit test coverage on all existing ci_fixer modules
- [ ] `GET /ci-fix-runs/{id}/context` endpoint — full pipeline state inspectable

### Phase 2 — Sandbox + Reproduction (next sprint)
**Goal:** The pipeline can reproduce failures, not just parse them.

- [ ] Build pre-warmed sandbox images (python, node, multi)
- [ ] `SandboxProvisioner` — stack detection + container lifecycle
- [ ] `ReproducerAgent` — run `reproducer_cmd` in sandbox, produce `ReproductionResult`
- [ ] Wire into existing pipeline: reproduction step before Fix Agent
- [ ] Flaky detection: if sandbox passes, post "looks flaky" comment, skip fix
- [ ] Dep layer caching per repo
- [ ] ≥80% test coverage on new agents

### Phase 3 — Verifier + Full E2E (sprint after)
**Goal:** The pipeline can confirm the app works, not just that linting passes.

- [ ] `VerifierAgent` — run test suite in sandbox post-fix
- [ ] Sandbox network isolation + resource limits
- [ ] Unknown stack LLM fallback path
- [ ] Base branch health check before starting fix
- [ ] Full pipeline integration test (real repo, real sandbox, real CI)
- [ ] **MESMD proof:** trigger real CI failures, confirm pipeline fixes them all, CI stays green

---

## 10. Success Criteria

Phase 1 done when:
- No duplicate fix PRs ever opened for the same branch
- `CIFixContext` fully populated and queryable via API
- 80% unit test coverage across all ci_fixer modules

Phase 2 done when:
- Reproducer Agent correctly classifies confirmed vs flaky vs env_mismatch
- Flaky failures generate a comment instead of a bad fix PR
- Sandbox spins up in <5 seconds (from pre-warmed image)

Phase 3 done when:
- MESMD app: zero open CI failures after pipeline runs end-to-end
- Verifier Agent confirms app smoke tests pass post-fix
- Full pipeline runs in <3 minutes for a lint failure

---

## 11. What We Are NOT Building

- Auto-merge — fix PRs are always draft, always human-approved before merge
- Fix for logic bugs — if Root Cause Agent classifies it as a test regression the engineer introduced, it escalates, it does not fix
- Multi-repo coordination — one pipeline per repo, no cross-repo fixes
- Jenkins support — Phase 2 at earliest
