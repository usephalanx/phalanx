# CI Fixer v2 — Live-Run Playbook

This document is for **live execution** of the CI Fixer v2 simulation
against real fixtures with real LLMs. The scripted integration test
([tests/integration/ci_fixer_v2/test_e2e_seed_corpus.py](../tests/integration/ci_fixer_v2/test_e2e_seed_corpus.py))
exercises the whole stack end-to-end without API keys or docker — run
that first to prove the wiring.

Live-run is different: it calls GPT-5.4 + Claude Sonnet 4.6, spins
sandbox containers, clones real repos, and measures the MVP exit gates
on a real corpus. It also costs real money.

---

## Prerequisites

### Secrets + env vars
- `OPENAI_API_KEY` — for the main agent (GPT-5.4)
- `ANTHROPIC_API_KEY` — for the coder subagent (Claude Sonnet 4.6)
- `GH_TOKEN` — read-only personal access token or GitHub App token, for
  corpus harvest and PR metadata during simulation runs
- `DATABASE_URL`, `REDIS_URL` — matches production (Postgres 16 +
  pgvector extension + Redis 7)

### Infrastructure
- **Docker daemon reachable** (sandbox-only validation per audit N3).
  Sandbox provisioning uses the same image set as v1
  (`phalanx-sandbox:python`, `:node`, `:multi`). `sandbox_enabled` must
  be `True` in settings.
- **N1 worker split deployed** — `phalanx-ci-fixer-worker` service from
  [docker-compose.prod.yml](../docker-compose.prod.yml) is up. Docker
  socket must be scoped to that worker only; the general
  `phalanx-worker` must NOT have it (audit item N1).
- **Migrations at head** — apply `20260419_0001` + `20260419_0002` to
  add `MemoryFact.agent_role` and `CIFixRun.cost_breakdown_json`
  respectively. Without `agent_role`, CI Fixer memory cross-contaminates
  engineering-agent memory (audit item F).

### Model identifiers (sanity check before first live run)
`settings.openai_model_reasoning_ci_fixer` defaults to `"gpt-5.4"`; if
your OpenAI SDK rejects that model name at call time, update the
setting (it's a one-line config change — no code rewrite). Same applies
to `settings.anthropic_model_ci_fixer_coder` (`"claude-sonnet-4-6"`).

---

## Step 1 — Harvest the corpus

Seed fixtures at [tests/simulation/fixtures/python/](../tests/simulation/fixtures/python/)
(3 hand-crafted) let you exercise the scoring harness immediately. For
MVP exit gates you need **~80 fixtures per language** across the 4
failure classes (spec §11).

Harvest per language × failure class:

```bash
export GH_TOKEN=ghp_...

# Python / lint — 20 fixtures from astral-sh/ruff
python scripts/harvest_ci_fixtures.py \
    --repo astral-sh/ruff \
    --language python \
    --failure-class lint \
    --days 30 \
    --limit 20

# Python / test_fail — pytest project itself is a good source
python scripts/harvest_ci_fixtures.py \
    --repo pytest-dev/pytest \
    --language python \
    --failure-class test_fail \
    --days 60 \
    --limit 20

# Python / flake — fastapi or pandas
python scripts/harvest_ci_fixtures.py \
    --repo tiangolo/fastapi \
    --language python \
    --failure-class flake \
    --days 60 \
    --limit 20

# Python / coverage — pick an actively maintained repo with a coverage gate
python scripts/harvest_ci_fixtures.py \
    --repo pallets/flask \
    --language python \
    --failure-class coverage \
    --days 60 \
    --limit 20
```

Each harvested fixture:

- Runs every text payload through [phalanx/ci_fixer_v2/simulation/redaction.py](../phalanx/ci_fixer_v2/simulation/redaction.py) before writing.
- Skips repos with GPL-class licenses (see `_INCOMPATIBLE_LICENSE_KEYS`).
- Records redaction + license in `meta.json` for audit.

Repeat per language (JavaScript/TypeScript, Java, C#). Target repos
listed in [docs/ci-fixer-v2-spec.md §11](ci-fixer-v2-spec.md).

---

## Step 2 — Run the simulation

Once you have 80+ fixtures per language:

```bash
python scripts/run_simulation_suite.py \
    --language python \
    --output-dir build/simulation/python \
    --fail-on-gate
```

What this does:

1. Iterates every fixture under `tests/simulation/fixtures/python/`.
2. For each: reconstructs the PR state (via `clone_instructions`),
   provisions a sandbox, builds `AgentContext`, runs the full v2 agent
   loop with live GPT-5.4 + Sonnet 4.6, scores against ground truth.
3. Aggregates into a `Scoreboard` (per language × failure_class).
4. Writes `scoreboard.json` + `scoreboard.md` to `--output-dir`.
5. Exits 1 if `--fail-on-gate` is set and MVP gates fail (for CI).

**Note:** the `live_runner` inside `scripts/run_simulation_suite.py`
raises `NotImplementedError` today; the concrete wiring from fixture →
DB-backed `CIFixRun` → `execute_v2_run` → outcome-reload-from-DB is the
one remaining piece. It's ~40 lines and depends on a per-fixture
`CIFixRun` seeder — decide at first live-run kickoff whether to seed
through the webhook endpoint or a direct SQL insert.

---

## Step 3 — Read the scoreboard

MVP exit gates (spec §12):

- **Lenient** pass rate **≥ 95%** per (language, failure_class)
- **Behavioral** pass rate **≥ 99%** per (language, failure_class)

Both must hold for a language to ship. `Strict` is informational only;
authors' fix styles vary.

If any row fails the gate:

1. Identify the common failure mode from the fixture-level traces (each
   run writes an `AgentTrace` timeline).
2. Classify by cause:
   - **Diagnosis**: agent couldn't find the failing file/line. Fix:
     sharpen diagnosis tool prompts or add a missing tool.
   - **Fix strategy**: agent picked the wrong action class. Fix: system
     prompt, or add guardrails (e.g., "preexisting failures must be
     declined with `preexisting_main_failure`").
   - **Sandbox**: the verification command didn't pass. Fix: sandbox
     image + env-setup prompt (see Phase 4 GPT env setup).
   - **Coder failure**: Sonnet couldn't apply a working patch. Fix:
     improve the coder system prompt or `delegate_to_coder` schema.
3. Iterate: change the tool/prompt/gate, rerun the simulation subset,
   measure again.

**Do not** declare the MVP complete until every row shows `PASS` in the
scoreboard. Per Raj's operating rules: "we will iterate until we have a
10/10 system."

---

## Step 4 — Cutover

Once all languages pass gates for a stable window (≥ 2 weeks of nightly
scoreboard showing green):

1. Flip `settings.phalanx_ci_fixer_v2_enabled` → `True` (or set
   `PHALANX_CI_FIXER_V2_ENABLED=1` in `.env.prod`).
2. Webhook handler at
   [phalanx/api/routes/ci_webhooks.py](../phalanx/api/routes/ci_webhooks.py)
   gets a one-line conditional that dispatches to `execute_v2_run` when
   the flag is on. (This conditional lands in Phase 2; spec §14.)
3. Legacy `phalanx/ci_fixer/` module + `phalanx/agents/ci_fixer.py`
   stay as fallback for one more release.
4. After another stable week, delete the legacy module entirely. No
   permanent dual pipeline.

---

## Cost expectations

Per fixture (rough):

- Diagnosis + reasoning turns (GPT-5.4): ~8k–15k input tokens, 500–2k
  output tokens, 2k–8k reasoning tokens.
- Coder subagent (Sonnet 4.6): ~5k–10k input tokens, 500–1k output
  tokens, 2k–4k thinking tokens.

At spec §9 prices (in USD per 1M tokens):

- GPT: $3 input / $15 output / $15 reasoning
- Sonnet: $3 input / $15 output / $15 thinking

Median per-fixture cost ≈ $0.15–$0.40. A full 80-fixture Python pass ≈
$15–$30. All four languages: $60–$120 per full sweep. Nightly CI at
this cost is sustainable; if the median cost per *merged* PR exceeds
$1, that's the primary signal to tune `reasoning_effort` down or
constrain the main-agent tool set.

---

## Troubleshooting

**`sandbox_not_provisioned`** — `sandbox_enabled=False` or Docker socket
unreachable. The only fallback is `escalate(infra_failure_out_of_scope)`
per audit N3; there is NO local-subprocess fallback for validation.

**`pgvector` errors on first `MemoryFact` insert** — the pgvector
extension is already present in the initial migration
([alembic/versions/20260317_0001_initial_schema.py:22-25](../alembic/versions/20260317_0001_initial_schema.py#L22-L25));
verify `CREATE EXTENSION` ran on the target Postgres host (audit item B).

**Cost overruns** — watch `CIFixRun.cost_breakdown_json` on recent runs.
If the median exceeds $1/PR, lower `reasoning_effort` from `medium` to
`low` in the main-agent callable (main agent's job is orchestration,
not deep reasoning on most turns).

**Provider outage mid-run** — the loop's provider adapters return
`stop_reason=max_tokens` with `text="provider_error: ..."` which the
main loop treats as implicit stop → clean escalation. No crash, no
silent failure.
