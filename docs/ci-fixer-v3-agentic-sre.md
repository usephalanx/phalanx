# CI Fixer v3 — Agentic SRE design

**Status**: design (2026-04-30). Implementation deferred to a fresh session.
**Surfaced by**: humanize regression smoke 2026-04-28, where SRE setup couldn't replicate humanize's CI env (`astral-sh/setup-uv@v8`) and forced TL to handle "sandbox env mismatch" outside its charter.
**Replaces**: deferred Path 1 (fat base image). The fat-image approach is per-language and accumulates dead weight. Agentic SRE is language-agnostic and matches the rest of v3's LLM-driven shape.

## 1. Problem statement

Today's SRE setup is **deterministic regex** over `pyproject.toml` + `apt install` lines in workflow YAML. That works for the common Python case but fails the moment a workflow uses anything novel:

- `astral-sh/setup-uv@v8` → sandbox lacks `uv`/`uvx` (humanize)
- `actions/setup-go@v5` → sandbox lacks `go`
- `actions/setup-node@v4` → sandbox lacks `node`/`npm`
- Custom `run:` blocks with `curl … | sh` installers
- Dockerfile-based CI (`container:` directive)
- Composite actions, custom local actions

When SRE setup is incomplete, SRE verify re-runs upstream commands literally and gets `127 not found`. Those failures bubble up as `new_failures` to commander → iter-2 TL is asked to "fix" what is actually a sandbox gap. TL correctly says "no code change possible" but the commander, engineer, and run-state machine have no clean way to handle this — runs FAIL without a deployable verdict.

Root cause: SRE charter is "make sandbox a faithful replica of upstream CI", and the deterministic implementation can't fulfill that charter for the long tail of CI patterns.

## 2. Goals

1. **Language-agnostic env setup**: SRE figures out what upstream CI installs (Python, Node, Go, Java, C#, Rust, ...) without per-language hardcoding.
2. **Self-recovery within charter**: SRE detects missing tools, tries reasonable install strategies (apt → pip → curl → conda), and reports ready/blocked clearly.
3. **Clean cross-agent contract**: TL only ever sees real code-failure questions. The "sandbox env mismatch" path moves entirely inside SRE.
4. **Bounded cost & latency**: per-run SRE LLM cost ≤ $0.20, wall-clock ≤ 90s on average for runs needing extra tools (vs ~30s deterministic today).
5. **External Python repos work without manual config**: humanize lint cell SHIPS in a single iter post-fix; one additional external Python repo using uv/poetry/pdm also works.

## 3. Non-goals

- Replacing TL or Engineer with a different model. They stay GPT-5.4 / Sonnet.
- Making SRE verify-mode agentic too. Verify stays deterministic for now (different problem class — comparing observed exit codes, not planning installs).
- Supporting arbitrary Dockerfile-based CI (`container:` directive). That's a separate base-image-swap design; SRE remains a "Linux sandbox + tools" model.
- Fixing TL/Engineer prompt weaknesses unrelated to env setup.

## 4. Architecture

### Current (deterministic)

```
SRE setup task:
  ├─ clone repo
  ├─ env_detector.py (regex over pyproject.toml + workflow YAML)
  ├─ provisioner.provision_on_the_fly(env_spec)  # docker run + apt + pip install
  └─ Task.output: { container_id, workspace_path, env_spec, setup_log }
```

### Proposed (agentic)

```
SRE setup task:
  ├─ clone repo
  ├─ provisioner.provision_bare_sandbox()  # docker run + base apt only
  ├─ run_sre_setup_subagent(  # LLM loop, mirrors run_coder_subagent
  │     llm_call=sonnet,
  │     tools=[read_file, list_workflows, exec_in_sandbox,
  │             check_command_available, install_apt, install_pip,
  │             install_via_curl, report_ready, report_blocked],
  │     repo_workspace=...,
  │     observed_failing_commands=ci_context.failing_commands,
  │     max_iterations=10,
  │   )
  └─ Task.output: {
       container_id, workspace_path,
       capabilities_installed: [{tool, version, install_method}],
       setup_log: [...],     # tool calls + observations, audit trail
       final_status: READY | PARTIAL | BLOCKED,
       blocked_reason: str | None,
     }
```

### Key design points

- **Reuses the coder subagent loop pattern** — `run_sre_setup_subagent` is structured like `run_coder_subagent` (proven in production for the engineer agent). Same LLM call interface, same tool dispatch, same retry-and-cap mechanics.
- **Tools are restricted** to env-setup operations only (no `apply_patch`, no `commit_and_push`, no `comment_on_pr`). SRE cannot accidentally edit the customer's code.
- **Bounded loop**: max 10 tool calls. If exhausted, return PARTIAL with what was installed.
- **Provider choice**: Sonnet for cost (pattern recognition + tool sequencing, not deep reasoning). Estimate ~5-7 tool calls per run, ~$0.05-$0.10 per setup.

## 5. System prompt sketch

```
You are the SRE Agent in the CI Fixer pipeline. Your job: prepare a Linux
sandbox to faithfully replicate upstream CI's environment.

You receive:
  - A workspace_path (already cloned at the failing PR's HEAD)
  - A bare sandbox container_id (Linux, sudo available, base packages only)
  - observed_failing_commands: the CI commands that failed and need to run here
  - Optional tech_lead_hints (rare — TL usually focuses on code, not env)

Your goal: when run_in_sandbox is called on each observed_failing_command, it
must execute (it may exit non-zero from real test failures — that's fine —
but it must NOT fail with "command not found" or environment errors).

Workflow you MUST follow:

1. INVESTIGATE: read the repo to understand the env contract.
   - Read .github/workflows/*.yml — look for `uses:` (action installs) and
     `run:` (shell installs). Common: setup-uv, setup-go, setup-node, custom
     curl installers.
   - Read pyproject.toml / package.json / go.mod / etc. for dep declarations.
   - Read .pre-commit-config.yaml / .tool-versions / Dockerfile if present.

2. PLAN: list the install steps you intend, in order.
   Examples:
     - "Install uv via pip" (workflow uses setup-uv)
     - "Install tox + tox-uv via pip" (workflow uses uvx tox)
     - "apt install gettext" (workflow has 'sudo apt install gettext')

3. EXECUTE: one install at a time, verifying each.
   - Use install_apt for system packages.
   - Use install_pip for Python packages.
   - Use install_via_curl ONLY for tools that explicitly require it (rust
     toolchain, deno, etc.) and only from known-safe domains.
   - After each install, call check_command_available to verify.

4. VERIFY: for each observed_failing_command's first token, confirm the
   command exists. (Don't run the failing_command — that's the next agent's
   job.)

5. REPORT: call report_ready with a summary of what you installed, OR
   report_blocked with a specific reason if a required tool can't be installed
   (no sudo, network failure, command requires GitHub Actions context, etc.).

Constraints:
  - Cap your tool calls at 10. Stop and report_partial if exhausted.
  - Only install what is EVIDENCED in the repo's setup files. Don't guess
    based on language stereotypes (e.g., not every Python repo needs `uv`).
  - Don't run the observed_failing_commands themselves.
  - Don't edit any files. Read-only on the workspace.
  - GitHub Actions expressions like `${{ matrix.python-version }}` cannot be
    expanded outside GHA. If a workflow needs them to function, report that
    in report_blocked — the run cannot proceed without GHA context.

Output: your final tool call must be one of report_ready, report_partial,
or report_blocked.
```

## 6. DAG contract changes

### Task description (cifix_commander → cifix_sre setup)

Today's `Task.description` already carries `ci_context` JSON. Add:

```json
{
  ...existing fields...,
  "observed_failing_commands": [
    "uvx --with tox-uv tox -e mypy",
    "ruff check ."
  ]
}
```

Commander populates this from the webhook + check_runs API. List of strings, in any order; SRE iterates over them.

### Task output schema (cifix_sre setup)

Today:
```json
{
  "mode": "setup",
  "container_id": "...",
  "workspace_path": "...",
  "env_spec": {...},
  "setup_log": [...]
}
```

After:
```json
{
  "mode": "setup",
  "container_id": "...",
  "workspace_path": "...",
  "capabilities_installed": [
    {"tool": "uv", "version": "0.8.x", "install_method": "pip"},
    {"tool": "tox", "version": "4.x", "install_method": "pip"}
  ],
  "setup_log": [
    {"step": "read_workflow", "file": ".github/workflows/lint.yml", ...},
    {"step": "install_pip", "packages": ["uv"], "exit_code": 0},
    ...
  ],
  "final_status": "READY",       // READY | PARTIAL | BLOCKED
  "blocked_reason": null,        // populated if BLOCKED
  "observed_failing_commands_status": [
    {"cmd": "uvx --with tox-uv tox -e mypy", "first_token_available": true},
    {"cmd": "ruff check .", "first_token_available": true}
  ]
}
```

### Commander handling of SRE final_status

| status | commander action |
|---|---|
| READY | proceed to TL (current path) |
| PARTIAL | proceed to TL with a flag `sre_setup_partial=true`. TL sees which capabilities are missing in `prior_sre_partial`. |
| BLOCKED | terminate run with `status=ESCALATED`, `escalation_reason=sre_blocked: <reason>`. No TL attempt. |

## 7. State machine — SRE setup loop

```
[start]
  → INVESTIGATING (read workflow YAML, pyproject, etc.)
  → PLANNING     (LLM produces ordered install plan)
  → INSTALLING   (loop: one install per iteration, verify each)
       ↓
       └→ if all installs succeed + first_tokens available: READY
       └→ if loop budget exhausted but some installs succeeded: PARTIAL
       └→ if a critical install fails after retry: BLOCKED
       └→ if workflow needs GHA-only context: BLOCKED with specific reason
```

Loop budget: 10 tool calls total (read_file + install + check_command counts each).

## 8. Failure modes & escalation

| failure | SRE response |
|---|---|
| Network failure during install | retry the install once with same params |
| Install command exit-non-zero | try ONE alternative path (apt→pip→curl), then BLOCKED |
| Loop budget exhausted | PARTIAL with installed list + missing list |
| `sudo` denied | BLOCKED ("sandbox lacks sudo for system package install") |
| Workflow uses `${{ matrix.* }}` expressions essential for command execution | BLOCKED ("workflow requires GitHub Actions context for matrix expansion") |
| `container:` directive in workflow | BLOCKED ("upstream uses custom container; out of scope for v3") |
| LLM produces malformed tool call | retry once; second malformed → BLOCKED |
| LLM tries to call disallowed tool | refuse silently, show available-tools error in next observation |

## 9. Testing strategy

Three tiers + a canary, mirroring the bug-#9/#10/#11 discipline.

### Tier-1 — fast, no Docker, no LLM (target < 5s)

Unit tests for tool implementations:
- `read_file`, `list_workflows` against in-memory fixture repos
- `check_command_available` (mock subprocess)
- `install_apt`, `install_pip`, `install_via_curl` (mock subprocess + assert command shape)
- Output schema validation (`final_status` ∈ {READY,PARTIAL,BLOCKED})
- System prompt parser: regex fixture LLM outputs and assert tool dispatch
- Loop-budget enforcement: mock LLM emits 11 tool_use messages, assert PARTIAL after 10

Fixture repos:
- `tests/integration/v3_harness/fixtures/python_uv/` — pyproject + workflow with setup-uv
- `tests/integration/v3_harness/fixtures/python_pip_only/` — vanilla pip
- `tests/integration/v3_harness/fixtures/node_pnpm/` — package.json with pnpm
- `tests/integration/v3_harness/fixtures/blocked_container/` — workflow with `container:` directive

### Tier-2 — real Postgres, mocked LLM (target < 30s)

Run full SRE setup task end-to-end with a scripted LLM:
- Fixture: `python_uv` → scripted LLM emits read_workflow + install_pip(uv) + check + report_ready
  - Assert: capabilities_installed includes uv; final_status=READY; Task.output schema valid
- Fixture: `blocked_container` → scripted LLM identifies `container:` and emits report_blocked
  - Assert: final_status=BLOCKED; blocked_reason mentions "custom container"
- Fixture: budget-exhausted (script LLM into 11 tool calls)
  - Assert: final_status=PARTIAL; setup_log has exactly 10 tool entries

### Tier-3 — real Docker, mocked LLM (target < 2 min, opt-in via env var)

Provisions an actual sandbox container, runs scripted install commands:
- Fixture: `python_uv` with real `pip install uv` → assert `uv --version` works
- Fixture: `blocked_container` → assert sandbox not even created
- Tests are GATED on `RUN_TIER3=1` env var so default test runs stay fast

### Canary — real LLM, real Docker, real prod

Three runs:
1. **Internal testbed coverage cell** — regression check; expect SHIPPED unchanged.
2. **Internal testbed all 4 cells** — bulk regression; expect 4/4 SHIP unchanged.
3. **Humanize lint cell** — primary unlock; expect SHIPPED in a single iter (no iter-2). Validates the agentic SRE installs `uv` correctly.
4. (Stretch) **One new external Python repo with poetry or pdm** — proves language-agnostic claim for Python siblings.

Success threshold: 4/4 internal still green AND humanize SHIPS in single iter AND new external repo SHIPS.

## 10. Execution plan (phased, with checkpoints)

### Phase 1 — Tools (Day 1, ~4-6 hours)
- [ ] Implement read_file, list_workflows, exec_in_sandbox, check_command_available
- [ ] Implement install_apt, install_pip, install_via_curl
- [ ] Implement report_ready, report_partial, report_blocked (sentinel tools)
- [ ] Tier-1 tests for each tool
- [ ] **Checkpoint**: tier-1 green; can dispatch tools manually from a python REPL

### Phase 2 — Loop + LLM wiring (Day 1-2, ~4 hours)
- [ ] `run_sre_setup_subagent` function modeled on `run_coder_subagent`
- [ ] Sonnet provider call wired (reuse `build_sonnet_coder_callable` pattern)
- [ ] System prompt v1 finalized
- [ ] Loop-budget enforcement
- [ ] Tier-1 tests with scripted LLM
- [ ] **Checkpoint**: scripted-LLM tests pass; output schema validates

### Phase 3 — Integration (Day 2, ~3-4 hours)
- [ ] Update `cifix_sre._execute_setup` to call `run_sre_setup_subagent`
- [ ] Update Task.output schema (capabilities_installed, final_status, etc.)
- [ ] Update commander to handle PARTIAL/BLOCKED states
- [ ] Update existing tier-1 + tier-2 tests for new schema
- [ ] **Checkpoint**: existing harness still green; new schema tests pass

### Phase 4 — Validation (Day 2-3, ~3 hours)
- [ ] Tier-3 docker-real tests (opt-in)
- [ ] Local end-to-end run on fixture repos
- [ ] **Checkpoint**: tier-3 green when run; mocked end-to-end green

### Phase 5 — Deploy + canary (Day 3, ~3 hours)
- [ ] Tag v1.4.0 (architectural change merits minor bump)
- [ ] Deploy via `deploy.sh`
- [ ] Verify schema migration (none needed, just code change)
- [ ] Run testbed 4/4 — must SHIP unchanged
- [ ] Run humanize lint cell — must SHIP in single iter
- [ ] (Stretch) Run one new external Python repo
- [ ] **Checkpoint**: success criteria from §2 all met

### Phase 6 — Document & wrap (Day 3, ~1 hour)
- [ ] Update [docs/ci-fixer-v3-webhook-coordination.md](ci-fixer-v3-webhook-coordination.md) with reference to this doc
- [ ] Update memory: bug #11/#12/#13 status, agentic-SRE status
- [ ] Mark Path 1 (fat base image) as superseded in any plan/changelog references
- [ ] Update website changelog v1.4.0

## 11. Success criteria (measurable)

| # | criterion | measurement |
|---|---|---|
| 1 | No regression on internal Python | `tests/integration/v3_harness*/` all green; testbed 4/4 cells SHIP |
| 2 | Humanize lint cell SHIPS in single iter | task chain has ≤4 tasks; no iter-2; 1 commit on src only |
| 3 | New external Python repo works | one new repo (poetry, pdm, or pip-tools based) SHIPS without manual config |
| 4 | Cost per run delta | < $0.20 average; tracked via task tokens_used field |
| 5 | Wall-clock delta | < 90s average for runs that need extra tool installs |
| 6 | Test coverage | tier-1 + tier-2 covers ≥ 80% of new SRE code |
| 7 | Cross-agent contract clean | TL never produces `confidence=0.0` for env reasons in 10 consecutive canary runs |

## 12. Open questions

1. **Provider choice**: Sonnet (cheap, pattern-match) or GPT-4o (better tool-use sequencing)? Recommend start with Sonnet, A/B if tool sequencing degrades.
2. **Caching**: should we memoize "this repo's setup looks like X" to skip the LLM loop on repeat runs? Defer to v1.4.1 — first prove the loop works.
3. **Verify mode**: should it ALSO be agentic? Defer; today's deterministic verify works once setup is correct.
4. **Tool variety**: do we need `install_brew` for macOS-base sandboxes? No — sandbox is Linux-only by design.
5. **Network egress**: agentic SRE will hit pypi.org, github.com, raw.githubusercontent.com. Already allowed in sandbox. Document allowed domains.
6. **Audit trail**: setup_log entries must be PII-free (no commit shas of customer code in logs). Standard practice; just call out.

## 13. Appendix — example trace (humanize lint)

```
Tool: list_workflows
Result: [".github/workflows/lint.yml", ".github/workflows/test.yml", ...]

Tool: read_file(".github/workflows/lint.yml")
Result: <yaml content showing setup-uv@v8 + uvx tox -e mypy>

Tool: install_pip(["uv"])
Result: {exit_code: 0, stdout: "Successfully installed uv-0.8.4"}

Tool: check_command_available("uv")
Result: {found: true, version: "uv 0.8.4"}

Tool: install_pip(["tox", "tox-uv"])
Result: {exit_code: 0}

Tool: check_command_available("tox")
Result: {found: true, version: "tox 4.27.0"}

Tool: report_ready(
  capabilities=[{tool:"uv",method:"pip"},{tool:"tox",method:"pip"}],
  observed_failing_commands_status=[
    {cmd:"uvx --with tox-uv tox -e mypy", first_token_available: true}
  ]
)
```

Total: 6 tool calls, ~30s wall, ~3000 input tokens + 200 output ≈ $0.04.
