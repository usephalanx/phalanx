# v1.7.1 — Provisioning Tiers

**Status**: spec lock (2026-05-02). Builds on the v1.7.0 SRE split. Same discipline as v1.7 spec — lock scope, then build.

**Origin**: research from 2026-05-02 (3 reports — production sandbox architectures, env provisioning patterns, security/observability/failure modes). Key finding from SWE-rebench: scaled to 21k tasks via two-call LLM pipeline + execution validation, with 7,500/21,000 (~36%) prebuilt deterministically. Cognition Devin's lesson: keeping environments current consumed >75% of dedicated engineering. Don't be Devin. Be SWE-bench.

**Premise**: today's `cifix_sre_setup` is fully agentic (Sonnet loop, $4 budget). Research shows 80% of repos have a usable workflow recipe in `.github/workflows/`, and another 10% can be detected by lockfile fingerprint. Only ~10% genuinely need the agent. Building the deterministic tiers cuts setup cost ~20× and reduces latency proportionally.

**Definition of Done — binary**:
- [ ] Tier 0 (workflow YAML extractor) lands setup commands for testbed + humanize
- [ ] Tier 1 (lockfile fingerprint) lands setup for repos without workflow YAML
- [ ] Cache layer hit-rate ≥ 90% on second run of same fixture
- [ ] Tier 2 (agentic) budget reduced from $4 → $0.50; only triggered on Tier 0/1 failure
- [ ] Average setup cost on testbed ≤ $0.40 (down from current ~$2.50)

---

## The 3 tiers + cache

```
                 +-----------+
   ci_context →  |   CACHE   | hit  → return cached recipe
                 +-----+-----+
                       | miss
                       ↓
                 +-----------+
                 |  TIER 0   |  parse .github/workflows/<job>.yml
                 |  workflow |  render to deterministic shell commands
                 |  extract  |
                 +-----+-----+
                       | failed (no workflow / parse error / unsupported actions)
                       ↓
                 +-----------+
                 |  TIER 1   |  detect by lockfile presence
                 |  lockfile |  uv.lock → uv sync, poetry.lock → poetry install, ...
                 |  detect   |
                 +-----+-----+
                       | failed (no recognized lockfile)
                       ↓
                 +-----------+
                 |  TIER 2   |  agentic Sonnet loop (existing v1.4.0)
                 |  agentic  |  budget reduced $4 → $0.50
                 |  fallback |  receives Tier 0/1's failed attempt as evidence
                 +-----+-----+
                       | success (any tier)
                       ↓
                 PERSIST RECIPE TO CACHE
```

---

## Tier 0 — workflow YAML extraction

**Module**: `phalanx/agents/_v171_workflow_extractor.py`

**Inputs**: workspace_path, failing_job_name (from ci_context)

**Output**: `WorkflowRecipe` dataclass with rendered shell commands

**Algorithm**:
1. Find `.github/workflows/*.yml` files in workspace
2. For each, parse YAML; for each job, check if its `name` (or job key) matches `failing_job_name`
3. For the matching job:
   - Extract `runs-on` (informational)
   - Walk `steps[]`, classify each:
     - `uses: actions/checkout@*` — skip (we have the workspace)
     - `uses: actions/setup-python@*` with version → render `python<version>` install or use system python
     - `uses: astral-sh/setup-uv@*` → render uv install (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
     - `uses: actions/setup-node@*` → render node version setup
     - `uses: actions/cache@*` — skip (no cache layer here yet)
     - `run: <shell>` — capture verbatim
   - Optionally include the failing `run` step itself (acts as `reproduce_command`)
4. Return ordered list of shell commands

**Supported `uses:` actions** (initial set, ranked by frequency in OSS Python repos):
- `actions/checkout` (skip)
- `actions/setup-python` (render or skip if system python matches)
- `astral-sh/setup-uv`
- `actions/setup-node`
- `actions/cache` (skip)
- `actions/upload-artifact` (skip — verify path)
- `actions/download-artifact` (skip)
- `pre-commit/action` (handle separately — render `pip install pre-commit && pre-commit run`)

**Failure modes** (return `None`, fall through to Tier 1):
- No workflow files found
- No job with matching name
- Job uses an unsupported `uses:` action critical to setup (e.g., custom org action that does heavy lifting)
- YAML parse error
- `${{ }}` template literals in critical paths the renderer can't resolve

**Tests** (tier-1):
- Parse real workflow YAMLs from testbed, humanize, fastapi, pydantic
- Each must produce a usable shell command list
- Reject malformed YAML cleanly
- Skip unsupported `uses:` actions with clear "tier_0_unsupported" reason

---

## Tier 1 — lockfile fingerprint

**Module**: `phalanx/agents/_v171_lockfile_detect.py`

**Algorithm** (priority order — first match wins):

| Detection | Install command |
|---|---|
| `uv.lock` exists | `uv sync --frozen` (or `uv sync` if frozen fails) |
| `poetry.lock` + `pyproject.toml` with `[tool.poetry]` | `poetry install` |
| `Pipfile.lock` | `pipenv sync` |
| `pixi.toml` + `pixi.lock` | `pixi install` |
| `pyproject.toml` with `[project]` + `[project.optional-dependencies].dev` | `pip install -e .[dev]` |
| `pyproject.toml` with `[project]` only | `pip install -e .` |
| `requirements-dev.txt` | `pip install -r requirements-dev.txt` |
| `requirements.txt` | `pip install -r requirements.txt` |
| `setup.py` (legacy) | `pip install -e .` |

**Tests**:
- Each detection rule with a minimal fixture
- Priority-order tests (uv.lock should win over pyproject.toml)
- "no recognized files" → return `None`

---

## Cache layer

**Module**: `phalanx/agents/_v171_setup_cache.py`

**Backing store**: per-repo JSONL file at `{settings.git_workspace}/_v171_setup_cache/{repo_hash}.jsonl`. SQLite later if we hit concurrency issues.

**Cache key**:
```python
sha256(
    repo_full_name.encode()
    + workflow_path.encode()  # or "" if no workflow
    + hash_of_files(["pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock",
                     "pnpm-lock.yaml", "requirements.txt", "requirements-dev.txt"])
)
```

**Cache value**: `SetupRecipe` JSON
```python
{
    "tier": "0" | "1" | "2",
    "commands": ["uv sync --frozen", "..."],
    "produced_at": "2026-05-02T...",
    "source": "workflow:.github/workflows/test.yml::test"
              | "lockfile:uv.lock"
              | "agent:tier2",
    "validated": bool,           # did this recipe successfully provision?
    "validation_evidence": {     # last successful run's signal
        "exit_codes": [0, 0],
        "duration_ms": 12345,
    }
}
```

**API**:
```python
def lookup(ci_context) -> SetupRecipe | None
def store(ci_context, recipe: SetupRecipe) -> None
def invalidate(ci_context, reason: str) -> None  # called when Tier 2 patches a stale recipe
```

**Tests**:
- Hit / miss / invalidate flows
- Hash stability across runs
- Concurrent-write safety (file locking or SQLite WAL)

---

## Tier 2 — agentic fallback (changes)

**Existing module**: `phalanx/ci_fixer_v3/sre_setup/loop.py` (v1.4.0)

**Changes for v1.7.1**:
1. ~~Budget cap: `$4` → `$0.50`~~ — **No change needed.** Audit revealed existing
   `MAX_SETUP_TOKENS = 50_000` already keeps each invocation under ~$0.20 on Sonnet
   (per the v1.4.0 design doc). The architecture-gaps doc's "$4 → $0.50" framing
   was based on research-level cost matrices; actual code was always tighter.
2. Receive Tier 0/1's failed-attempt as input context: when commander's
   `_select_env_spec_v171` returns a non-cache tier, the agent's seed prompt
   already gets `det_spec_summary` (the chosen install_commands) — same shape,
   different source. Existing prompt language ("close the GAPS the determinist
   couldn't") works for both cases.
3. On success: write-back to `_v171_setup_cache` happens in `cifix_sre._execute_setup`
   AFTER the agentic gap-fill returns READY. Per-tier provenance is in `v171_tier`
   on the Task.output JSON.
4. On failure: cache.invalidate would be useful but is deferred — for v1.7.1
   we just don't write a recipe entry; next run re-tries from scratch.

---

## SRE setup wiring

**Module**: `phalanx/agents/cifix_sre.py` (`_execute_setup`)

**Algorithm**:
```python
async def _execute_setup(ci_context, integration):
    # 1. Cache lookup
    recipe = setup_cache.lookup(ci_context)
    if recipe and recipe.validated:
        return execute_recipe_in_container(recipe, container)  # ~5s

    # 2. Tier 0 — workflow YAML
    if not recipe:
        recipe = workflow_extractor.extract(workspace, failing_job_name)
    if recipe:
        result = try_execute_recipe(recipe, container)
        if result.ok:
            setup_cache.store(ci_context, recipe.with_validation(result))
            return result
        tier_0_failure = result

    # 3. Tier 1 — lockfile fingerprint
    if not recipe or not result.ok:
        recipe = lockfile_detect.detect(workspace)
    if recipe:
        result = try_execute_recipe(recipe, container)
        if result.ok:
            setup_cache.store(ci_context, recipe.with_validation(result))
            return result
        tier_1_failure = result

    # 4. Tier 2 — agentic fallback (existing v1.4.0)
    return await sre_setup_subagent.run(
        ci_context, container,
        prior_attempts={"tier_0": tier_0_failure, "tier_1": tier_1_failure},
        max_cost_usd=0.50,
    )
```

---

## Phase breakdown

| Phase | Scope | Time |
|---|---|---|
| 1.1 | Tier 0 workflow extractor + tier-1 tests on testbed/humanize/fastapi YAMLs | 2 days |
| 1.2 | Tier 1 lockfile detector + tier-1 tests covering 9 detection rules | 1 day |
| 1.3 | Cache layer (per-repo JSONL) + tier-1 tests for hit/miss/invalidate | 1 day |
| 1.4 | Wire into `cifix_sre._execute_setup`; reduce Tier 2 budget; thread evidence through | 1 day |
| 1.5 | Tier-2 integration tests (synthetic ci_context → cache miss → Tier 0 → ship) | 1 day |
| 1.6 | Telemetry + per-tier cost metric in `Task.output` | 0.5 day |

**Total: ~6.5 days** focused work. Same time-box as v1.7.0 phase.

---

## Out of scope (explicit non-goals)

- **Sandbox hardening** (egress proxy, gVisor, resource caps) — separate v1.7.2 ticket; deploy-side work, different concern
- **Cross-language detection beyond Python** (Go/Rust/Node) — Tier 1 detector handles their lockfiles structurally but we don't ship per-language test corpus until v1.8
- **Recipe sharing across repos** — per-repo cache only; no global recipe library
- **LLM-extracted recipes a la SWE-rebench** — for novel-repo first-encounter, we use the agentic Tier 2 (one Sonnet call, narrow patch). SWE-rebench's two-call extraction pipeline is a v2.0 conversation
- **Workflow YAML matrix expansion** (`strategy.matrix.python-version: [3.10, 3.11, 3.12]`) — pick first entry; defer matrix-aware extraction
- **Composite actions / reusable workflows** — bail out to Tier 1 when encountered

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Tier 0 extractor misclassifies a critical step | med | high | tier-1 tests against 5+ real workflows; agentic Tier 2 still catches |
| Cache returns stale recipe when deps changed | med | med | hash includes lockfile content; auto-invalidates on content change |
| Per-repo JSONL cache hits concurrency issues | low | med | switch to SQLite with WAL if observed; not blocking initial ship |
| Tier 2's reduced budget ($0.50) is insufficient for novel repos | med | med | budget escapes — `$0.50` is a soft cap, escalate ticket if exceeded |
| `${{ matrix.* }}` template literals leak into Tier 0 commands | high | low | Bug #14 lesson — already have detection; either skip step or fall through |

---

## Success metric

After v1.7.1 ships, measure on testbed:
- **Setup cost p50**: target ≤ $0.20 (baseline ~$2.50)
- **Setup latency p50**: target ≤ 30s (baseline ~3min)
- **Tier-2 hit rate**: target ≤ 15% (baseline 100%)
- **Cache hit rate** (after 2nd run on same repo): target ≥ 90%

If any number misses, we have signal about which tier to invest in first.
