# Phalanx

**Prompt in. PR out.**

Phalanx is an open-source AI engineering team. Specialized agents coordinate from planning to production — with human approval at every gate. Works from any platform: Slack, Discord, API, voice.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg)](docker-compose.yml)

---

## Demo

https://github.com/usephalanx/phalanx/assets/demo.mp4

> `/phalanx build "Add a hello world endpoint"` → plan approval → agents build, test, review → PR opened in 3 minutes.

---

## How It Works

```
/phalanx build "Add JWT auth"
        ↓
Commander → Planner → Builder → Reviewer → QA → Security → Release
        ↓                                                       ↓
  [Approve Plan?]                                        [Ship to prod?]
```

1. **Delegate** — send a task from Slack, Discord, API, or any platform
2. **Approve** — review the implementation plan before any code is written
3. **Ship** — agents build, test, review, and open a PR. You approve the merge.

---

## The Agents

| Agent | Role |
|-------|------|
| **Commander** | Accepts work orders, drives the run end-to-end |
| **Planner** | Decomposes tasks into ordered steps with file paths and test cases |
| **Builder** | Writes code, creates files, commits to a feature branch |
| **Reviewer** | Reviews commits for quality, correctness, and project patterns |
| **QA** | Runs tests, measures coverage, verifies passing before advancing |
| **Security** | Scans for vulnerabilities, secrets, and injection risks |
| **Release** | Opens the PR, runs health checks, marks the run shipped |

---

## Self-Hosting

### Prerequisites

- Docker + Docker Compose
- Python 3.11+
- PostgreSQL (via Docker)
- Redis (via Docker)
- Slack app with socket mode enabled (or any other platform integration)

### Quick Start

```bash
# 1. Clone
git clone https://github.com/usephalanx/phalanx.git
cd phalanx

# 2. Configure
cp .env.example .env
# Edit .env — add your Anthropic API key, Slack tokens, DB credentials

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

# 5. Start the gateway
python -m phalanx.gateway.slack_bot
```

### Production Deploy

Build locally and ship to your server (no `docker build` on the server):

```bash
# Set your server details
export DEPLOY_HOST="ubuntu@your-server-ip"
export DEPLOY_DIR="/home/ubuntu/phalanx"
export DEPLOY_KEY="~/.ssh/your-key.pem"

./deploy.sh           # auto-bump patch version
./deploy.sh v1.2.0    # explicit version
./deploy.sh --migrate-only
```

---

## Configuration

All agent behavior is configured in YAML — no code changes needed.

```yaml
# configs/team.yaml
agents:
  builder:
    model: claude-opus-4-5
    max_tokens: 8192
    skills:
      - python
      - fastapi
      - pytest

approval_gates:
  plan: true       # require human approval before building
  ship: true       # require human approval before merging
  release: true    # require human approval before deploying
```

---

## Workflow States

Every run follows a deterministic 16-state machine:

```
INTAKE → RESEARCHING → PLANNING → [AWAITING_PLAN_APPROVAL]
       → EXECUTING → VERIFYING → [AWAITING_SHIP_APPROVAL]
       → READY_TO_MERGE → MERGED → RELEASE_PREP
       → [AWAITING_RELEASE_APPROVAL] → SHIPPED
```

Human gates are non-skippable in code.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,qa]"

# Run tests (requires Postgres on 5433)
pytest --cov=phalanx --cov-report=term-missing --cov-fail-under=70 -x -q

# Lint
ruff check phalanx/
ruff format phalanx/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

---

## Stack

- **Python 3.11** — async throughout (`asyncio`)
- **FastAPI** — REST API + health endpoints
- **Celery** — distributed task queue for agent execution
- **PostgreSQL + pgvector** — state, artifacts, and semantic memory
- **Redis** — Celery broker + result backend
- **Slack Bolt** — socket-mode gateway (Discord, API adapters coming)
- **Alembic** — database migrations
- **Docker Compose** — local dev and production

---

## Roadmap

- [ ] Discord integration
- [ ] REST API / webhook gateway (no Slack required)
- [ ] Voice input via Whisper
- [ ] Multi-project support
- [ ] Managed cloud version (`app.usephalanx.com`)

---

## License

MIT — see [LICENSE](LICENSE)

## Community

- Website: [usephalanx.com](https://usephalanx.com)
- X: [@usephalanx](https://x.com/usephalanx)
- Issues: [github.com/usephalanx/phalanx/issues](https://github.com/usephalanx/phalanx/issues)
- Generated apps showcase: [github.com/usephalanx/showcase](https://github.com/usephalanx/showcase)
