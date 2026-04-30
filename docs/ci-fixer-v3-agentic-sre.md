# CI Fixer v3 — Agentic SRE design (v2)

**Status**: design v2 (2026-04-30) — v1 reviewed, 7 reliability gaps closed. Implementation deferred to fresh session.
**Surfaced by**: humanize regression smoke 2026-04-28; SRE setup couldn't replicate humanize CI env (`astral-sh/setup-uv@v8`) and forced TL to handle "sandbox env mismatch" outside its charter.
**Replaces**: deferred Path 1 (fat base image) — per-language, accumulates dead weight. This design is language-agnostic.

## 1. Problem statement

Today's SRE setup is deterministic regex over `pyproject.toml` + `apt install` lines. That covers a narrow band of Python repos. Anything else (`uses: setup-uv`, `setup-go`, `setup-node`, custom curl installers, monorepos, matrices, composite actions) trips it. SRE then can't fulfill its charter — replicate upstream CI in the sandbox — and the system has no clean recovery, so iter-2 TL gets asked questions outside its charter.

## 2. Goals

1. **Language-agnostic env setup** — Python, Node, Go, Java, C# without per-language hardcoding.
2. **Reliable** (numeric criteria in §11) — predictable behavior, bounded cost, low false-install rate.
3. **Self-recovery within charter** — SRE detects missing tools, tries reasonable strategies, reports clearly.
4. **Clean cross-agent contract** — TL only sees real code-failure questions. Env-mismatch path stays inside SRE.
5. **External Python repos work without manual config** — humanize lint cell SHIPS in single iter post-fix; one additional external Python repo also works.

## 3. Non-goals

- Replacing TL or Engineer agents.
- Making SRE verify-mode agentic (different problem class — comparing exit codes, not planning installs).
- Supporting `container:` directive (custom upstream container) — separate base-image-swap design.
- Supporting `services:` block (sidecar databases etc.) — out of scope for v3.

## 4. Architecture — **hybrid deterministic-first**

The biggest mistake of design v1 was assuming the LLM should drive setup from scratch. That's expensive, non-deterministic, and unnecessary for the common case. Most repos resolve cleanly with deterministic detection. The LLM only needs to fill GAPS.

### Decision tree per setup task

```
1. provision_bare_sandbox()                 # docker run + base apt
2. det_spec = env_detector.detect_env(repo) # current deterministic logic
3. provision_from_spec(det_spec)            # apt installs + pip installs from det_spec
4. observed_first_tokens = [
     extract_first_token(cmd) for cmd in observed_failing_commands
   ]
5. for token in observed_first_tokens:
       if not check_command_available(token):
           gaps.append(token)
6. if not gaps:
       return Output(final_status=READY, ...)   # FAST PATH — no LLM call
7. else:
       agentic_result = run_sre_setup_subagent(
           workspace=repo,
           container_id=sandbox.container_id,
           gaps=gaps,
           det_spec=det_spec,             # what we already installed
           token_budget=TOKEN_BUDGET,
           iteration_cap=10,
           hot_fallback_on_provider_failure=True,
       )
       return Output(final_status=agentic_result.status, ...)
```

**Properties**:
- **Most runs never invoke the LLM.** If env_detector + base provisioning produces a sandbox where all `observed_first_tokens` exist, we return READY with zero LLM cost.
- **LLM is augmentation, not replacement.** It only addresses specific gaps the deterministic path missed.
- **Hot fallback**: if Sonnet is degraded (rate-limited, 5xx), we surface PARTIAL with `det_spec` results — not BLOCKED. Run continues with whatever deterministic detection found.

### Tools the LLM agent gets

All tools are **strict-input** — they validate args before doing anything. Hallucinated args become tool errors, not state changes.

| tool | signature | constraints |
|---|---|---|
| `read_file(path)` | path: str within `repo/` | path must be inside workspace; max 200KB |
| `list_workflows()` | () → list[str] | returns relative paths; readonly |
| `check_command_available(name, min_version=None)` | name: str | name = single shell-safe token |
| `install_apt(packages, evidence_file, evidence_line)` | packages: list[str], evidence: file+line | packages must be alnum+`-`+`+`; evidence_file must exist; rejected if evidence span doesn't textually contain ANY of the package names |
| `install_pip(packages, evidence_file, evidence_line)` | same shape | same validation |
| `install_via_curl(tool_name, install_url, evidence_file, evidence_line)` | tool_name validates, install_url must be in domain whitelist | whitelist: pypi.org, files.pythonhosted.org, github.com (raw), astral.sh, get.pnpm.io, sh.rustup.rs (extensible) |
| `report_ready(capabilities, observed_token_status)` | capabilities: list[{tool, version, install_method, evidence_ref}], observed_token_status: list[{cmd, first_token, found}] | terminal — exits the loop |
| `report_partial(capabilities, gaps_remaining, reason)` | partial = some installed, some not | terminal |
| `report_blocked(reason, evidence)` | reason: enum | terminal — see §8 for enum values |

**Why evidence is required**: gap #2 from the v1 review. LLMs violate "only install what's evidenced" prompt instructions; we make this a tool-level constraint that can't be bypassed. The tool itself reads the evidence file, checks the package name actually appears at/near the evidence line, and rejects the call otherwise. Tracked in audit log.

### Token budget + memoization

Gap #3 from v1 review.

- **Per-run token budget**: `MAX_SETUP_TOKENS = 50_000` (input + output combined). Enforced by the loop wrapper. Exceeding → return PARTIAL with what installed.
- **Iteration cap**: 10 tool calls (independent of token budget; whichever hits first).
- **Memoization**: before invoking LLM, compute `setup_cache_key = sha256(pyproject.toml + all .github/workflows/*.yml + .pre-commit-config.yaml + tool-versions)`. Look up in `sre_setup_cache` table. If hit AND cached final_status==READY AND ≤24h old, replay the cached install plan deterministically (skip LLM entirely). Cache invalidation on repo change.

```sql
-- New table (alembic migration with the implementation):
CREATE TABLE sre_setup_cache (
  cache_key      VARCHAR(64) PRIMARY KEY,           -- hex sha256
  repo_full_name VARCHAR(255) NOT NULL,
  install_plan   JSONB NOT NULL,                    -- ordered install steps
  final_status   VARCHAR(20) NOT NULL,
  created_at     TIMESTAMPTZ DEFAULT now(),
  hit_count      INTEGER DEFAULT 0
);
CREATE INDEX sre_setup_cache_repo ON sre_setup_cache(repo_full_name, created_at DESC);
```

### Provider-degradation fallback

Gap #4. The loop wrapper maintains a 3-strikes counter:
- Sonnet 5xx / rate-limit / timeout = 1 strike.
- 3 consecutive strikes → break loop, return `final_status=PARTIAL` with `det_spec` + reason `"agentic_unavailable_used_deterministic_only"`.
- Run continues to TL. TL prompt update (see §6) explicitly handles this case.

## 5. System prompt sketch

Tighter than v1, with hard constraints up front:

```
You are the CI Fixer v3 SRE Agent. Your charter: ensure the sandbox can run
the customer repo's failing CI commands.

You are invoked AFTER deterministic env_detector has already run. The base
sandbox has Python + apt baseline + whatever det_spec found in pyproject.toml.

Your job: address the SPECIFIC gaps listed in your inputs.

INPUTS:
  - workspace_path:    repo cloned at PR HEAD (read-only to you)
  - container_id:      sandbox where you exec installs
  - gaps:              list of first-tokens from failing CI commands that
                       are NOT YET available in the sandbox
  - det_spec:          summary of what env_detector already installed
                       (so you don't re-install things)

OBJECTIVE:
  For each token in `gaps`, install it via tools. Then call report_ready
  (or report_partial / report_blocked).

HARD CONSTRAINTS:
  1. Every install_* call REQUIRES an evidence_file + evidence_line pointing
     to where in the repo the package or tool is mentioned. The tool
     verifies the evidence is real; calls without valid evidence fail.

  2. You may NOT install a package not directly evidenced in the repo
     (workflow YAML, pyproject.toml, package.json, .pre-commit-config, etc.).
     "Common Python repos use X" is NOT evidence.

  3. You may NOT run the failing CI commands themselves. That's the next
     agent's job.

  4. You may NOT edit any files in the workspace.

  5. install_via_curl is restricted to a whitelist of installer domains.
     Do not attempt arbitrary URLs.

  6. Token budget: 50000 tokens (input+output combined). Iteration cap:
     10 tool calls. Whichever hits first ends the loop.

ESCALATE (call report_blocked) WHEN:
  - A gap requires GHA-only context (e.g., `${{ matrix.* }}` expansion that
    isn't expanded outside GitHub Actions).
  - A gap requires `services:` (sidecar containers — out of scope for v3).
  - A gap requires sudo for system install but sudo is denied.
  - You discover the workflow uses a `container:` directive (custom upstream
    image — out of scope).
  - You're stuck (same install attempted twice and failed both times).

SELF-RECOVERY (do try):
  - If install_pip fails for "package not found", check if upstream uses
    a different install method (curl script, prebuilt binary).
  - If install_via_curl is blocked by whitelist, see if the same tool is
    pip-installable.
  - One alternative attempt per gap. No more.

OUTPUT:
  Final tool call MUST be one of report_ready / report_partial /
  report_blocked. The loop ignores any non-terminal call after a terminal
  one (defensive).
```

## 6. DAG contract changes

### Task description (cifix_commander → cifix_sre setup)

```json
{
  ...existing fields...,
  "observed_failing_commands": [
    "uvx --with tox-uv tox -e mypy",
    "ruff check ."
  ]
}
```

### Task output schema (cifix_sre setup)

Backwards-compatible — old `env_spec` field stays, new fields ADDED:

```json
{
  "mode": "setup",
  "container_id": "...",
  "workspace_path": "...",
  "env_spec": {...},                           // KEEP — populated by deterministic det
  "capabilities_installed": [
    {"tool": "uv", "version": "0.8.4",
     "install_method": "pip",
     "evidence_ref": ".github/workflows/lint.yml:18"}
  ],
  "setup_log": [
    {"phase": "deterministic", "step": "apt_install_baseline", ...},
    {"phase": "agentic", "tool": "read_file", ...}
  ],
  "final_status": "READY",                     // READY | PARTIAL | BLOCKED
  "blocked_reason": null,
  "observed_token_status": [
    {"cmd": "uvx ...", "first_token": "uvx", "found": true}
  ],
  "tokens_used": 4823,
  "fallback_used": false,                      // true if agentic provider degraded
  "cache_hit": false                           // true if memoized plan replayed
}
```

### Commander handling of final_status

| status | commander action |
|---|---|
| READY | proceed to TL (current path) |
| PARTIAL | proceed to TL with `prior_sre_partial=true` flag in TL task description; TL prompt acknowledges gaps |
| BLOCKED | terminate run with `status=ESCALATED`, `escalation_reason=sre_blocked: <enum>`. No TL attempt, no engineer attempt. |

### TL prompt update (in scope for v1.4.0)

Add to TL system prompt:

```
You may receive a `prior_sre_partial` flag. When this is set, the sandbox
is missing some capabilities upstream CI uses. Inspect `prior_sre_gaps` —
if your diagnosis depends on running a failing_command that uses a missing
capability, set confidence=0.5, list the missing capability in
open_questions, and proceed with best-effort code diagnosis only.

You should NEVER receive a `prior_sre_blocked` indicator — those runs
terminate before reaching you. If you see one anyway, return
confidence=0.0 + open_questions=["unexpected: SRE blocked, run should
have terminated"].
```

## 7. State machine

```
[start]
  → DETERMINISTIC_DETECT  (env_detector.detect_env — current logic)
  → BASE_PROVISION         (apt baseline + det_spec installs)
  → CHECK_GAPS             (any failing first-tokens missing?)
       ↓ no gaps
       → READY (return early, NO LLM call)
       ↓ gaps present
  → MEMOIZE_LOOKUP         (sha256 of setup files; cache hit?)
       ↓ hit (cached plan, recent, READY)
       → REPLAY_PLAN → READY
       ↓ miss
  → AGENTIC_INSTALL        (LLM loop, up to 10 iter / 50k tokens)
       ↓ all gaps closed
       → READY
       ↓ some closed, budget hit
       → PARTIAL
       ↓ provider degraded ≥3 strikes
       → PARTIAL (fallback_used=true)
       ↓ explicit report_blocked
       → BLOCKED
```

## 8. Failure modes & escalation enum

| `blocked_reason` enum value | when |
|---|---|
| `gha_context_required` | workflow uses `${{ matrix.* }}` essential to command execution |
| `services_required` | workflow has `services:` block (sidecar DBs) |
| `custom_container` | workflow has `container:` directive |
| `sudo_denied` | system install needed but sudo unavailable |
| `tool_unavailable` | tool can't be installed via any method (apt + pip + curl all failed) |
| `loop_exhausted` | budget/iteration cap before all gaps closed (when fallback also unavailable) |
| `evidence_missing` | LLM tried to install something with no evidence in the repo |
| `tool_chain_blocked` | install A succeeded but tool A needs B which isn't installable |

## 9. Testing strategy

Three tiers + canary, with **explicit fixtures for the hard cases gap #5 surfaced**.

### Tier-1 (no Docker, no LLM, < 5s)

Tool-level + loop-level unit tests:
- Each tool's input validation (evidence required, domain whitelist, etc.)
- Loop budget enforcement (mock LLM emits 11 tool_use → assert PARTIAL after 10)
- Token budget enforcement (mock LLM that returns 60k-token responses → abort)
- Cache memoization (insert pretend cache row → assert plan replayed without LLM)
- Provider degradation (mock LLM raises 3× → assert fallback path returns PARTIAL with det_spec)
- Decision tree (no gaps → no LLM call ever invoked; mock LLM is verified untouched)

### Tier-2 (real Postgres, mocked LLM, < 30s)

Run full SRE setup task end-to-end with scripted LLM, against fixture repos:

| fixture | expected outcome |
|---|---|
| `python_uv` (setup-uv@v8 + uvx tox) | READY, capabilities=[uv,tox], 1 LLM call |
| `python_pip_only` (vanilla pyproject) | READY, no LLM call (deterministic suffices) |
| `python_poetry` (poetry-based) | READY, capabilities=[poetry], 1 LLM call |
| `node_pnpm` (package.json + setup-pnpm) | READY, capabilities=[node, pnpm] |
| **`monorepo_subdir`** (root workflow with `working-directory: backend/`) | READY, install scoped to backend/ deps |
| **`matrix_narrowed`** (workflow has matrix py 3.10/3.11/3.12, failing run was 3.12) | READY, sandbox uses 3.12 specifically |
| **`composite_action`** (workflow uses local `./.github/actions/setup`) | READY, recursively reads composite, gets uv |
| **`conditional_install`** (`if: matrix.os == 'ubuntu-latest'` block with `apt install`) | READY, installs unconditionally (sandbox is linux) |
| **`gha_required`** (workflow needs `${{ matrix.python-version }}` literal in cmd) | BLOCKED with `gha_context_required` |
| **`custom_container`** (`container: ghcr.io/example:latest`) | BLOCKED with `custom_container` |
| **`hallucination_attempt`** (LLM scripted to install `numpy` with fake evidence_line=999) | install rejected; tool returns evidence-validation error |
| **`provider_degraded`** (LLM mock raises 3× consecutive) | PARTIAL, fallback_used=true |
| **`cache_hit`** (pre-populate sre_setup_cache for fixture's hash) | READY, cache_hit=true, no LLM call |

### Tier-3 (real Docker, mocked LLM, < 2 min, opt-in via `RUN_TIER3=1`)

For three core fixtures (`python_uv`, `python_pip_only`, `python_poetry`), provision a real container, run real install commands, assert tools work end-to-end.

### Canary (real LLM, real Docker, real prod)

Run order:
1. **Internal testbed all 4 cells** — must SHIP unchanged (regression).
2. **Humanize lint cell** — must SHIP in single iter (primary unlock).
3. **One new external Python repo** using poetry or pdm — must SHIP without manual config.
4. **(Stretch)** Re-run testbed with `RUN_NO_DETERMINISTIC=1` env override (force agentic path always) — confirm agentic path also works alone, not just as augmentation.

## 10. Execution plan (phased, with checkpoints)

### Phase 0 — Tools + validation (Day 1, ~3-4 hours)
- [ ] Tool implementations with strict input validation
- [ ] Evidence-checking helper (read file, find package name in line span)
- [ ] Domain whitelist for install_via_curl
- [ ] Tier-1 tests for each tool's validation
- [ ] **Checkpoint**: tier-1 tools green; manually call from REPL works

### Phase 1 — Loop + LLM wiring (Day 1-2, ~4 hours)
- [ ] `run_sre_setup_subagent` modeled on `run_coder_subagent`
- [ ] Sonnet provider call (reuse `build_sonnet_coder_callable` pattern)
- [ ] Token budget + iteration cap + provider-strikes counter
- [ ] System prompt v2 finalized
- [ ] Tier-1 tests with scripted LLM (incl. hallucination, degradation, exhaustion)
- [ ] **Checkpoint**: scripted-LLM tests pass; output schema validates

### Phase 2 — Memoization + cache (Day 2, ~2 hours)
- [ ] Alembic migration: `sre_setup_cache` table
- [ ] Cache key computation (sha256 of relevant files)
- [ ] Cache lookup before LLM invocation; replay on hit
- [ ] Cache write on READY
- [ ] **Checkpoint**: tier-2 cache_hit fixture passes

### Phase 3 — Hybrid integration (Day 2, ~3 hours)
- [ ] Update `cifix_sre._execute_setup` with hybrid decision tree
- [ ] Backwards-compatible Task.output schema
- [ ] Commander handling of PARTIAL/BLOCKED
- [ ] TL prompt updates in `cifix_techlead.py`
- [ ] Update existing tier-1+tier-2 tests for new schema
- [ ] **Checkpoint**: existing harness still green; new schema tests pass

### Phase 4 — Validation (Day 2-3, ~4 hours)
- [ ] All 13 tier-2 fixtures green
- [ ] Tier-3 (opt-in) green
- [ ] Local end-to-end run on fixture repos
- [ ] **Checkpoint**: success criteria measurement script ready

### Phase 5 — Deploy + canary (Day 3, ~3 hours)
- [ ] Tag v1.4.0
- [ ] Deploy via `deploy.sh` (runs alembic migration)
- [ ] Verify schema in prod + agent code shipped
- [ ] Run canary sequence (testbed 4/4 → humanize → new external)
- [ ] **Checkpoint**: success criteria from §11 all met

### Phase 6 — Operate + observe (Day 3, ~2 hours)
- [ ] Add structured trace per SRE setup run (existing `setup_log` extended)
- [ ] Simple SQL views for observability:
  - "% runs reaching READY/PARTIAL/BLOCKED last 7d"
  - "Top 10 install methods used"
  - "Cache hit rate"
  - "Fallback rate (provider degraded)"
- [ ] Update memory + website changelog v1.4.0

## 11. Success criteria — **numeric and measurable**

Gap #7 from v1 review. All 7 must be met to declare v1.4.0 ready:

| # | criterion | measurement | target |
|---|---|---|---|
| 1 | Internal Python regression | testbed 4/4 cells SHIP across 5 consecutive runs | 4/4 × 5 |
| 2 | Humanize lint single-iter | task chain ≤ 4 tasks; only src/ touched | confirmed |
| 3 | New external Python repo SHIPS | one new repo (poetry, pdm, or rye-based) commits successfully | confirmed |
| 4 | Reliability — terminal state rate | % SRE tasks reaching READY/PARTIAL/BLOCKED (not stuck/error) | ≥ 95% |
| 5 | Reliability — false install rate | % installed packages without valid repo evidence | < 1% |
| 6 | Cost overhead vs deterministic | wall-clock delta on no-gap runs (where LLM never invoked) | ≤ 5% |
| 7 | Cost overhead with gaps | dollar cost per run with LLM invocation | < $0.20 avg |
| 8 | TL contract clean | TL produces confidence=0.0 for env reasons in 10 consecutive canary runs | 0 occurrences |

## 12. Open questions deferred to implementation

1. **Provider choice**: Sonnet vs Haiku? Sonnet for now — cost/latency comparable, reasoning headroom helpful. A/B with Haiku in v1.4.1 if cost dominates.
2. **Cache TTL**: 24h proposed. Could be longer (7d) given setup file changes are rare. Start with 24h, observe.
3. **Cache key vs version**: should `python>=3.11` in pyproject vs `python>=3.12` invalidate cache? Currently yes (any setup-file change invalidates). May be over-aggressive; observe.
4. **Composite action recursion**: how deep? Cap at 3 levels.
5. **Network egress logging**: log every install_* outbound call for SOC2 / supply chain review later.

## 13. Appendix — example traces

### Example A — fast path, no LLM (most runs)

```
Phase: deterministic_detect
  env_detector.detect_env(workspace) → spec={python_version: 3.12, system_deps: [], install_commands: ["pip install -e .[dev]"]}

Phase: base_provision
  apt_install_baseline → ok
  pip install -e .[dev] → ok (3s)

Phase: check_gaps
  observed_failing_commands = ["ruff check ."]
  first_tokens = ["ruff"]
  check_command_available("ruff") → found, version 0.4.1
  gaps = []

Phase: ready
  final_status=READY, tokens_used=0, cache_hit=false (key written)
```

### Example B — humanize, with LLM augmentation

```
Phase: deterministic_detect → spec={python_version: 3.12, system_deps: [], install_commands: ["pip install -e ."]}
Phase: base_provision → ok
Phase: check_gaps → gaps=["uvx"] (workflow uses uvx, env_detector missed it)
Phase: memoize_lookup → MISS

Phase: agentic_install (Sonnet)
  Tool: list_workflows → [".github/workflows/lint.yml", "test.yml", ...]
  Tool: read_file(".github/workflows/lint.yml") → <yaml with setup-uv@v8>
  Tool: install_pip(["uv"], evidence_file=".github/workflows/lint.yml", evidence_line=18)
        → evidence valid (line 18: "uses: astral-sh/setup-uv@v8.0.0"), exit 0
  Tool: check_command_available("uv") → found, 0.8.4
  Tool: check_command_available("uvx") → found (bundled with uv)
  Tool: report_ready(capabilities=[{tool:"uv", version:"0.8.4", install_method:"pip", evidence_ref:".github/workflows/lint.yml:18"}])

Result: final_status=READY, tokens_used=~4500, cache_key written
Total wall: ~30s, cost ~$0.05
```

### Example C — blocked (custom container)

```
Phase: deterministic → spec
Phase: base_provision → ok
Phase: check_gaps → gaps=["python3.12-something-weird"]
Phase: agentic_install
  Tool: list_workflows → ["test.yml"]
  Tool: read_file("test.yml") → <yaml with `container: ghcr.io/example/ci:v1`>
  Tool: report_blocked(reason="custom_container",
                      evidence={"file":".github/workflows/test.yml","line":12})

Result: final_status=BLOCKED, run terminated, escalation_reason="sre_blocked: custom_container"
```
