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
| **CI Fixer** | Triggered by CI webhook failures. Reads logs, diagnoses root cause, opens a fix PR autonomously. | GPT-4.1 |
| **Prompt Enricher** | Detects vague or ambiguous work orders and resolves intent before planning begins. | GPT-4.1 |

---

## What Ships with Every Run

- **Isolated workspace** — each run clones into `/tmp/forge-repos/{slug}-{run_id[:8]}/`. No cross-run contamination.
- **QA.md recipe** — builder generates a machine-readable test recipe (stack, runner, install steps, coverage threshold). QA follows it exactly.
- **Agent traces** — every agent decision is recorded. Inspect at `GET /runs/{run_id}/trace`.
- **Live demo URL** — SRE deploys the finished app to `demo.usephalanx.com/{slug}` after every successful run.

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
- [ ] Discord integration
- [ ] Voice input via Whisper
- [ ] Multi-project support
- [ ] Managed cloud version (`app.usephalanx.com`)

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
