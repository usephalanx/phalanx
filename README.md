# Phalanx

**Prompt in. Shipped app out.**

Phalanx is an open-source AI engineering team. Specialized agents coordinate from planning to production — with human approval at every gate. Self-hostable. Config-driven. No vendor lock-in.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg)](docker-compose.yml)

---

## Demo

> `/phalanx build "Create a Hello World REST API"` → agents plan, build, test, review, deploy → live URL in under 5 minutes.

[![Watch the demo](https://usephalanx.com/demo.mp4)](https://usephalanx.com)

Browse apps built by Phalanx: **[demo.usephalanx.com](https://demo.usephalanx.com)**

---

## How It Works

```
/phalanx build "Add JWT auth"
        ↓
Commander → Planner → Builder → Reviewer → QA → Security → Release → SRE
        ↓                                                       ↓          ↓
  [Approve Plan?]                                        [Ship to prod?]  Live URL
```

1. **Delegate** — send a task from Slack or the REST API
2. **Approve** — review the implementation plan before any code is written
3. **Ship** — agents build, test, review, and open a PR. You approve the merge.
4. **Deploy** — SRE agent generates a Dockerfile, deploys the app, returns a live URL

---

## The Agents

| Agent | Role | Model |
|-------|------|-------|
| **Commander** | Accepts work orders, drives the run end-to-end, manages state transitions | GPT-4.1 |
| **Planner** | Decomposes tasks into ordered steps with file paths, function names, and test cases | GPT-4.1 |
| **Builder** | Writes code, creates files, commits to an isolated feature branch. Generates QA.md recipe on final task. | Claude Sonnet 4.6 |
| **Reviewer** | Reviews all commits for quality, correctness, and project patterns. Verdict: APPROVED / CHANGES_REQUESTED / CRITICAL_ISSUES | GPT-4.1 |
| **QA** | Reads QA.md recipe, installs deps, runs test suite, measures coverage delta. Blocks if coverage drops. | GPT-4.1 |
| **Security** | Runs detect-secrets, bandit, pip-audit, optional Trivy. Blocks on any critical finding. | — |
| **Release** | Opens the PR, tags the release, runs health checks. Run holds until you approve the merge. | — |
| **SRE** | Generates a Dockerfile for the built app, deploys it to the demo server, configures nginx routing. Returns a live URL. | GPT-4.1 |
| **CI Fixer (v2)** | Agent + tools + loop. Reads real CI logs, queries fingerprint memory, delegates code changes to the Sonnet coder subagent, verifies the fix in a sandbox container, commits + pushes only after sandbox exit 0. Hard verification gate — never commits a fix that didn't run green in sandbox. | GPT-5.4 (main) + Sonnet 4.6 (coder) |
| **Prompt Enricher** | Detects vague or ambiguous work orders and resolves intent before planning begins. | GPT-4.1 |

---

## What Ships with Every Run

- **Isolated workspace** — each run clones into `/tmp/forge-repos/{slug}-{run_id[:8]}/`. No cross-run contamination.
- **QA.md recipe** — builder generates a machine-readable test recipe (stack, runner, install steps, coverage threshold). QA follows it exactly.
- **Agent traces** — every agent decision is recorded. Inspect at `GET /runs/{run_id}/trace`.
- **Live demo URL** — SRE deploys the finished app to `demo.usephalanx.com/{slug}` after every successful run.

---

## CI Fixer v2

The CI Fixer is its own architecture: **single agent + tools + loop**, not a pipeline. When a PR's CI fails, the agent is given the failing log and a set of tools; it decides each step.

```
                           ┌────────────────────────┐
  CI webhook / simulate →  │    Main agent          │   GPT-5.4 (reasoning)
                           │  diagnose → decide     │   ──────────────────
                           │  → act → verify        │   Tools:
                           │  → coordinate          │    fetch_ci_log
                           └──────────┬─────────────┘    get_pr_context / diff
                                      │                  query_fingerprint
                      delegate_to_coder                  read_file / grep / glob
                                      ▼                  git_blame
                           ┌────────────────────────┐    get_ci_history
                           │   Coder subagent       │    run_in_sandbox
                           │  patch → verify loop   │    delegate_to_coder
                           └──────────┬─────────────┘    comment_on_pr
                                      │                  commit_and_push
                            apply_patch + verify         open_fix_pr
                                      ▼                  escalate
                           ┌────────────────────────┐   ──────────────────
                           │   Sonnet 4.6 coder     │   Coder tools:
                           │  (docker sandbox)      │    read_file, grep,
                           └────────────────────────┘    apply_patch,
                                                         run_in_sandbox
```

**Hard invariants** (enforced in the loop, not the tool):
- `commit_and_push` is blocked unless `run_in_sandbox` has executed the ORIGINAL failing command and seen exit 0.
- `apply_patch` auto-syncs the changed files into the sandbox so verification runs against the patched state, not a stale `docker cp` snapshot.
- Every tool call, LLM call, and git subprocess is bounded by `asyncio.wait_for` — no single stuck call can hang the run.
- If `fetch_ci_log` fails, the agent escalates `infra_failure_out_of_scope` rather than reasoning from partial data.
- `preexisting_main_failure` escalation requires concrete `get_ci_history` evidence on the default branch.

**Sandbox / CI parity.** The agent inspects the repo's `pyproject.toml` / `package.json` / CI workflow and mirrors CI's install steps inside the sandbox (e.g. `pip install -e ".[dev]"`) before running the validator. This catches failures that old-baked-image versions of ruff/pytest/eslint would miss.

**Simulate CLI.** Skip the webhook roundtrip when debugging:

```bash
docker exec phalanx-prod-phalanx-ci-fixer-worker-1 \
  python -m phalanx.ci_fixer_v2.simulate \
    --repo owner/name --pr 42 \
    --branch fix/my-branch --sha abc1234 \
    --job-id 123456789 --failing-command "ruff check ."
```

Runs the full agent loop in-process against real LLMs + real sandbox + real GitHub. Exit 0 if the agent committed, 1 if escalated.

**Preflight diag.**

```bash
python -m phalanx.ci_fixer_v2.diag [--repo owner/name]
```

Checks every dependency (env vars, OpenAI reachable, Anthropic reachable, DB migrations at head, Redis, Docker daemon, sandbox images present) before a live run.

---

## Self-Hosting

### Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | Compose v2 required |
| Python 3.11+ | For local dev and migrations |
| Anthropic API key | Builder agent (Claude) |
| OpenAI API key | Commander, Planner, Reviewer, SRE (GPT-4.1) |
| GitHub token (`repo` scope) | git push + PR creation |
| Slack app (socket mode) | Optional — can use REST API instead |

### Quick Start

```bash
# 1. Clone
git clone https://github.com/usephalanx/phalanx.git
cd phalanx

# 2. Configure
cp .env.example .env
# Required: ANTHROPIC_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN
# Optional: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID

# 3. Start infra
docker compose up postgres redis -d

# 4. Run migrations
source .venv/bin/activate  # or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
python -c "
from alembic.config import Config; from alembic import command
cfg = Config(); cfg.set_main_option('script_location', 'alembic')
cfg.set_main_option('sqlalchemy.url', 'postgresql+psycopg2://phalanx:phalanx@localhost:5432/phalanx')
command.upgrade(cfg, 'head')
"

# 5. Start the full stack
docker compose up -d

# 6. Verify
curl http://localhost:8000/health
# → {"status":"ok","db":"ok","redis":"ok"}
```

### Send Your First Work Order

From Slack:
```
/phalanx build "Create a FastAPI app with /health and /hello endpoints and pytest tests"
```

Via REST API (no Slack required):
```bash
curl -X POST http://localhost:8000/work-orders \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Hello World FastAPI",
    "description": "FastAPI app with /health and /hello endpoints. Write pytest tests.",
    "project_id": "my-project"
  }'
```

### Production Deploy

Build locally, ship to your server — no `docker build` on the server:

```bash
export DEPLOY_HOST="ubuntu@your-server-ip"
export DEPLOY_KEY="~/.ssh/your-key.pem"

./deploy.sh           # auto-bump patch version
./deploy.sh v1.4.0    # explicit version
./deploy.sh --migrate-only
```

> **Required:** Set `PHALANX_WORKER=1` on every Celery worker container. Without it, the SQLAlchemy pool binds to the first event loop and all subsequent tasks fail with "Future attached to a different loop".

---

## Configuration

All agent behavior is configured in YAML — no code changes needed.

```yaml
# configs/team.yaml
agents:
  builder:
    model: claude-sonnet-4-6
    max_tokens: 20000
    skills:
      - python
      - fastapi
      - pytest
      - react
      - typescript
  reviewer:
    model: gpt-4.1
    max_tokens: 4096

approval_gates:
  plan: true       # require approval before building
  ship: true       # require approval before merging
  release: true    # require approval before deploying
```

---

## CI Webhooks

Phalanx receives CI failure webhooks and automatically dispatches the CI Fixer agent.

```yaml
# .github/workflows/ci.yml
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]

jobs:
  notify-phalanx:
    if: github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -X POST https://your-phalanx.com/webhook/github \
            -H "Content-Type: application/json" \
            -d '{"run_id": "${{ github.event.workflow_run.id }}", "repo": "${{ github.repository }}"}'
```

Supported providers: GitHub Actions, Buildkite, CircleCI, Jenkins.

---

## Workflow States

Every run follows a deterministic 16-state machine. Transitions are enforced in code — no run can skip a state.

```
INTAKE → RESEARCHING → PLANNING → [AWAITING_PLAN_APPROVAL]
       → EXECUTING → VERIFYING  → [AWAITING_SHIP_APPROVAL]
       → READY_TO_MERGE → MERGED → RELEASE_PREP
       → [AWAITING_RELEASE_APPROVAL] → SHIPPED

Error states: FAILED · ESCALATING · CANCELLED · BLOCKED
```

Human gates are non-skippable in code.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,qa]"

# Run tests
pytest --cov=phalanx --cov-report=term-missing --cov-fail-under=70 -x -q

# Lint
ruff check phalanx/
ruff format phalanx/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

---

## Stack

- **Python 3.11** — async throughout (`asyncio`)
- **FastAPI** — REST API, health endpoints, webhook ingestion
- **Celery** — distributed task queue for agent execution
- **PostgreSQL + pgvector** — state, artifacts, and semantic memory
- **Redis** — Celery broker + result backend
- **Anthropic Claude** — Builder agent (code generation)
- **OpenAI GPT-4.1** — Commander, Planner, Reviewer, SRE (reasoning)
- **Node.js + npm** — React/Vite/TypeScript builds inside the worker
- **Slack Bolt** — socket-mode gateway
- **Alembic** — database migrations
- **Docker Compose** — local dev and production

---

## Roadmap

- [x] CI webhook ingestion (GitHub, Buildkite, CircleCI, Jenkins)
- [x] Live demo portal (`demo.usephalanx.com`)
- [x] Agent reasoning traces (`GET /runs/{run_id}/trace`)
- [x] SRE agent — autonomous Dockerfile generation + deploy
- [x] Workspace isolation per run
- [x] **CI Fixer v2 MVP** — agent + tools + loop, GPT-5.4 main + Sonnet 4.6 coder, sandbox verification gate, real end-to-end PR close on prod GitHub
- [ ] CI Fixer scorecard — top-5 languages × 4 failure classes (see matrix below)
- [ ] Tier-2 memory (pgvector) for pattern recall across repos
- [ ] Discord integration
- [ ] Voice input via Whisper
- [ ] Multi-project support
- [ ] Managed cloud version (`app.usephalanx.com`)

### CI Fixer Scorecard — progress

End-to-end PR close on prod (real LLMs, real sandbox, real GitHub CI):

| Language | Lint | Test fail | Flake | Coverage |
|---|:---:|:---:|:---:|:---:|
| Python     | ✅ | ✅ | ✅ | ✅ |
| TypeScript | ✅ | ✅ | ✅ | ✅ |
| JavaScript | ✅ | ✅ | ✅ | ✅ |
| Java       | ⏳ | ⏳ | ⏳ | ⏳ |
| C#         | ⏳ | ⏳ | ⏳ | ⏳ |

**Python + TypeScript + JavaScript rows complete.** 12 of 20 cells closed end-to-end on prod, all 12 replay-gated.

- Python → [`usephalanx/phalanx-ci-fixer-testbed`](https://github.com/usephalanx/phalanx-ci-fixer-testbed) PRs #1–4
- TypeScript → [`usephalanx/phalanx-ci-fixer-testbed-ts`](https://github.com/usephalanx/phalanx-ci-fixer-testbed-ts) PRs #1–6
- JavaScript → [`usephalanx/phalanx-ci-fixer-testbed-js`](https://github.com/usephalanx/phalanx-ci-fixer-testbed-js) PRs #1–7

Per-row recording cost: Python ~\$0.79 / TypeScript ~\$2.63 / JavaScript ~\$2.25 (includes 2 flake retries + 1 coverage retry — both with `delegate_to_coder` escalation patterns specific to jest-style multi-line patches).

**Observation:** `flake` is the most retry-prone cell across languages — 0 retries for Python, 1 for TS, 2 for JS. Root cause: `apply_patch` diff construction is brittler for multi-line test-block additions in JS/TS than in Python. Known sharp edge for Java/C# planning; v2 prompt tune is a follow-up.

### Regression gates (3 layers)

Prompt / loop / tool changes go through three cascading checks before deploy:

| Layer | What it catches | Cost | Latency |
|---|---|---|---|
| **1. Unit tests** (`uv run pytest tests/unit/ci_fixer_v2/`) — 342 tests, mocked SDKs | code bugs, tool shape regressions, provider adapter errors | $0 | ~3 s |
| **2. Live regression smoke** (`scripts/v2_python_regression.sh`) — runs all 4 cells against real LLMs + real sandbox + real GitHub | behavior drift, SDK contract changes, sandbox/CI version drift | ~$1 | ~20 min |
| **3. Replay fixtures** (`uv run pytest tests/integration/scorecard/`) — recorded runs replayed with canned LLM/tool responses | same as Layer 2, deterministically | $0 | ~70 ms |

Two loop-level hard invariants:
- **Verification gate** — `commit_and_push` blocked unless `last_sandbox_verified=True`
- **Evidence gate** — escalation with `infra_failure_out_of_scope` / `preexisting_main_failure` without trace evidence is coerced to `LOW_CONFIDENCE` (logged as `v2.loop.escalation_reason_forced`)

### Architecture decision: no fix-type router

After running Python × {lint, test_fail, flake} end-to-end, all three traced through the **same loop, same tool sequence, same prompt** and committed on the first or second delegate round. Variance across classes (validate_cmd, target files) was already extracted from the CI log + manifest files — not hardcoded per class. A full Strategy / Router abstraction would add code without adding capability. What goes in instead:

1. **Language playbooks** — deterministic env-setup per stack (Python + pyproject → `pip install -e ".[dev]"`, Node + package.json → `npm ci`, …). Skips a GPT env-planner call per run and removes drift.
2. **Coverage rule** in base system prompt — "Never lower `--cov-fail-under` or equivalent. Add tests or escalate."
3. **New escalation enums** — `FLAKY_TEST_DETECTED`, `COVERAGE_ADJUSTMENT_NEEDED_OUT_OF_SCOPE`.

Language router (Python / JS / TS / Java / C#) is still real and already partially exists (sandbox image selection + env planner). That's the only router axis the code needs.

---

## License

MIT — see [LICENSE](LICENSE)

## Community

- Website: [usephalanx.com](https://usephalanx.com)
- Docs: [usephalanx.com/documentation.html](https://usephalanx.com/documentation.html)
- Changelog: [usephalanx.com/changelog.html](https://usephalanx.com/changelog.html)
- X: [@usephalanx](https://x.com/usephalanx)
- Issues: [github.com/usephalanx/phalanx/issues](https://github.com/usephalanx/phalanx/issues)
- Live demos: [demo.usephalanx.com](https://demo.usephalanx.com)
- Generated apps showcase: [github.com/usephalanx/showcase](https://github.com/usephalanx/showcase)
