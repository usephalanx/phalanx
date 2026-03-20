# FORGE — Claude Code Context

## What this is
FORGE is an AI-powered software development OS. Slack commands trigger a multi-agent pipeline that plans, builds, reviews, tests, and ships code autonomously.

## Architecture
```
Slack /phalanx build → Gateway → Celery → Commander → Orchestrator
                                                          ↓
                               Planner → Builder → Reviewer → QA → Security → Release
```

- **Gateway** (`forge/gateway/slack_bot.py`) — Bolt socket-mode app. Receives `/phalanx` slash commands, creates WorkOrder + Run in DB, dispatches `commander` Celery task.
- **Commander** (`forge/agents/commander.py`) — Drives the run: LLM plans tasks, posts plan approval to Slack, then hands off to `WorkflowOrchestrator`.
- **Orchestrator** (`forge/workflow/orchestrator.py`) — Dispatches tasks in sequence, polls DB for completion, handles approval gates.
- **Agents** (`forge/agents/`) — Each agent (planner, builder, reviewer, qa, security, release) is a Celery task + async class. All use `asyncio.run()` (not deprecated `get_event_loop()`).
- **State machine** (`forge/workflow/state_machine.py`) — Enforces valid Run status transitions.
- **DB** (`forge/db/`) — PostgreSQL + pgvector via SQLAlchemy async. `FORGE_WORKER=1` → NullPool (required for Celery fork workers to avoid event-loop conflicts).

## Key invariants
- **NullPool in workers**: `FORGE_WORKER=1` must be set in every Celery worker container. Without it, a persistent pool binds to the first event loop created by `asyncio.run()` and all subsequent tasks in the same fork-worker fail with "Future attached to a different loop".
- **Fresh `get_db()` per poll**: `orchestrator._dispatch_and_wait()` opens a new `async with get_db()` context on every poll iteration — never reuses a session across `asyncio.sleep()` yields.
- **QAAgent** has a different `__init__` than BaseAgent: `(run_id, repo_path, task_id, test_command, coverage_threshold)` — no `agent_id`, calls `evaluate()` not `execute()`.
- **Approval model**: field is `status` (not `decision`), values `PENDING/APPROVED/REJECTED`.

## Local dev setup
```bash
# Start infra (postgres on 5433, redis on 6379)
docker compose up postgres redis -d

# Run DB migrations
source .venv/bin/activate
python -c "
from alembic.config import Config; from alembic import command
cfg = Config(); cfg.set_main_option('script_location','alembic')
cfg.set_main_option('sqlalchemy.url','postgresql+psycopg2://forge:forge_dev_password@localhost:5433/forge')
command.upgrade(cfg,'head')
"

# Run tests (must pass 70% coverage)
pytest --cov=forge --cov-report=term-missing --cov-fail-under=70 -x -q
```

## Production deploy
```bash
./deploy.sh           # auto-bumps patch version, builds linux/amd64, scp to prod, restarts
./deploy.sh v1.2.0    # explicit version
./deploy.sh --migrate-only
```
Server: `ubuntu@44.233.157.41`, key: `~/work/LightsailDefaultKey-us-west-2.pem`

## Test baseline
342 passed, 0 failed, ≥70% coverage. Run before every deploy.

## File map (critical paths)
| File | Purpose |
|------|---------|
| `forge/agents/commander.py` | Celery task + CommanderAgent; soft_time_limit=3600 |
| `forge/agents/builder.py` | Celery task + BuilderAgent; soft_time_limit=1800 |
| `forge/workflow/orchestrator.py` | WorkflowOrchestrator — dispatch/poll loop |
| `forge/workflow/state_machine.py` | RunStatus enum + valid transitions |
| `forge/workflow/approval_gate.py` | ApprovalGate — Slack button + DB polling |
| `forge/db/session.py` | get_db(), NullPool logic |
| `forge/gateway/slack_bot.py` | Slack Bolt gateway |
| `docker-compose.prod.yml` | Prod compose — FORGE_WORKER=1 on worker |
| `deploy.sh` | Build + ship to Lightsail |
| `scripts/e2e_simulate.py` | Full in-process pipeline sim (no Slack/Celery) |
| `tests/unit/` | Unit tests — mock-heavy, fast |
| `tests/integration/` | Integration tests — real DB (aiosqlite) |
