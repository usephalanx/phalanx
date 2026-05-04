"""CI Fixer v3 — Tech Lead agent (investigator).

Phase 1 implementation. GPT-5.4 with read-only diagnosis tools.

Role:
  - Reads the parent Run's ci_context (failing command, job name, repo, PR).
  - Uses GPT-5.4 to diagnose root cause. Tools are read-only — it can:
      fetch_ci_log, get_pr_context, get_pr_diff, get_ci_history, git_blame,
      query_fingerprint, read_file, glob, grep.
  - Does NOT write code. Does NOT run sandbox. Does NOT commit.
  - Final turn emits a JSON fix_spec block; we parse it and write to
    Task.output so the Engineer (next task in the DAG) can read it.

Output shape (written to tasks.output):
  {
    "root_cause": str,              # one-sentence diagnosis
    "affected_files": [str],        # repo-relative paths to edit
    "fix_spec": str,                # natural-language change description
    "confidence": float,            # 0.0 .. 1.0
    "open_questions": [str],        # unknowns left for the Engineer
    "model": "gpt-5.4",
    "turns_used": int,
    "tool_calls_used": int,
  }

Invariants:
  - Single Celery task invocation; no internal retry loop beyond max_turns.
  - Reuses v2 tool implementations + providers. No copy-paste.
  - Zero changes to v2 or build flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path

import structlog
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.settings import get_settings
from phalanx.db.models import CIIntegration, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


# Tool subset visible to the Tech Lead. Strict subset of v2's MAIN_AGENT_TOOL_NAMES
# — read-only + diagnosis. Anything NOT listed here is unreachable via the LLM.
_TECHLEAD_TOOLS: tuple[str, ...] = (
    "fetch_ci_log",
    "get_pr_context",
    "get_pr_diff",
    "get_ci_history",
    "git_blame",
    "query_fingerprint",
    "read_file",
    "glob",
    "grep",
    "validate_self_critique",  # v1.6.0 Phase 1: REQUIRED before emit_fix_spec
)

# Side-effect import: register validate_self_critique with the v2 tool
# registry so the TL loop can dispatch it. Module-level so it's done once
# at import time (mirrors v2 tool registration pattern).
from phalanx.agents import _tl_self_critique  # noqa: F401, E402

_MAX_TURNS = 8
_MAX_TOOL_CALLS = 15  # hard upper bound across all turns

_SYSTEM_PROMPT = """You are a Senior Tech Lead investigating a failing CI build.

You have READ-ONLY tools. You do NOT write code. You do NOT run sandbox commands.
You do NOT commit. Your only job is to produce a precise fix specification that
a different engineer will implement in the next step.

Pre-dispatch probes (v1.7 — ALWAYS read first when present):
  Before you start investigating, the commander has already run two
  deterministic probes against the repo and attached results to your
  initial message under "=== Git history matches ===" and "=== Recent
  infra commits ===". These are evidence-grounded signals that often
  identify the bug class before any LLM reasoning:

    - Git history matches: prior commits whose diffs contain your error
      tokens. A strong match here often means a fix already exists for
      this error class — review the prior fix's diff before re-deriving.

    - Recent infra commits: changes to .github/, Dockerfile, requirements,
      pyproject in the last 30d. If the failure first appeared after one
      of these, the diagnosis is likely env_drift, NOT a code bug. In that
      case set review_decision="ESCALATE", confidence=0.0, affected_files=[],
      and reference the infra commit in open_questions.

  Treat these probe results as senior-engineer-level evidence already
  surfaced for you. Do NOT re-derive what they tell you; build on it.

Workflow you MUST follow:
  1. Read the probe results in your initial message (above).
  2. Call `fetch_ci_log` with the provided job_id to see the actual failure.
  3. Use `get_pr_diff` to see what this PR changed.
  4. Read the affected file(s) with `read_file` or confirm authorship with
     `git_blame`. Use `get_ci_history` / `query_fingerprint` only if you
     suspect a known recurring failure.
  5. Do NOT loop — each tool should only be called once unless new
     information requires a follow-up read.

When you have enough evidence, end your turn with a single markdown fenced
`json` code block containing EXACTLY this shape:

```json
{
  "root_cause": "one-sentence diagnosis of why CI failed",
  "error_line_quote": "verbatim line from ci_log_text containing the actual failure (20-240 chars)",
  "affected_files": ["repo-relative/path.py"],
  "fix_spec": "natural-language description of the minimum edit required",
  "failing_command": "exact shell command that was failing in CI",
  "verify_command": "exact shell command engineer runs after applying the patch to confirm success",
  "verify_success": {
    "exit_codes": [0],
    "stdout_contains": null,
    "stderr_excludes": null
  },
  "confidence": 0.0,
  "open_questions": ["any unknowns the engineer should be aware of"],
  "self_critique": {
    "ci_log_addresses_root_cause": true,
    "affected_files_exist_in_repo": true,
    "verify_command_will_distinguish_success": true,
    "grounding_satisfied": true,
    "step_preconditions_satisfied": true,
    "error_line_quoted_from_log": true,
    "notes": "one-sentence senior-engineer-style sanity check"
  }
}
```

`failing_command` is what was failing in CI. `verify_command` is what the
engineer runs after the patch. Pick both NARROW: the smallest command
that re-runs only the failed check.

  NEVER use wrapper commands as failing_command/verify_command:
    prek, pre-commit, lefthook, husky, make, tox, nox, hatch,
    npm test, yarn test, pnpm test, turbo, nx, gradlew, sbt.
  These bundle multiple checks; a sibling failure unrelated to your
  fix will make the gate red even when your patch is correct.

  Use VERBATIM when CI's command IS narrow:
    pytest tests/test_x.py::test_y -xvs
    ruff check src/foo.py
    mvn -B test -Dtest=Foo#bar

  Rule: if CI shows "pytest collected 300, only test_login failed",
  emit `pytest path::test_login`, NOT the full pytest invocation.

verify_command rules (v1.5.0 contract):
  DEFAULT (most fixes)        verify_command = failing_command, exit_codes [0]
  FIX DELETES A TEST          broaden to parent dir/module (avoids exit 4 trap)
  FIX RENAMES/MOVES           target the NEW location
  FIX IS CONFIG-ONLY          exercise the config path, e.g. python -c '...'
  FIX BUMPS A LIB VERSION     `python -c 'import X; print(X.__version__)'`
                               + verify_success.stdout_contains the version

verify_success: exit_codes [0] by default. Only add non-zero codes when
your fix's intent justifies it (delete-test fix → [0,4,5]). stdout_contains
and stderr_excludes are optional substring matchers; use sparingly.

How to fill `self_critique` — tool-validated, not declared.

  Before emitting fix_spec, you MUST call `validate_self_critique` with:
    draft_root_cause, draft_affected_files, draft_verify_command,
    ci_log_text (verbatim from earlier fetch_ci_log),
    draft_steps (the FLAT list of step dicts across ALL engineer tasks
                 in your task_plan; pass [] if no engineer task yet),
    draft_error_line_quote (the candidate line for fix_spec.error_line_quote)

  The tool returns AUTHORITATIVE booleans; use them verbatim.
  Commander re-runs the same checks against your final fix_spec; mismatch
  → TL task FAILED + re-dispatch.

  Six checks:

    c1 ci_log_addresses_root_cause: distinctive ≥4-char tokens from
        root_cause must overlap with ci_log (≥1 hit AND ≥30% coverage).
        Catches fabricated diagnoses.

    c2 affected_files_exist_in_repo: every path in affected_files must
        resolve to a real file. No path traversal, no absolute paths.

    c3 verify_command_will_distinguish_success: first shell token of
        verify_command must resolve via `command -v` in the sandbox.

    c4 grounding_satisfied (v1.7): for every step in your task_plan that
        modifies a file (replace/insert/delete_lines), you MUST have
        called read_file on that file THIS turn. Catches patch
        hallucination — TL emitting steps for files it never opened.

    c5 step_preconditions_satisfied (v1.7): for every `replace` step,
        the `old` substring must literally exist in the target file's
        current content. Catches stale/typo'd OLD text.

    c7 error_line_quoted_from_log (v1.7): error_line_quote must be a
        verbatim substring of ci_log_text, length 20-240 chars.
        Forces diagnosis to anchor in real log evidence, not paraphrase.

    c8 test_behavior_preserved (v1.7.2.5): for flake/timing/random/sleep
        /timeout/nondeterministic failures, plan must NOT delete tests,
        skip tests, or remove coverage. The right fix is to make the
        test deterministic. See "FLAKE / TIMING FAILURES" section below.

  If ANY validator boolean is false:
    - Fix the underlying issue (read the file you missed; grep the
      actual `old` text; pick a real error line; re-token root_cause).
    - Re-call validate_self_critique with the corrected draft.
    - After 2 iterations max, if you still cannot reach all-true,
      emit fix_spec with `confidence: 0.4` and the failing checks
      documented in `open_questions`.

  notes: one-line senior-engineer-style review of your own fix_spec.

v1.7 contract additions — task_plan + env_requirements (NEW):

After diagnosing, you MUST emit a `task_plan` describing how the rest of
the run will execute. The commander uses this to build the DAG.

Available downstream agents:
  cifix_sre_setup   — provisions sandbox env (pip installs, services)
  cifix_engineer    — applies the fix via deterministic step actions
                      (NO LLM judgment in v1.7 — engineer is a typist)
  cifix_sre_verify  — runs verify_command, reports pass/fail

Each task_plan entry MUST have:
  task_id     — "T2", "T3", ... (you are T1; entries are sequential)
  agent       — one of the three above (lowercase, exact)
  depends_on  — list of task_ids that finish first; [] for the first
  purpose     — one-line human-readable summary
  steps       — REQUIRED for cifix_engineer + cifix_sre_verify
  env_requirements — REQUIRED for cifix_sre_setup; SAME SCHEMA as the
                     top-level env_requirements

Step actions:
  read           — {"action":"read","file":"<path>"}
  replace        — {"action":"replace","file":"<path>","old":"<exact text>","new":"<text>"}
  insert         — {"action":"insert","file":"<path>","after_line":<int>,"content":"<text>"}
  delete_lines   — {"action":"delete_lines","file":"<path>","line":<int>,"end_line":<int>}
  apply_diff     — {"action":"apply_diff","diff":"<unified diff text>"}
                   CRITICAL: diff text MUST be valid input to `git apply`.
                   Hunk headers MUST be `@@ -<start>[,<count>] +<start>[,<count>] @@`
                   with explicit line numbers. Empty `@@` markers are NOT
                   accepted. Each diff MUST also have `--- a/<path>` and
                   `+++ b/<path>` file headers above its first hunk.
                   PREFER `replace`/`insert` over `apply_diff` when adding
                   ≤5 hunks; reserve `apply_diff` for new-file creation
                   or large-scale rewrites. The plan validator rejects
                   malformed diffs and forces re-plan.
  run            — {"action":"run","command":"<shell>","expect_exit":<int>,"expect_stdout_contains":"<str>"}
  commit         — {"action":"commit","message":"<commit msg>"}
  push           — {"action":"push"}

Plan structural rules (commander rejects malformed plans):
  - Last task in plan MUST be cifix_sre_verify
  - No cycles in depends_on graph
  - Engineer tasks always end with commit + push

WHEN to include cifix_sre_setup:

  REQUIRED (emit cifix_sre_setup) whenever ANY of these is true:
    - Failure is ModuleNotFoundError / ImportError on a third-party pkg
    - Failure mentions a missing system tool (uv, node, mvn, etc.) and
      we are NOT escalating
    - Fix introduces a new pip dep (even if you're also adding it to
      pyproject.toml — sandbox needs it BEFORE verify runs, since
      `pip install -e .` runs as a separate setup step)
    - Fix needs a service (postgres/redis/mysql) the sandbox doesn't
      already have
    - Fix needs a specific Python version different from the sandbox default
    - verify_command invokes a test runner (pytest, unittest, jest,
      vitest, cargo test, mvn test, etc.) — even if the runner is
      already in pyproject's dev dependencies. The sandbox provisioner
      can't be assumed to install [project.optional-dependencies] —
      make it explicit.

  SKIP cifix_sre_setup ONLY when ALL of these hold:
    - Failure is a pure lint/format issue (ruff, black, isort, eslint
      with no test runner step)
    - Verify is a single non-test-runner command and its first token
      (e.g., `ruff`, `mypy`) is genuinely available in the sandbox
    - No new system tools or services needed

  When in doubt — INCLUDE cifix_sre_setup. Skipping it when you needed
  it makes engineer's narrow_verify fail with confusing dep errors;
  including it when you didn't is a small wasted step.

When to use replace vs apply_diff:
  - 1-2 line edits, exact text known          → replace (safer; clearer)
  - Insert a new function / test block         → insert
  - Multi-site rewrite, > 5 hunks               → apply_diff
  - Creating a NEW file                        → apply_diff with
                                                  `--- /dev/null` header
  - DELETING a file                            → apply_diff with
                                                  "deleted file mode"

  HARD RULE (plan validator enforces): apply_diff with ≤ 5 hunks is
  REJECTED unless the diff is a new-file creation (`--- /dev/null`) OR
  a file deletion (`+++ /dev/null`). For ≤ 5 targeted edits, use
  `replace` / `insert` — they have a much higher success rate than
  the LLM-emitted unified-diff path.

PLAN COMPLETENESS — every emitted task_plan MUST satisfy:
  - affected_files non-empty IFF any engineer step modifies a file
    (mismatch — empty list with file edits OR populated list with
    no edits — is rejected)
  - every cifix_engineer task contains at least one patch step
    (replace / insert / delete_lines / apply_diff) BEFORE its commit
  - verify_command directly tests the failure (typically a substring
    of failing_command, narrowed to the failing target)

BEHAVIORAL PRESERVATION — answer these THREE questions before emit:

  Q1. Does this patch preserve the intent of the failing test?
  Q2. Am I hiding the failure or actually fixing the behavior?
  Q3. If I remove or weaken a test, can I prove the behavior is still
      validated by another test?

  If you cannot answer all three with a confident YES, do NOT ship a
  test-deleting / test-skipping / assertion-removing plan. Either find
  a deterministic source-fix OR ESCALATE.

STRATEGY DECISION TREE — pick ONE branch based on failure shape:

  Flake / timing / random / sleep / timeout / nondeterministic:
    → Fix the source of nondeterminism (seed, mock, freeze-time,
      remove unnecessary sleep). NEVER delete or skip the test.
    → See "FLAKE / TIMING FAILURES" block below.

  Coverage drop (--cov-fail-under, missing lines):
    → Add tests via `replace` / `insert` to bring coverage back.
    → Use apply_diff ONLY if the additions span > 5 hunks.

  Syntax error / import error:
    → Minimal one-line fix (replace), nothing more. Do NOT refactor.

  Assertion failure (production code is wrong):
    → Edit the source; preserve all existing assertions.

  CI config drift / sandbox env mismatch:
    → ESCALATE. Do NOT edit `.github/workflows/`, `tox.ini`,
      `pre-commit-config.yaml`, etc. (patch_safety would block anyway.)

REPLAN MODE (iteration > 1) — additional requirements:

  When commander dispatches you on iter ≥ 2, ci_context will include:

    - prior_failure_fingerprint  — 16-char hash of the prior verify
                                   failure (cmd + exit_code + normalized
                                   output). Reference this in your
                                   replan_reason.
    - prior_task_plan            — the FULL task_plan from your prior
                                   TL emit. Compare its structural
                                   signature (action types + files) to
                                   what you're about to emit; if you
                                   propose the same shape, the plan
                                   validator rejects you.
    - prior_verify_command       — the verify_command you set last time.
                                   It's almost certainly fine; what
                                   failed is the patch shape, not the
                                   verify scope.
    - prior_replan_reason        — present from iter ≥ 3. The reason
                                   you cited in the previous replan.
                                   Don't repeat verbatim; explain
                                   what's NEW.
    - prior_sre_failures[].stdout_tail / stderr_tail
                                  — the actual output ruff/pytest/etc.
                                   produced. Read these to understand
                                   what the engineer's commit actually
                                   broke.

  You MUST:

    - Set `replan_reason` (string field on fix_spec) explaining why
      the prior strategy failed and what's different about this attempt.
      Reference prior_failure_fingerprint or quote a specific line
      from the prior verify output. Empty / generic replan_reason →
      plan validator rejects.

    - Choose a DIFFERENT strategy than prior_task_plan. The plan
      validator computes a structural signature (ordered (action,
      file) tuples across engineer tasks) and rejects if your new
      plan's signature matches the prior's byte-for-byte. Different
      action types, different files, or a pivot to a different fix
      shape — any of these counts as "different strategy."

env_requirements (top-level AND mirrored in cifix_sre_setup task):
  python              — Python version, e.g., "3.11"
  python_packages     — pip packages, e.g., ["httpx", "pytest>=7"]
  os_packages         — apt/brew packages (rare)
  env_vars            — {"NAME": "value"} pairs
  services            — subset of {"postgres", "redis", "mysql"}
  reproduce_command   — REQUIRED. Command SRE runs to confirm env reproduces failure
  reproduce_expected  — REQUIRED. Human-readable expected outcome BEFORE fix

FLAKE / TIMING FAILURES — preserve behavioral intent, do NOT delete:

  Triggered by signals in root_cause OR ci_log: flake, flaky, timeout,
  timing, race, nondeterministic, intermittent, sleep, random, jitter.

  DO:
    - Fix the source of nondeterminism (the test was right, the
      environment is the problem).
    - Seed randomness deterministically: random.seed(42), numpy seed,
      pytest-randomly seed, faker seed.
    - Remove unnecessary sleeps; use deterministic synchronization
      primitives (events, locks, mocks, freezegun).
    - Loosen a timing assertion ONLY when the behavioral coverage is
      preserved — e.g. assert duration < 5.0 instead of < 0.1 if the
      test still demonstrates "operation completes in bounded time."
    - Replace time.time() / datetime.now() with frozen-time fixtures.
    - Mock the source of variance (e.g. patch `time.sleep`, `random.random`).

  DO NOT:
    - Delete the test (`delete_lines` on a tests/ path).
    - Skip the test (@pytest.mark.skip, @pytest.mark.skipif,
      @pytest.mark.xfail, @unittest.skip, pytestmark=pytest.mark.skip,
      `pytest.skip(...)` inline).
    - Remove the test function in apply_diff (lines starting with `-def
      test_...` in the diff body).
    - Bump the timeout to dodge it (--timeout=999).
    - Reduce coverage thresholds to avoid the failure.

  If after investigation you cannot identify a SAFE deterministic fix,
  ESCALATE rather than ship a deletion or skip:
    review_decision = "ESCALATE"
    confidence = 0.0
    affected_files = []
    open_questions = ["this test is flaky because <root cause>; we
      could not find a deterministic fix without changing behavioral
      coverage. Suggest <human action>."]

  c8 self-critique enforces this. The plan validator and engineer's
  patch_safety guards will both refuse a deletion-shaped fix anyway —
  ESCALATING up front is faster than burning iterations getting
  bounced.

ESCALATE path (env-mismatch — sandbox lacks tooling, NOT a code bug):
  When you determine the failure is environmental (sandbox lacks `uv`,
  `node`, `mvn`, etc.) and the maintainer's CI is correct:
    - Set review_decision: "ESCALATE"
    - Set confidence: 0.0
    - Set affected_files: []
    - Put the env gap in open_questions
    - task_plan still REQUIRED (commander needs structural validity);
      emit a single no-op cifix_sre_verify task

Examples (illustrative — adapt to actual bug):

LINT FIX (no SRE setup; replace + commit + push + verify):
{
  "task_plan": [
    {"task_id":"T2","agent":"cifix_engineer","depends_on":[],"purpose":"wrap line",
     "steps":[
       {"id":1,"action":"replace","file":"src/x.py","old":"long line...","new":"short\\nline"},
       {"id":2,"action":"commit","message":"fix(lint): wrap line"},
       {"id":3,"action":"push"}
     ]},
    {"task_id":"T3","agent":"cifix_sre_verify","depends_on":["T2"],"purpose":"verify",
     "steps":[{"id":1,"action":"run","command":"ruff check src/x.py","expect_exit":0}]}
  ]
}

NEW DEP FIX (sre_setup + engineer + verify):
{
  "env_requirements": {
    "python":"3.11",
    "python_packages":["httpx","pytest>=7"],
    "reproduce_command":"python -m pytest tests/ -q",
    "reproduce_expected":"fails with ModuleNotFoundError: No module named 'httpx'"
  },
  "task_plan": [
    {"task_id":"T2","agent":"cifix_sre_setup","depends_on":[],
     "purpose":"install httpx","env_requirements":{... same shape ...}},
    {"task_id":"T3","agent":"cifix_engineer","depends_on":["T2"],
     "purpose":"add dep to pyproject","steps":[...]},
    {"task_id":"T4","agent":"cifix_sre_verify","depends_on":["T3"],
     "purpose":"verify","steps":[...]}
  ]
}

ESCALATE (env-mismatch — DON'T edit the customer's CI config):
{
  "review_decision":"ESCALATE",
  "confidence":0.0,
  "affected_files":[],
  "open_questions":["sandbox lacks uv; upstream uses astral-sh/setup-uv@v3"],
  "task_plan":[{"task_id":"T2","agent":"cifix_sre_verify","depends_on":[],
                "purpose":"no-op (escalated)",
                "steps":[{"id":1,"action":"run","command":"echo escalated","expect_exit":0}]}]
}

CRITICAL: Your final turn must contain the fenced ```json``` block. You
MAY include a short prose summary before it. You MUST NOT omit any of the
six required keys (root_cause, affected_files, fix_spec, failing_command,
confidence, open_questions) — every key must be present even when the
value is an empty list or an empty string. The four v1.5.0 OPTIONAL keys
(verify_command, verify_success, self_critique) are STRONGLY ENCOURAGED
but a missing one falls back to the v1.4.x default (verify_command =
failing_command, verify_success = {exit_codes:[0]}, self_critique
omitted). The v1.7 keys (task_plan REQUIRED, env_requirements,
review_decision) follow the rules above. A missing REQUIRED key causes
an investigation failure and the run is escalated.

Confidence 0.0-1.0. Be honest — if the fix is unclear, confidence < 0.5 and
list open_questions. The engineer will escalate rather than guess.

NEVER patch CI infrastructure to make CI green:
  Files under `.github/workflows/`, `tox.ini`, `noxfile.py`,
  `pre-commit-config.yaml`, `Makefile`, `package.json` scripts, etc. are
  the repo maintainers' choices. If the agent's sandbox cannot run the
  CI exactly as the maintainers configured it (e.g., upstream uses `uv`
  but our sandbox doesn't ship `uv`; upstream uses Node hooks but
  `libatomic.so.1` is missing), this is a SANDBOX ENV MISMATCH — the
  fix lives in our infrastructure, NOT the customer's repo.

  When you see this:
    - Set `confidence` to 0.0
    - Put the env mismatch in `open_questions` (e.g.
      "sandbox lacks uv; upstream CI requires it for tox")
    - Set `affected_files` to [] and `fix_spec` to a short note explaining
      that the code itself appears fine; the failure is environmental
    - Do NOT add CI files to `affected_files`. Editing them ships a
      regression from the maintainers' point of view and will be rejected.

  This is the difference between fixing a bug and rewriting CI to dodge it.
  We always do the former; the latter is escalated to humans.
"""


@celery_app.task(
    name="phalanx.agents.cifix_techlead.execute_task",
    bind=True,
    queue="cifix_techlead",
    max_retries=1,
    soft_time_limit=600,
    time_limit=720,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:  # pragma: no cover
    from phalanx.ci_fixer_v3.task_lifecycle import persist_task_completion  # noqa: PLC0415

    agent = CIFixTechLeadAgent(run_id=run_id, agent_id="cifix_techlead", task_id=task_id)
    result = asyncio.run(agent.execute())
    asyncio.run(persist_task_completion(task_id, result))
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixTechLeadAgent(BaseAgent):
    AGENT_ROLE = "cifix_techlead"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_techlead.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            # Tech Lead reads ci_context from its own Task.description (seeded by
            # cifix_commander when persisting the DAG).
            ci_context = _parse_ci_context(task.description)
            integration = await self._load_integration(session, ci_context.get("repo"))
            # Try to inherit the workspace from the upstream sre_setup task.
            # In v3 flow this is always present; if we can't find it, we fall
            # back to cloning (preserves simulate-path + any edge case where
            # TechLead is invoked outside the standard DAG).
            sre_setup = await self._load_sre_setup_output(session)

        # Missing must-have fields → fast fail
        missing = _missing_required(ci_context)
        if missing:
            err = f"ci_context missing required fields: {missing}"
            self._log.error("cifix_techlead.bad_context", missing=missing)
            return AgentResult(success=False, output={}, error=err)

        # Workspace: inherit from sre_setup when available; otherwise clone ourselves.
        if sre_setup and sre_setup.get("workspace_path"):
            workspace_path = sre_setup["workspace_path"]
            self._log.info(
                "cifix_techlead.inherited_workspace",
                workspace=workspace_path,
                from_sre_task=True,
            )
        else:
            try:
                workspace_path = await _clone_workspace(
                    run_id=self.run_id,
                    repo_full_name=ci_context["repo"],
                    branch=ci_context["branch"],
                    github_token=_resolve_github_token(integration),
                )
                self._log.info(
                    "cifix_techlead.cloned_workspace_fallback",
                    workspace=workspace_path,
                    reason="no sre_setup upstream output found",
                )
            except Exception as exc:
                self._log.exception("cifix_techlead.clone_failed", error=str(exc))
                return AgentResult(success=False, output={}, error=f"workspace clone failed: {exc}")

        # Build an AgentContext reused from v2 — Tech Lead's tools don't need sandbox.
        ctx = _build_techlead_context(
            run_id=self.run_id,
            ci_context=ci_context,
            workspace_path=workspace_path,
            integration=integration,
        )

        # Build GPT-5.4 LLM callable with TL-only tool schemas.
        llm_call = _build_techlead_llm(tool_names=_TECHLEAD_TOOLS)

        # v1.7 Tier 1 — pre-dispatch probes. Cheap deterministic git
        # queries that surface evidence TL would otherwise have to find
        # via N tool calls. See phalanx/agents/_v17_probes.py.
        probe_block = ""
        try:
            from phalanx.agents._v17_probes import run_pre_tl_probes  # noqa: PLC0415

            probes = run_pre_tl_probes(
                failing_command=ci_context.get("failing_command", ""),
                error_line_or_log=ci_context.get("failing_command", ""),
                workspace_path=workspace_path,
            )
            probe_block = probes.render_for_tl()
            self._log.info(
                "cifix_techlead.probes",
                git_hits=len(probes.git_log_hits),
                env_drift=len(probes.env_drift_hits),
                tokens=probes.error_tokens_searched,
            )
        except Exception as exc:  # noqa: BLE001 — probes are best-effort
            self._log.warning("cifix_techlead.probes_failed", error=str(exc))
            probe_block = ""

        # Seed the first user message with the normalized CI context +
        # probe results (if any).
        initial_message = _build_initial_message(ci_context)
        if probe_block:
            initial_message = probe_block + "\n\n" + initial_message
        ctx.messages.append({"role": "user", "content": initial_message})

        # Run the investigation loop.
        try:
            fix_spec, turns_used, tool_calls_used = await _run_investigation_loop(
                ctx=ctx,
                llm_call=llm_call,
                max_turns=_MAX_TURNS,
                max_tool_calls=_MAX_TOOL_CALLS,
                logger=self._log,
            )
        except _InvestigationError as exc:
            return AgentResult(
                success=False,
                output={"error_class": exc.kind, "detail": exc.detail},
                error=f"{exc.kind}: {exc.detail}",
                tokens_used=ctx.cost.total_tokens if hasattr(ctx.cost, "total_tokens") else 0,
            )

        # v1.6.0 Phase 1 + v1.7 — self-critique gate. The LLM was prompted
        # to call validate_self_critique and place its output in
        # fix_spec.self_critique. Here we verify what was actually emitted
        # has all-true booleans. v1.7 adds c4/c5/c7 keys; treat absent
        # v1.7 keys as "not asserted" rather than False so v1.6 callers
        # still flow through cleanly.
        sc = fix_spec.get("self_critique")
        if isinstance(sc, dict):
            booleans = {
                "ci_log_addresses_root_cause": sc.get("ci_log_addresses_root_cause"),
                "affected_files_exist_in_repo": sc.get("affected_files_exist_in_repo"),
                "verify_command_will_distinguish_success": sc.get(
                    "verify_command_will_distinguish_success"
                ),
            }
            # v1.7 keys: only check when present (backwards compat with v1.6 emits)
            # v1.7.2.5: + test_behavior_preserved (c8) — flake-strategy guard
            for v17_key in (
                "grounding_satisfied",
                "step_preconditions_satisfied",
                "error_line_quoted_from_log",
                "test_behavior_preserved",
            ):
                if v17_key in sc:
                    booleans[v17_key] = sc.get(v17_key)
            failing = [k for k, v in booleans.items() if v is not True]
            if failing:
                # If TL flagged ITSELF as low-confidence (≤0.5), allow it
                # through with the failing self_critique — engineer's
                # confidence guard then triggers low_confidence skip cleanly.
                # Otherwise: a high-confidence claim with failing self_critique
                # is an inconsistency we reject.
                conf = float(fix_spec.get("confidence") or 0.0)
                if conf > 0.5:
                    self._log.warning(
                        "cifix_techlead.self_critique_inconsistent",
                        failing_checks=failing,
                        confidence=conf,
                    )
                    return AgentResult(
                        success=False,
                        output={
                            "error_class": "self_critique_inconsistent",
                            "failing_checks": failing,
                            "confidence": conf,
                            **fix_spec,
                        },
                        error=(
                            f"self_critique_inconsistent: confidence={conf:.2f} but "
                            f"checks failing={failing}. Re-investigate or lower confidence."
                        ),
                        tokens_used=_tokens_used_from_ctx(ctx),
                    )
                # Low-confidence path is allowed — engineer's guard will skip cleanly.
                self._log.info(
                    "cifix_techlead.self_critique_low_confidence_skip",
                    failing_checks=failing,
                    confidence=conf,
                )

        # v1.7.2.5 — plan validator gate (structural).
        # v1.7.2.7 — completeness + REPLAN strategy-change checks.
        # All catch problems BEFORE engineer dispatches; failed
        # validation marks the TL task FAILED so commander re-dispatches
        # without burning an engineer iteration.
        plan = fix_spec.get("task_plan")
        if isinstance(plan, list) and plan:
            from phalanx.agents._plan_validator import (  # noqa: PLC0415
                PlanValidationError,
                validate_plan,
                validate_plan_completeness,
                validate_replan_strategy,
            )
            try:
                # 1. Structural validation (existing v1.7.2.5)
                validate_plan(plan)

                # 2. Completeness — affected_files mismatch / missing
                #    patch steps (v1.7.2.7)
                validate_plan_completeness(
                    plan,
                    affected_files=fix_spec.get("affected_files") or [],
                )

                # 3. REPLAN strategy-change (v1.7.2.7) — only fires when
                #    commander injected ci_context.prior_failure_fingerprint
                #    and ci_context.prior_task_plan. Iteration > 1 implies
                #    we've already failed once.
                iteration = int(ci_context.get("iteration") or 1)
                prior_fp = ci_context.get("prior_failure_fingerprint")
                prior_plan = ci_context.get("prior_task_plan")
                if iteration > 1 or prior_fp:
                    validate_replan_strategy(
                        current_plan=plan,
                        prior_plan=prior_plan if isinstance(prior_plan, list) else None,
                        iteration=iteration,
                        fix_spec_replan_reason=fix_spec.get("replan_reason"),
                    )
            except PlanValidationError as exc:
                self._log.warning(
                    "cifix_techlead.plan_validation_failed",
                    error=str(exc),
                    confidence=fix_spec.get("confidence"),
                )
                return AgentResult(
                    success=False,
                    output={
                        "error_class": "plan_validation_failed",
                        "validation_error": str(exc),
                        **fix_spec,
                    },
                    error=f"plan_validation_failed: {exc}",
                    tokens_used=_tokens_used_from_ctx(ctx),
                )

        self._log.info(
            "cifix_techlead.done",
            confidence=fix_spec.get("confidence"),
            affected_files=fix_spec.get("affected_files"),
            turns=turns_used,
            tool_calls=tool_calls_used,
        )
        return AgentResult(
            success=True,
            output={
                **fix_spec,
                "model": "gpt-5.4",
                "turns_used": turns_used,
                "tool_calls_used": tool_calls_used,
            },
            tokens_used=_tokens_used_from_ctx(ctx),
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_integration(self, session, repo: str | None) -> CIIntegration | None:
        if not repo:
            return None
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == repo)
        )
        return result.scalar_one_or_none()

    async def _load_sre_setup_output(self, session) -> dict | None:
        """Find the earliest COMPLETED cifix_sre task with mode='setup' in this run.

        sre_setup is seq=1 in the v3 DAG; we pick the FIRST one (by seq) so
        iteration 2+ still reads iteration 1's setup (we reuse the container
        across iterations, intentionally — see commander._append_iteration_dag).
        """
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role.in_(
                    ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                ),
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.asc())
        )
        for (output,) in result.all():
            if isinstance(output, dict) and output.get("mode") == "setup":
                return output
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Investigation loop — self-contained, reuses v2 tool dispatch + providers.
# Kept as module-level functions (not methods) so unit tests can inject fakes.
# ─────────────────────────────────────────────────────────────────────────────


class _InvestigationError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


async def _run_investigation_loop(
    ctx,
    llm_call,
    max_turns: int,
    max_tool_calls: int,
    logger,
) -> tuple[dict, int, int]:
    """Core loop: drive the LLM until it emits a fix_spec JSON block."""
    # Lazy imports so the module loads without heavy deps until actually invoked.
    from phalanx.ci_fixer_v2.tools import base as tools_base  # noqa: PLC0415

    total_tool_calls = 0
    for turn in range(max_turns):
        logger.info("cifix_techlead.turn_start", turn=turn, messages=len(ctx.messages))
        response = await llm_call(ctx.messages)
        logger.info(
            "cifix_techlead.turn_response",
            turn=turn,
            stop_reason=response.stop_reason,
            tools=[u.name for u in response.tool_uses] if response.tool_uses else [],
        )

        # Record the assistant's turn in the history for the next round.
        from phalanx.ci_fixer_v2.agent import _assistant_message_content  # noqa: PLC0415

        ctx.messages.append({"role": "assistant", "content": _assistant_message_content(response)})

        if response.stop_reason == "end_turn" and not response.tool_uses:
            # Model thinks it's done. Parse the text for a JSON fix_spec.
            fix_spec = _parse_fix_spec_from_text(response.text or "")
            if fix_spec is None:
                # Surface the raw text in the failure reason so we can
                # diagnose prompt-vs-model mismatches without needing a
                # separate trace table. Truncated to 800 chars.
                text_tail = (response.text or "")[:800]
                logger.warning(
                    "cifix_techlead.fix_spec_parse_failed",
                    turn=turn,
                    text_len=len(response.text or ""),
                    text_preview=text_tail,
                )
                raise _InvestigationError(
                    "no_fix_spec_emitted",
                    f"LLM stopped without a valid JSON fix_spec block. "
                    f"Raw text (up to 800 chars): {text_tail!r}",
                )
            return (fix_spec, turn + 1, total_tool_calls)

        # Dispatch each tool_use; append tool_result messages for the next turn.
        for use in response.tool_uses or []:
            total_tool_calls += 1
            if total_tool_calls > max_tool_calls:
                raise _InvestigationError(
                    "tool_call_cap",
                    f"Tech Lead exceeded {max_tool_calls} tool calls without a fix_spec",
                )
            if use.name not in _TECHLEAD_TOOLS:
                # Shouldn't happen — LLM only sees TL tools — but belt and braces.
                raise _InvestigationError("forbidden_tool", f"Tech Lead tried to call {use.name!r}")
            if not tools_base.is_registered(use.name):
                raise _InvestigationError("unregistered_tool", f"Tool {use.name!r} not in registry")
            tool = tools_base.get(use.name)
            try:
                result = await tool.handler(ctx, use.input)
            except Exception as exc:
                result = tools_base.ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "cifix_techlead.tool_error",
                    tool=use.name,
                    error=str(exc),
                )
            ctx.messages.append(_tool_result_message(use.id, result))

    raise _InvestigationError(
        "turn_cap_reached",
        f"Tech Lead exhausted {max_turns} turns without a fix_spec",
    )


def _tool_result_message(tool_use_id: str, result) -> dict:
    """Tool-result message shape expected by v2's provider translators.

    OpenAI's Responses API rejects role='tool' — the supported values
    are 'assistant'/'system'/'developer'/'user'. Tool results are
    nested under a user message as a content block with type='tool_result'.
    This mirrors v2/agent.py:_tool_result_message exactly.
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result.to_tool_message_content(),
            }
        ],
    }


_FIX_SPEC_REQUIRED_KEYS = {
    "root_cause",
    "affected_files",
    "fix_spec",
    "failing_command",
    "confidence",
    "open_questions",
}

# v1.5.0 contract additions — verify_command + verify_success matrix.
# Kept OPTIONAL on the wire (backwards compat); engineer falls back to
# failing_command + exit_code==0 when absent. See
# docs/ci-fixer-v3-agent-contracts.md §4.
#
# v1.7 contract additions — task_plan + env_requirements + review_decision.
# These are also wire-OPTIONAL: parser accepts shapes without them so v1.6
# code paths keep working. Commander's plan validator + corpus harness
# enforce v1.7 structural correctness when present.
_FIX_SPEC_OPTIONAL_KEYS = {
    "verify_command",
    "verify_success",
    "self_critique",
    "task_plan",
    "env_requirements",
    "review_decision",
    "replan_reason",
    "error_line_quote",
}

# verify_success keys — closed schema on the matrix.
_VERIFY_SUCCESS_KEYS = {
    "exit_codes",  # list[int]; default [0]
    "stdout_contains",  # str | None
    "stderr_excludes",  # str | None
}

_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_fix_spec_from_text(text: str) -> dict | None:
    """Find a JSON fix_spec object anywhere in the model's text output.

    Tolerant parser — tries four strategies in order, accepts the LAST
    valid candidate:
      1. ```json ...``` fenced blocks (explicit)
      2. ``` ...``` unlabeled fences containing a dict
      3. bare top-level JSON (whole text is exactly a dict)
      4. embedded JSON objects found by brace-balance scanning

    If a model emits BOTH a draft and a refined block (multi-block output),
    the last-valid wins — mirrors how a human reads overlapping drafts.
    """
    if not text:
        return None

    candidates: list[dict] = []

    # 1. ```json``` fenced blocks (the explicit contract)
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            candidates.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    # 2. Unlabeled ```...``` fences (model sometimes drops the `json` tag)
    for match in _UNLABELED_FENCE_RE.finditer(text):
        try:
            candidates.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    # 3. Bare top-level JSON
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        with contextlib.suppress(json.JSONDecodeError):
            candidates.append(json.loads(stripped))

    # 4. Brace-balance scan — catches JSON embedded in mixed prose.
    candidates.extend(_scan_balanced_json_objects(text))

    # Prefer the LAST valid candidate (latest refinement wins).
    for obj in reversed(candidates):
        if not isinstance(obj, dict):
            continue
        if not _FIX_SPEC_REQUIRED_KEYS.issubset(obj.keys()):
            continue
        if not isinstance(obj.get("affected_files"), list):
            continue
        if not isinstance(obj.get("open_questions"), list):
            continue
        try:
            obj["confidence"] = float(obj["confidence"])
        except (TypeError, ValueError):
            continue

        # v1.5.0 contract — validate + normalize OPTIONAL fields. Missing
        # = backwards-compat defaults (engineer treats verify_command as
        # failing_command and verify_success.exit_codes as [0]).
        _normalize_v15_optional_fields(obj)
        return obj

    return None


def _normalize_v15_optional_fields(obj: dict) -> None:
    """In-place normalization of v1.5.0 contract additions.

    - `verify_command`: str (drop if not str)
    - `verify_success`: closed-schema dict; drop unknown keys; normalize
      exit_codes to list[int] with [0] default; stdout_contains /
      stderr_excludes must be str if present
    - `self_critique`: pass-through dict (informational; not load-bearing)

    Invalid optional fields are DROPPED rather than failing the parse —
    we'd rather lose richness than reject a fix_spec on a typo.
    """
    # verify_command — keep iff str, else drop
    vc = obj.get("verify_command")
    if vc is not None and not isinstance(vc, str):
        obj.pop("verify_command", None)

    # verify_success — closed schema
    vs = obj.get("verify_success")
    if vs is not None:
        if not isinstance(vs, dict):
            obj.pop("verify_success", None)
        else:
            cleaned: dict = {}
            ec = vs.get("exit_codes")
            if isinstance(ec, list) and all(isinstance(x, int) for x in ec):
                cleaned["exit_codes"] = ec or [0]
            elif ec is None:
                cleaned["exit_codes"] = [0]
            else:
                cleaned["exit_codes"] = [0]  # invalid → default

            for matcher in ("stdout_contains", "stderr_excludes"):
                m = vs.get(matcher)
                if isinstance(m, str) and m:
                    cleaned[matcher] = m

            obj["verify_success"] = cleaned

    # self_critique — pass-through dict, drop if not a dict
    sc = obj.get("self_critique")
    if sc is not None and not isinstance(sc, dict):
        obj.pop("self_critique", None)

    # v1.7 — task_plan: list of dicts; drop if not list or contains non-dicts
    tp = obj.get("task_plan")
    if tp is not None:
        if not isinstance(tp, list) or not all(isinstance(t, dict) for t in tp):
            obj.pop("task_plan", None)

    # v1.7 — env_requirements: dict pass-through; drop if not dict
    er = obj.get("env_requirements")
    if er is not None and not isinstance(er, dict):
        obj.pop("env_requirements", None)

    # v1.7 — review_decision: must be one of the three Literal values
    rd = obj.get("review_decision")
    if rd is not None and rd not in {"SHIP", "REPLAN", "ESCALATE"}:
        obj.pop("review_decision", None)

    # v1.7 — replan_reason: str pass-through
    rr = obj.get("replan_reason")
    if rr is not None and not isinstance(rr, str):
        obj.pop("replan_reason", None)

    # v1.7 — error_line_quote: str pass-through (validation in c7)
    elq = obj.get("error_line_quote")
    if elq is not None and not isinstance(elq, str):
        obj.pop("error_line_quote", None)


_UNLABELED_FENCE_RE = re.compile(r"```\s*(\{.*?\})\s*```", re.DOTALL)


def _scan_balanced_json_objects(text: str) -> list[dict]:
    """Find all substrings that parse as balanced JSON objects.

    Linear scan for `{`, track depth, emit substring when depth returns
    to zero. Rejects non-dict parses silently. Handles nested objects
    correctly but not strings containing braces — rare enough to ignore.
    """
    out: list[dict] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                snippet = text[start : i + 1]
                try:
                    obj = json.loads(snippet)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    return out


def _build_techlead_context(
    run_id: str,
    ci_context: dict,
    workspace_path: str,
    integration: CIIntegration | None,
):
    from phalanx.ci_fixer_v2.context import AgentContext  # noqa: PLC0415

    return AgentContext(
        ci_fix_run_id=f"v3-{run_id}",
        repo_full_name=ci_context["repo"],
        repo_workspace_path=workspace_path,
        original_failing_command=ci_context.get("failing_command", ""),
        pr_number=ci_context.get("pr_number"),
        has_write_permission=False,  # Tech Lead cannot write — enforced by tool scope
        ci_api_key=_resolve_github_token(integration),
        ci_provider=(integration.ci_provider if integration else "github_actions"),
        author_head_branch=ci_context.get("branch"),
        sandbox_container_id=None,  # Tech Lead has no sandbox — its tools don't need one
    )


def _build_techlead_llm(tool_names: tuple[str, ...]):
    # Ensure v2 tools are imported so the registry is populated.
    import phalanx.ci_fixer_v2.tools.diagnosis  # noqa: F401, PLC0415
    import phalanx.ci_fixer_v2.tools.reading  # noqa: F401, PLC0415
    from phalanx.ci_fixer_v2.providers import build_gpt_reasoning_callable  # noqa: PLC0415
    from phalanx.ci_fixer_v2.tools import base as tools_base  # noqa: PLC0415

    schemas = [tools_base.get(name).schema for name in tool_names]

    settings = get_settings()
    return build_gpt_reasoning_callable(
        model=settings.openai_model_reasoning_ci_fixer,  # "gpt-5.4" in prod
        api_key=settings.openai_api_key,
        system_prompt=_SYSTEM_PROMPT,
        tool_schemas=schemas,
        reasoning_effort="medium",
    )


async def _clone_workspace(
    run_id: str, repo_full_name: str, branch: str, github_token: str | None
) -> str:
    """Shallow clone at the PR head branch. Returns absolute workspace path."""
    if not github_token:
        raise RuntimeError("no github token available for clone")
    import git  # noqa: PLC0415

    base = Path(get_settings().git_workspace) / f"v3-{run_id}-techlead"
    if base.exists():
        import shutil  # noqa: PLC0415

        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    git.Repo.clone_from(url, base, branch=branch, depth=1)
    return str(base)


def _resolve_github_token(integration: CIIntegration | None) -> str | None:
    if integration and integration.github_token:
        return integration.github_token
    return get_settings().github_token or None


def _parse_ci_context(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _missing_required(ci_context: dict) -> list[str]:
    # `failing_command` is intentionally NOT required — GitHub webhooks do not
    # include the specific step-level command that failed. Tech Lead derives
    # it from fetch_ci_log and writes it into fix_spec.failing_command for
    # downstream consumers (Engineer uses it for sandbox verification).
    required = ("repo", "branch", "failing_job_id", "pr_number")
    return [k for k in required if not ci_context.get(k)]


def _build_initial_message(ci_context: dict) -> str:
    # Compact, structured — no markdown noise. GPT-5.4 reads this once.
    iteration = ci_context.get("iteration")
    prior = ci_context.get("prior_sre_failures") or []
    header = (
        "CI failure to investigate:\n"
        f"- repo: {ci_context.get('repo')}\n"
        f"- pr: #{ci_context.get('pr_number')} on branch {ci_context.get('branch')!r}\n"
        f"- failing_job: {ci_context.get('failing_job_name')} "
        f"(job_id={ci_context.get('failing_job_id')})\n"
        f"- failing_command: {ci_context.get('failing_command')}\n"
        f"- head_sha: {ci_context.get('sha')}\n"
    )

    # Iteration 2+ — a prior engineer pass fixed *something* but SRE's CI
    # mimicry found cascading failures. Anchor the Tech Lead on the new
    # problems, not the original one which is already green.
    if iteration and iteration > 1 and prior:
        failures_summary = "\n".join(
            f"  - job={f.get('name')!r}  exit={f.get('exit_code')}  "
            f"cmd={f.get('cmd')!r}\n    stderr_tail: {f.get('stderr_tail', '').strip()[:240]!r}"
            for f in prior[:6]
        )
        header += (
            f"\nThis is iteration #{iteration}. A prior patch resolved the original "
            "failure, but the SRE agent ran the repo's full CI in sandbox and "
            "found NEW failures the engineer must now address:\n"
            f"{failures_summary}\n\n"
            "Focus on these new failures. Do NOT re-diagnose the original "
            "failing command — it is already green."
        )

    header += (
        "\nStart with fetch_ci_log to see the raw failure context. "
        "End your turn with the JSON fix_spec block as described."
    )
    return header


def _tokens_used_from_ctx(ctx) -> int:
    """Best-effort token accounting so the framework's telemetry has a number."""
    cost = getattr(ctx, "cost", None)
    if cost is None:
        return 0
    for attr in ("total_tokens", "input_tokens", "output_tokens"):
        val = getattr(cost, attr, None)
        if isinstance(val, int):
            return val
    return 0
