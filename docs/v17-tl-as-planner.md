# CI Fixer v3 — v1.7: TL-as-planner architecture

**Status**: spec lock (2026-05-01). Build starts after this doc is approved. Same pattern that worked for v1.5 (agent contracts) and v1.6 (self-critique + reaper).

**Origin**: Phase 3 Path 1 result. v3 hit humanize's real tz-aware bug. TL diagnosed it perfectly. Engineer's coder subagent ran 2 patch attempts, hit `MAX_SUBAGENT_TURNS=10`, produced a 0-length diff. Sprint landed at 6.0/10. Bug #17 surfaced.

**The architectural decision** (Raj 2026-05-01): the engineer can't be the place LLM judgment lives. Centralize strategic thinking in TL; make ICs deterministic executors of TL-issued steps; let SRE keep its agentic env loop because real infra is genuinely messy. Reframe commander as a TPM (Jira board manager) — no LLM, no engineering judgment, just dispatch and close.

**Definition of Done — binary**: one external Python repo where v3 takes a real CI red → real CI green, fully automated, with this architecture. Not "5/5 corpus." **One.** External evidence. Push to a real repo, real GitHub CI re-runs green on v3's commit.

---

## 1. Problem

### What v1.6 has

Static DAG persisted up front by commander on every run:

```
sequence_num=1: cifix_sre        (setup)
sequence_num=2: cifix_techlead   (diagnose)
sequence_num=3: cifix_engineer   (fix)
sequence_num=4: cifix_sre        (verify)
```

Every run starts identically. Engineer is an LLM agent (Sonnet) with a coder subagent loop capped at `MAX_SUBAGENT_TURNS=10`. TL emits a fix_spec; engineer's coder figures out the actual patch.

### Why it fails

1. **SRE-setup-before-TL is wasteful.** A one-line lint fix doesn't need a Docker sandbox; today we build one anyway.
2. **Engineer carries judgment it can't sustain.** Bug #17: humanize tz fix. TL's spec was correct. Coder ran out of turns trying to invent the implementation.
3. **No one owns "is this bug *actually* fixed?"** Commander says SHIPPED when last task is green. That's how Bug #16 shipped pytest exit 4 as success.
4. **Re-planning is impossible.** Static DAG can't grow. If the engineer's fix unlocks a child bug, there's no place to add a task — run terminates whichever way it terminates.

### What we want

- TL = single source of LLM judgment. Plans, supervises, reviews, re-plans.
- Engineer = step interpreter. No invention. Apply patch, run verify, commit, push, report.
- SRE = env deliverer. Keeps its v1.4.0 agentic loop, scoped to "deliver the env TL specified."
- Commander = TPM. Dispatch by sequence_num, watch cost cap, close the run on TL's signal.

---

## 2. Roles and LLM access

| Role | LLM access | Per-task cost cap | Scope of judgment |
|---|---|---|---|
| **Commander** | None | n/a | Webhook precheck, persist tasks, dispatch, close. No engineering judgment. |
| **TL** | GPT-5.4, unbounded calls within cap | **$5** | Diagnosis, planning, code-shaping (writes the actual diff), review, re-plan, escalate. Sole strategic LLM. |
| **SRE** | Sonnet 4.6, agentic loop (keeps v1.4.0 behavior) | **$4** | Env reproduction only — install deps, fix sandbox, debug install failures. Bounded scope: deliver `env_requirements`. |
| **Engineer** | Sonnet 4.6, narrow scope | **$1** | Step interpretation — translate TL's instructions/diff into apply_patch calls; check preconditions; no invention. |

**Run-level cost cap: $25.** Worst-case realistic run (TL plan + SRE setup + Engineer + TL review + SRE delta + Engineer + TL final) tops out around $20. The $25 cap exists to catch runaway loops, not to gate normal work.

**Operating principle**: amount of LLM ∝ amount of judgment the work requires. No budget hacks; if a task needs more thinking, it's a different task.

---

## 3. Architecture

### Flow diagram

```
webhook → commander
            │
            ├─ precheck (dedup, repo enabled, version=v3)
            ├─ open Run row
            └─ persist task #1: TL plan
                │
                ▼
          ┌────TL plan mode────┐
          │ • read CI log      │
          │ • read affected    │
          │   files (ground)   │
          │ • generate diff    │
          │ • emit task_plan   │
          │ • self-critique    │
          └─────────┬──────────┘
                    │ task_plan emitted
                    ▼
            commander reads
            task_plan → persists
            tasks #2..#N
                    │
                    ▼
       ┌──── SRE setup (if needed) ────┐
       │ agentic loop, $4 cap          │
       │ delivers env_requirements     │
       └──────────────┬────────────────┘
                      ▼
           ┌── Engineer step exec ──┐
           │ apply diff             │
           │ run narrow_verify      │
           │ commit + push          │
           │ report result          │
           └────────┬───────────────┘
                    ▼
       ┌─── TL review mode ────┐
       │ all subtasks done?    │
       │ verify actually green?│
       │ new bug surfaced?     │
       └────────┬──────────────┘
                │
       ┌────────┴────────┐
       │                 │
   SHIP signal       REPLAN signal
       │                 │
       ▼                 ▼
   commander         TL replan mode → emit
   marks Run         delta task_plan → commander
   SHIPPED           extends DAG → loop back to
                     SRE/Engineer dispatch
```

### Termination

A run terminates when ONE of:
1. **TL emits SHIP signal** (review mode → all good) → commander marks Run SHIPPED.
2. **TL emits ESCALATE signal** (review mode → can't recover) → commander marks Run ESCALATED.
3. **Cost cap fires** → commander aborts, marks Run FAILED with `cost_cap_exceeded`.
4. **Reaper kills stuck run** (Phase 2 carry-forward) → Run FAILED with `reaper_*`.

There is no hard re-plan count cap. Cost cap is the terminator. Honest dollars > arbitrary integers.

---

## 4. TL contract

### 4.1 Three modes

TL agent has one prompt with three branches selected by an input flag:

```python
class TLMode(StrEnum):
    PLAN = "plan"        # initial diagnosis + first task_plan
    REVIEW = "review"    # post-IC supervision, ship/replan/escalate decision
    REPLAN = "replan"    # generate delta task_plan after review found new work
```

Mode is set by commander when dispatching. Default mode for the first TL task is `PLAN`. Mode after IC subtasks complete is `REVIEW`. Mode if `REVIEW` says "more work" is `REPLAN`.

### 4.2 Output schema (extends v1.5 fix_spec)

```python
class TLOutput(TypedDict):
    # v1.5 fields (keep)
    root_cause: str
    fix_spec: str                        # human-readable summary
    affected_files: list[str]
    failing_command: str
    confidence: float
    open_questions: list[str]
    verify_command: str                  # full-CI-equivalent verify
    verify_success: VerifySuccess
    self_critique: SelfCritique          # v1.6 deterministic validator output

    # v1.7 NEW
    task_plan: list[TaskSpec]            # ordered DAG; commander persists these
    env_requirements: EnvRequirements    # SRE consumes
    review_decision: Literal["SHIP", "REPLAN", "ESCALATE"] | None  # only set in REVIEW mode
    replan_reason: str | None            # only set in REPLAN mode

class TaskSpec(TypedDict):
    task_id: str                         # "T2", "T3", ... (TL-assigned, sequential)
    agent: Literal["cifix_sre_setup", "cifix_engineer", "cifix_sre_verify"]
    depends_on: list[str]                # task_ids that must finish first; [] = ready immediately
    purpose: str                         # one-line human-readable
    steps: list[Step]                    # for engineer/sre_verify; SRE setup has env_requirements instead
    narrow_verify: NarrowVerify | None   # for engineer: how to verify just this subtask

class Step(TypedDict):
    id: int
    action: Literal["read", "replace", "insert", "delete_lines", "apply_diff", "run", "commit", "push"]
    file: str | None                     # for file ops
    line: int | None                     # for replace/insert/delete_lines
    after_line: int | None               # for insert
    old: str | None                      # for replace — exact match required
    new: str | None                      # for replace/insert
    diff: str | None                     # for apply_diff (unified diff format)
    command: str | None                  # for run
    expect_exit: int | None              # for run
    expect_stdout_contains: str | None   # for run
    message: str | None                  # for commit

class EnvRequirements(TypedDict):
    python: str | None                   # e.g., "3.11"
    os_packages: list[str]               # apt-get install candidates
    python_packages: list[str]           # pip install candidates
    env_vars: dict[str, str]             # name → value
    services: list[Literal["postgres", "redis", "mysql"]]  # docker-compose-able
    reproduce_command: str               # SRE runs this to confirm env reproduces failure
    reproduce_expected: str              # human-readable expected outcome (e.g., "fails with 'connection refused'")

class NarrowVerify(TypedDict):
    command: str
    success: VerifySuccess               # reuses v1.5 schema
```

### 4.3 TL prompt structure (mode-branched)

#### PLAN mode

```
You are CI Fixer v3's Tech Lead. You are the SOLE source of LLM judgment in
this system. Engineer and SRE-verify do not think — they execute your
instructions. SRE-setup uses LLM only to deliver the env you specify.

Your responsibilities in PLAN mode:
  1. Diagnose the bug from CI log + repo state.
  2. Ground every claim — read affected files BEFORE writing line-numbered
     steps. Do not invent line numbers from log inference.
  3. Emit a granular task_plan that anticipates child bugs ("if A succeeds,
     B is likely needed because [reason]"). Spell those out as siblings now,
     don't discover mid-flight.
  4. For each engineer task, write step-level instructions OR an exact
     unified diff. The engineer cannot adapt — your steps must be applied
     verbatim.
  5. Specify env_requirements precisely. SRE will deliver exactly this; if
     you under-specify, engineer will fail and you'll have to re-plan.
  6. Run validate_self_critique before emit_fix_spec. Use returned booleans
     verbatim (Phase 1 v1.6 contract).

Cost budget for this task: $5. Use it. Read files. Re-read CI log if needed.
Run grep. Trace the bug deeply. Do not cheap out.

Output: TLOutput dict. task_plan is REQUIRED. review_decision MUST be null.
replan_reason MUST be null.
```

#### REVIEW mode

```
You are reviewing a v3 run that has completed all subtasks you previously
planned. You see:
  - the original CI log
  - your previous task_plan
  - each subtask's output (engineer's diff, SRE's env transcript, narrow_verify results)
  - the FULL verify_command run on the final state

Decide:
  SHIP: original failing_command now passes; no new red surfaced. Done.
  REPLAN: subtasks completed but verify reveals new work. Emit a delta
          task_plan for the additional work. Set review_decision="REPLAN"
          and replan_reason explaining what surfaced.
  ESCALATE: subtasks repeatedly fail in ways you can't fix. Set
          review_decision="ESCALATE" and explain in fix_spec.

Cost budget: $5. Read the diffs carefully. Re-run validate_self_critique on
any new task_plan you emit.

Output: TLOutput dict. review_decision REQUIRED. task_plan present only if
review_decision="REPLAN".
```

#### REPLAN mode

(Reached only via REVIEW emitting `review_decision="REPLAN"` — same agent invocation continues.)

```
You discovered new work in your previous review. Emit a DELTA task_plan
containing only the new tasks (not the already-completed ones). Commander
will append these to the existing DAG with depends_on chains as you specify.

Constraints:
  - Each new task_id must be unique across the run (use T<N+1>, T<N+2>...).
  - depends_on can reference completed task_ids OR new ones in this delta.
  - replan_reason MUST be set.

Cost budget: same $5 task budget continues.
```

### 4.4 Grounding requirement (HARD GATE)

Before TL emits any `Step` with `line` or `after_line` set, TL MUST have called `read_file` on `step.file` in this turn. The `_tl_grounding` validator (new module) audits the tool trace and rejects emit_fix_spec if any line-numbered step references a file not yet read.

**Mechanism**: extend Phase 1's `validate_self_critique` to add check c4:

```python
def check_c4_grounding_satisfied(steps, tool_calls_in_session):
    files_read = {tc.input["path"] for tc in tool_calls_in_session if tc.name == "read_file"}
    for step in steps:
        if step.get("line") is not None or step.get("old") is not None:
            if step["file"] not in files_read:
                return False, f"step {step['id']} references {step['file']} but no read_file call in this turn"
    return True, None
```

If c4 fails, TL must re-read and re-emit. Same retry pattern as c1/c2/c3.

### 4.5 Self-critique extensions

Phase 1's `validate_self_critique` (v1.6 Phase 1 module `_tl_self_critique`) gains:

- **c4: grounding_satisfied** — see 4.4
- **c5: every_step_precondition_checkable** — for each `replace` step, the `old` substring must appear in the file's current content. Validator does the grep itself; TL claims about "old text exists" are not load-bearing.
- **c6: env_requirements_resolvable** — `python`/`os_packages`/`python_packages` strings must be syntactically valid; `services` must be in the supported set; `reproduce_command` first token must be resolvable (same as c3).

All three are deterministic. The TL prompt mandates calling validate_self_critique, but the **commander** holds the authoritative gate (`commander_verify_fix_spec_self_critique` in `_tl_self_critique.py`, v1.6 Phase 1 carry-forward).

### 4.6 Tool registry for TL

| Tool | Source | Purpose |
|---|---|---|
| `read_file` | existing | Grounding |
| `glob` | existing | Find files |
| `grep` | existing | Search content |
| `fetch_ci_log` | existing | Read failure |
| `fetch_workflow_yaml` | existing | Read CI shape |
| `validate_self_critique` | v1.6 Phase 1 (extend with c4/c5/c6) | Authoritative gate |
| `emit_fix_spec` | existing (extend schema) | Final output |

No new tools — just schema extensions.

---

## 5. Engineer contract

### 5.1 Role

Receive a `TaskSpec` (subset of TL's plan). Walk steps. Apply patches. Run verify. Commit. Push. Report.

**No invention.** If a step's precondition doesn't match (e.g., `old` text not at `line`), engineer fails with `step_precondition_violated` and reports back. TL re-plans. Engineer never tries to "fix it up."

### 5.2 Step interpreter (deterministic dispatch)

```python
async def execute_step(step: Step, sandbox: Sandbox) -> StepResult:
    match step["action"]:
        case "read":
            content = await sandbox.read_file(step["file"])
            return StepResult(ok=True, output={"content_len": len(content)})

        case "replace":
            current = await sandbox.read_file(step["file"])
            if step["old"] not in current:
                return StepResult(ok=False, error="step_precondition_violated",
                                  detail=f"'{step['old'][:80]}' not in {step['file']}")
            new_content = current.replace(step["old"], step["new"], 1)
            await sandbox.write_file(step["file"], new_content)
            return StepResult(ok=True)

        case "insert":
            lines = (await sandbox.read_file(step["file"])).splitlines(keepends=True)
            insert_at = step["after_line"]  # 1-indexed
            lines.insert(insert_at, step["content"] if step["content"].endswith("\n") else step["content"] + "\n")
            await sandbox.write_file(step["file"], "".join(lines))
            return StepResult(ok=True)

        case "apply_diff":
            r = await sandbox.run(["git", "apply", "-"], stdin=step["diff"])
            if r.exit_code != 0:
                return StepResult(ok=False, error="diff_apply_failed", detail=r.stderr[:500])
            return StepResult(ok=True)

        case "run":
            r = await sandbox.run(shlex.split(step["command"]))
            expected = step.get("expect_exit", 0)
            if r.exit_code != expected:
                return StepResult(ok=False, error="run_unexpected_exit",
                                  detail=f"got {r.exit_code} expected {expected}, stderr={r.stderr[-500:]}")
            needle = step.get("expect_stdout_contains")
            if needle and needle not in r.stdout:
                return StepResult(ok=False, error="run_stdout_mismatch",
                                  detail=f"expected substring {needle!r} not found")
            return StepResult(ok=True, output={"stdout_tail": r.stdout[-500:]})

        case "commit":
            await sandbox.run(["git", "add", "-A"])
            r = await sandbox.run(["git", "commit", "-m", step["message"]])
            if r.exit_code != 0:
                return StepResult(ok=False, error="commit_failed", detail=r.stderr[:500])
            sha = (await sandbox.run(["git", "rev-parse", "HEAD"])).stdout.strip()
            return StepResult(ok=True, output={"commit_sha": sha})

        case "push":
            r = await sandbox.run(["git", "push"])
            if r.exit_code != 0:
                return StepResult(ok=False, error="push_failed", detail=r.stderr[:500])
            return StepResult(ok=True)

        case "delete_lines":
            lines = (await sandbox.read_file(step["file"])).splitlines(keepends=True)
            start, end = step["line"], step.get("end_line", step["line"])  # inclusive
            del lines[start - 1 : end]
            await sandbox.write_file(step["file"], "".join(lines))
            return StepResult(ok=True)
```

This is deterministic Python. **No LLM call inside `execute_step`.**

### 5.3 Engineer agent loop

```python
async def execute(self, task: Task) -> AgentResult:
    spec: TaskSpec = task.input["spec"]

    for step in spec["steps"]:
        result = await execute_step(step, self.sandbox)
        if not result.ok:
            return AgentResult(
                success=False,
                error=f"step {step['id']} failed: {result.error}",
                output={"failed_step_id": step["id"], "detail": result.detail,
                        "completed_steps": [s["id"] for s in spec["steps"][:step["id"] - 1]]},
            )

    # Narrow verify if specified
    if nv := spec.get("narrow_verify"):
        r = await self.sandbox.run(shlex.split(nv["command"]))
        if not is_verify_success(r, nv["success"]):
            return AgentResult(
                success=False,
                error="narrow_verify_failed",
                output={"verify_exit": r.exit_code, "verify_stdout_tail": r.stdout[-1000:]},
            )

    return AgentResult(success=True, output={
        "completed_steps": [s["id"] for s in spec["steps"]],
        "commit_sha": _last_commit_sha_from_steps(spec["steps"]),  # extract from step results
    })
```

### 5.4 Sonnet usage

Engineer's Sonnet usage is reduced to **one optional call per task**: if `step.action == "apply_diff"` and the diff fails to apply cleanly, an LLM-driven fallback can attempt to re-anchor the hunks (handles minor formatting drift). This is the only judgment call. Cap at 5 turns; if it fails, return `step_precondition_violated`. The $1 cap easily covers this — typically engineer spends $0 because there's no Sonnet call.

For v1.7's first build, **skip even this fallback**. Pure deterministic interpreter. If TL's diff doesn't apply, engineer fails, TL re-plans. Add the LLM fallback in v1.7.1 only if data shows it's needed.

### 5.5 No coder subagent

The `coder_subagent` (today's Sonnet inner loop with `MAX_SUBAGENT_TURNS=10`) is **deleted** in v1.7. Engineer becomes a Celery task with the step interpreter. This deletes ~600 LOC and structurally eliminates Bug #17.

---

## 6. SRE contract

### 6.1 Two SRE roles

v1.4.0 already has agentic SRE setup. We split it cleanly:

- **`cifix_sre_setup`** (agentic, $4 cap) — consumes `env_requirements`, delivers a working sandbox where `reproduce_command` produces `reproduce_expected`.
- **`cifix_sre_verify`** (mostly deterministic, $1 cap — reused engineer-style step interpreter) — runs the full `verify_command`, captures result, returns. No agentic loop.

### 6.2 SRE setup contract

Input: `env_requirements` from TL's plan.

```python
async def execute(self, task: Task) -> AgentResult:
    env_req: EnvRequirements = task.input["env_requirements"]

    # Phase 1: deterministic baseline
    await self._install_os_packages(env_req["os_packages"])
    await self._install_python(env_req.get("python"))
    await self._install_python_packages(env_req["python_packages"])
    await self._set_env_vars(env_req["env_vars"])
    await self._start_services(env_req["services"])

    # Phase 2: validate by running reproduce_command
    r = await self.sandbox.run(shlex.split(env_req["reproduce_command"]))
    if self._matches_expected(r, env_req["reproduce_expected"]):
        return AgentResult(success=True, output={"validated_via": "deterministic"})

    # Phase 3: agentic loop (existing v1.4.0 mechanism, scoped to env)
    return await self._agentic_repair_loop(env_req, max_cost_usd=4.0)
```

The agentic repair loop (v1.4.0's `sre_setup_subagent`) keeps its tool kit: `bash`, `read_file`, `write_file`, `git`, `pip`, `apt`. Its objective in v1.7 is narrowed: "make `reproduce_command` produce `reproduce_expected`." Not "guess what the bug needs."

### 6.3 SRE verify contract

Input: `verify_command` + `verify_success` from TL.

Pure step-style execution: run the command in the post-fix sandbox, apply `is_verify_success` matcher (v1.5 contract), return PASS/FAIL with stdout/stderr tails. No agentic loop. No LLM judgment.

### 6.4 SRE escalation

If `cifix_sre_setup` exhausts its $4 cap without delivering env, return `AgentResult(success=False, error="env_unreachable")`. TL receives this in REVIEW mode and decides REPLAN (try a different env shape) or ESCALATE.

---

## 7. Commander contract

### 7.1 State machine

Commander stays a Celery task chain orchestrator. Two new methods:

```python
async def _persist_initial_dag(self):
    """Create ONLY the TL plan task. sequence_num=1."""
    await self._insert_task(
        run_id=self.run_id,
        sequence_num=1,
        agent_role="cifix_techlead",
        input={"mode": "plan", "ci_context": self.ci_context},
    )

async def _extend_dag_from_tl_plan(self, tl_task_output: dict):
    """After TL plan/replan, persist task_plan tasks atomically.
    Validates plan against agent registry + DAG shape rules."""
    plan = tl_task_output["task_plan"]
    self._validate_plan(plan)  # registry check, no cycles, terminal task is verify
    next_seq = await self._next_sequence_num()
    for ts in plan:
        await self._insert_task(
            run_id=self.run_id,
            sequence_num=next_seq,
            agent_role=ts["agent"],
            input={"spec": ts},
            depends_on_seq=self._resolve_deps(ts["depends_on"]),
        )
        next_seq += 1

async def _dispatch_review_after_subtasks(self):
    """When all current subtasks complete, persist a TL review task."""
    next_seq = await self._next_sequence_num()
    await self._insert_task(
        run_id=self.run_id,
        sequence_num=next_seq,
        agent_role="cifix_techlead",
        input={
            "mode": "review",
            "previous_plan": self._collected_plans,
            "subtask_outputs": await self._collect_completed_outputs(),
            "verify_command": self._original_verify,
        },
    )

async def execute(self):
    # 1. Pre-checks (existing)
    await self._precheck()
    if self._aborted:
        return

    # 2. Persist + dispatch initial TL plan
    await self._persist_initial_dag()
    await self._wait_for_task_complete(seq=1)

    if not self._cost_cap_ok():
        return self._terminate("cost_cap_exceeded")

    # 3. Read TL plan, extend DAG
    tl_output = await self._read_task_output(seq=1)
    if tl_output["self_critique_failed"]:
        return self._terminate_with_retry_or_escalate()
    await self._extend_dag_from_tl_plan(tl_output)

    # 4. Loop: dispatch by sequence_num, dispatch TL review when batch done
    while not self._terminal:
        await self._dispatch_next_pending()
        if not self._cost_cap_ok():
            return self._terminate("cost_cap_exceeded")
        if await self._all_current_subtasks_done():
            await self._dispatch_review_after_subtasks()
            review = await self._wait_for_review()
            match review["review_decision"]:
                case "SHIP": return self._terminate("SHIPPED")
                case "ESCALATE": return self._terminate("ESCALATED")
                case "REPLAN":
                    # Same TL task with mode flipped; emit delta plan
                    await self._dispatch_replan_from_review(review)
                    delta = await self._wait_for_replan()
                    await self._extend_dag_from_tl_plan(delta)
                    # loop continues
```

### 7.2 Plan validator (deterministic)

```python
_AGENT_REGISTRY = {"cifix_sre_setup", "cifix_engineer", "cifix_sre_verify"}

def _validate_plan(plan: list[TaskSpec]) -> None:
    """Raise PlanValidationError if plan is malformed.
    Commander rejects malformed plans → TL task FAILED → retry/escalate."""
    if not plan:
        raise PlanValidationError("empty plan")

    ids = {t["task_id"] for t in plan}
    if len(ids) != len(plan):
        raise PlanValidationError("duplicate task_ids")

    for t in plan:
        if t["agent"] not in _AGENT_REGISTRY:
            raise PlanValidationError(f"unknown agent: {t['agent']}")
        for dep in t["depends_on"]:
            if dep not in ids and not _is_completed_task_id(dep):
                raise PlanValidationError(f"task {t['task_id']} depends on unknown {dep}")

    # No cycles (topological sort succeeds)
    if _has_cycle(plan):
        raise PlanValidationError("plan contains cycle")

    # Last task in topological order must be sre_verify
    sorted_plan = _topo_sort(plan)
    if sorted_plan[-1]["agent"] != "cifix_sre_verify":
        raise PlanValidationError("plan must terminate in cifix_sre_verify")
```

### 7.3 Cost cap (extends v1.6 Phase 2)

```python
COST_PER_TOKEN_USD = 20e-6
MAX_RUN_COST_USD = 25.0   # bumped from $1 to $25 in v1.7

PER_AGENT_COST_CAPS = {
    "cifix_techlead": 5.0,
    "cifix_sre_setup": 4.0,
    "cifix_sre_verify": 1.0,
    "cifix_engineer": 1.0,
}

async def _check_run_cost_cap(self) -> bool:
    """Returns True iff dispatch should ABORT due to run cost cap."""
    total = await self._sum_run_tokens()
    if total * COST_PER_TOKEN_USD > MAX_RUN_COST_USD:
        await self._fail_run(f"cost_cap: ${total * COST_PER_TOKEN_USD:.2f} > ${MAX_RUN_COST_USD}")
        return True
    return False
```

Per-agent caps are enforced **inside the agent's loop** (TL/SRE check their own running cost, abort if their per-task budget exceeded). Run cap is the commander backstop.

### 7.4 No LLM in commander

Reaffirmed: commander imports no LLM client. All "decisions" are deterministic (pattern match on review_decision, cost arithmetic, SQL queries). If a future change wants commander to "smart-route," that's a v1.8 architectural conversation — not a sneak-in change.

---

## 8. Migration from v1.6

### 8.1 Atomic, not gradual

v1.6 → v1.7 is a hard cutover for v3 dispatches. No dual pipeline. Rationale:

- Operating rule: "never introduce dual pipelines."
- v1.7 changes the agent contract shape (engineer is no longer LLM-driven). Trying to support both shapes simultaneously doubles the surface area and creates exactly the bug class we're trying to eliminate.
- The cutover window is small: deploy v1.7, watch testbed 4-cell + humanize one-shot, decide ship or rollback.

### 8.2 Files modified

| File | Change |
|---|---|
| `phalanx/agents/cifix_techlead.py` | Mode branching (plan/review/replan), task_plan emission, env_requirements emission |
| `phalanx/agents/_tl_self_critique.py` | Add c4 (grounding), c5 (step preconditions), c6 (env requirements) checks |
| `phalanx/agents/cifix_engineer.py` | Replace agent body with step interpreter (`execute_step` dispatch). DELETE coder subagent invocation. |
| `phalanx/agents/_engineer_step_interpreter.py` | NEW — `execute_step` function (~200 LOC) |
| `phalanx/agents/cifix_sre.py` | Split into setup vs verify code paths; setup keeps agentic loop scoped to env_requirements |
| `phalanx/agents/cifix_commander.py` | Two-phase DAG persistence, plan validator, review loop, cost cap update |
| `phalanx/agents/_plan_validator.py` | NEW — `_validate_plan` function |
| `phalanx/db/models.py` | Add `Task.depends_on_seq: list[int]` (JSONB) for DAG dependency tracking |
| `alembic/versions/20260503_0001_task_depends_on_seq.py` | NEW migration |
| `phalanx/queue/tasks.py` | Update execute_task to honor `depends_on_seq` (skip if any unmet) |

### 8.3 Files deleted

- `phalanx/agents/coder_subagent.py` (entire module — was the source of Bug #17)
- Related coder_subagent tests

### 8.4 Backwards compatibility

There is none for in-flight runs. v1.6 runs in EXECUTING state at deploy time will be reaped by the existing reaper or completed by their existing v1.6 task chain. New runs after deploy use v1.7. v3 dispatches do not span deploys.

---

## 9. Test strategy

### 9.1 Tier-1 (unit, fast, no Docker)

**TL tests** (`tests/integration/v3_harness/test_tl_v17.py`):
- PLAN mode emits valid TLOutput with task_plan + env_requirements
- REVIEW mode with all-green inputs emits SHIP
- REVIEW mode with surfacing new bug emits REPLAN with replan_reason
- REVIEW mode with repeated env failure emits ESCALATE
- Self-critique c4 fails when step references unread file
- Self-critique c5 fails when `old` substring not in target file
- Self-critique c6 fails when `reproduce_command` first token unresolvable
- TL emit blocked when any self-critique false (commander gate)

**Engineer step interpreter tests** (`tests/unit/ci_fixer_v3/test_engineer_step_interpreter.py`):
- `read`, `replace`, `insert`, `delete_lines`, `apply_diff`, `run`, `commit`, `push` each happy + sad path
- `replace` precondition mismatch returns `step_precondition_violated`
- `run` with `expect_exit=4` accepts exit 4
- `run` with `expect_stdout_contains` validates substring
- Diff that doesn't apply returns `diff_apply_failed` (no LLM fallback in v1.7)

**Plan validator tests** (`tests/unit/ci_fixer_v3/test_plan_validator.py`):
- Empty plan rejected
- Duplicate task_ids rejected
- Unknown agent rejected
- Cycle rejected
- Plan not terminating in sre_verify rejected
- Valid plan accepted

**Commander state machine tests** (`tests/unit/ci_fixer_v3/test_commander_v17.py`):
- Initial DAG persists only TL plan task
- After TL plan, _extend_dag_from_tl_plan persists N tasks with correct sequence_nums
- After all subtasks complete, review task is dispatched
- review_decision=SHIP → terminate SHIPPED
- review_decision=REPLAN → delta tasks appended
- review_decision=ESCALATE → terminate ESCALATED
- Run cost cap fires at $25
- Per-agent cost caps enforced (mocked token-usage records)

### 9.2 Tier-2 (integration, Docker, fast cadence)

**`tests/integration/v3_harness/test_v17_dispatch.py`**:
- Synthetic webhook → commander persists TL plan task → mocked TL output → commander extends DAG → mocked engineer success → mocked SRE verify pass → review SHIP → Run SHIPPED
- Synthetic webhook with engineer step_precondition_violated → review REPLAN → delta task → success second pass
- Synthetic webhook with $25 cost cap exceeded → Run FAILED with cost_cap_exceeded

### 9.3 Tier-3 (real prod / canary)

**Internal testbed regression** (must pass before external attempt):
- 4 Python cells (lint, test_fail, flake, coverage) all SHIP cleanly under v1.7
- Cost per shipped run logged; expect avg ≤ $3 (well under $25 cap)
- No reaper hits, no escalations

**External proof — DoD**:
- One external Python repo, real bug, real CI, real green push by v3
- Candidate: humanize tz-aware fix re-attempt (Path 1 from Phase 3). Webhook already wired.
- Acceptance:
  1. v3 reaches SHIPPED
  2. v3 commits a fix to `src/humanize/time.py`
  3. Real GitHub CI re-runs green on v3's commit
  4. Manual diff review: addresses tz-aware datetime handling (exact or functional equivalent)

---

## 10. Phase breakdown

Each phase has a binary milestone. No looping. Same discipline as v1.6 sprint.

### Phase 1 — TL extensions (1.5 days)

**Scope**:
- Schema additions (TaskSpec, EnvRequirements, NarrowVerify, Step in shared types)
- TL prompt mode-branching (plan/review/replan)
- Self-critique extensions c4/c5/c6
- Plan validator module

**Files**: `cifix_techlead.py`, `_tl_self_critique.py`, `_plan_validator.py`, types module

**Milestone**: Tier-1 TL + plan validator tests all green. PLAN mode emits valid TLOutput against fixture CI logs.

### Phase 2 — Engineer step interpreter (1 day)

**Scope**:
- `_engineer_step_interpreter.py` with all 8 actions
- New `cifix_engineer.execute()` body using interpreter
- DELETE `coder_subagent.py` and its tests

**Files**: `_engineer_step_interpreter.py` (new), `cifix_engineer.py`, deletes

**Milestone**: Tier-1 step interpreter tests all green. Engineer tests run without coder subagent.

### Phase 3 — SRE split + commander state machine (1.5 days)

**Scope**:
- Split SRE into setup (agentic) + verify (deterministic) code paths
- Two-phase DAG persistence in commander
- Review-after-subtasks dispatch loop
- Cost cap update ($25 run, per-agent caps)
- DB migration for `Task.depends_on_seq`
- Celery `execute_task` honors `depends_on_seq`

**Files**: `cifix_sre.py`, `cifix_commander.py`, `db/models.py`, alembic migration, `queue/tasks.py`

**Milestone**: Tier-2 dispatch tests all green. Synthetic flow: webhook → SHIPPED works end to end with mocked agents.

### Phase 4 — Internal regression (0.5 days)

**Scope**:
- Deploy v1.7 to staging
- Run testbed 4-cell on v1.7
- Compare to v1.6 baseline

**Milestone**: 4/4 testbed cells SHIP under v1.7 with no regressions. Avg run cost ≤ $3.

### Phase 5 — External proof (1 day)

**Scope**:
- Re-attempt humanize Path 1 (tz revert PR)
- Watch v3 dispatch under v1.7
- Manual review of result

**Milestone**: humanize Path 1 SHIPS — real CI green on v3's commit, fix addresses tz-aware datetime handling. **DoD met.**

If milestone NOT met:
- Capture full task chain + final commit
- File new bug, return to spec
- Don't iterate-to-fix in this phase (same discipline as v1.6 Phase 3)

**Total: ~5.5 days.** Sprint time-box: 7 days. Buffer for one phase to slip by 1 day.

---

## 11. Definition of Done — binary checklist

- [ ] Phase 1: TL emits valid TLOutput with task_plan + env_requirements; self-critique c4/c5/c6 catch fixture violations
- [ ] Phase 2: Engineer step interpreter handles all 8 actions; coder subagent deleted
- [ ] Phase 3: Two-phase DAG works; review loop dispatches REPLAN on demand; $25 run cap fires correctly
- [ ] Phase 4: 4/4 testbed cells SHIP under v1.7; avg run cost ≤ $3
- [ ] **Phase 5 (the proof point)**: humanize Path 1 SHIPS — v3's commit makes real CI green; fix addresses tz-aware datetime

If Phase 5 RED at end of sprint, ship v1.7 architecture without the proof point claim. Don't fudge.

---

## 12. What's NOT in v1.7

Explicit non-goals — defer to v1.8 or later:

- **Phase 4 of v1.6 sprint (observability dashboard).** Slips to v1.8 Phase 1.
- **Phase 5 of v1.6 sprint (concurrency stress).** Slips to v1.8 Phase 2.
- **Phase 6 of v1.6 sprint (GitHub App).** Slips to v1.8 Phase 3.
- **Multi-language support** (Go, Rust, Node). v1.7 is Python-only.
- **LLM fallback in engineer's apply_diff** (re-anchor hunks). Defer to v1.7.1 if data shows need.
- **Smart commander routing** (LLM-based). Commander stays deterministic. Period.
- **Multi-tenant credential resolution.** PAT path stays for v1.7.
- **Marketplace publication.** Needs v1.8 GitHub App + 3-week review.

---

## 13. Risk register

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| TL hallucinates line numbers despite c4 grounding gate | med | high | c5 validates `old` substring presence; engineer's interpreter rejects mismatches loudly; TL re-plans from fresh evidence |
| TL writes wrong unified diff (whitespace drift) | med | med | engineer reports `diff_apply_failed` → TL re-plans; if persistent, add v1.7.1 LLM fallback |
| TL re-plans loop chews through $25 cap on first wild bug | med | low | per-task $5 cap on TL constrains depth per call; cost cap is the honest terminator |
| Plan validator rejects legitimate plans (over-strict) | low | high | tier-1 corpus tests both directions; no semantic rules, only structural |
| Engineer step interpreter has subtle off-by-one (insert/delete) | med | high | tier-1 tests cover each action with golden-file fixtures |
| External proof (humanize) fails for the same reason as Phase 3 | low | high | architecture explicitly addresses Phase 3's failure mode (engineer judgment); if it fails again, capture and file as architectural insight |
| Hard cutover from v1.6 breaks in-flight runs | low | low | reaper handles stuck runs; v3 runs are short (<10min); deploy at low-traffic window |
| Commander review loop has subtle race (subtask completes between checks) | med | med | tier-2 dispatch tests use deterministic clock + mocked agents; reaper backstops |

---

## 14. Open questions

1. **Should TL emit unified diffs or step-level instructions, or both?**
   - Recommend: TL emits whichever is cleaner *for this fix*. Single line change → step. Multi-hunk diff → diff. Engineer interpreter handles both via the action dispatch. TL prompt encourages diff for >3 line changes.

2. **Should `depends_on_seq` enforce strict topological dispatch, or allow parallel SRE_setup + Engineer prep?**
   - Recommend: strict topological in v1.7. Parallelism is a v1.8 concern (paired with concurrency stress phase).

3. **Should commander surface the cost cap as a GitHub PR comment when it fires?**
   - Defer — observability is v1.8 Phase 1. For v1.7, structured log + DB error_message is enough.

4. **Should TL's REVIEW mode have access to `apply_diff` to "fix it themselves" if they spot a one-line tweak?**
   - No. TL plans, doesn't execute. Keeping the boundary clean preserves the architectural property that engineer is the sole code-writing surface. If REVIEW spots a small fix, REPLAN with a single-step engineer task.

5. **What if `cifix_sre_setup` consumes its $4 cap mid-loop?**
   - Already covered: returns `env_unreachable` → TL REVIEW handles it (REPLAN with simpler env or ESCALATE).

---

## 15. Why we believe this works

The v1.6 sprint produced one piece of strong evidence: **TL is good enough to be the planner.** Phase 3 Path 1's TL output was spot-on — root cause, affected files, verify command, self-critique all true on a real wild bug. The failure was downstream, in engineer's coder loop trying to invent the implementation.

v1.7 closes the loop by giving TL the responsibility *and* the budget to do the work it's already good at, and removes the responsibility from the place we observed it fail. The cost ($5 per TL task vs $0.10 today) is real but not the bottleneck — correctness is. We're trading dollars for predictability, and one external repo green is worth the trade.

If this works, v1.7 becomes the architecture the marketplace launch is built on. If it doesn't, we'll have learned what the ceiling actually is — and that's a v1.8 conversation, not a v1.7 one.
