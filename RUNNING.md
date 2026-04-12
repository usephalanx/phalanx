# Running the FORGE API

## TEAM_BRIEF
stack: Python/FastAPI
test_runner: pytest tests/
lint_tool: ruff check .
coverage_tool: pytest-cov
coverage_threshold: 70
coverage_applies: true

## Prerequisites

- Python 3.11+
- Docker & Docker Compose (for PostgreSQL and Redis)

## 1. Start Infrastructure

```bash
docker compose up postgres redis -d
```

PostgreSQL runs on port 5433, Redis on port 6379.

## 2. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Run DB Migrations

```bash
source .venv/bin/activate
python -c "
from alembic.config import Config; from alembic import command
cfg = Config(); cfg.set_main_option('script_location','alembic')
cfg.set_main_option('sqlalchemy.url','postgresql+psycopg2://forge:forge_dev_password@localhost:5433/forge')
command.upgrade(cfg,'head')
"
```

## 4. Run the Server

```bash
# Option A — run directly
python main.py

# Option B — run via uvicorn with auto-reload
uvicorn phalanx.api.main:app --reload
```

The server starts at http://localhost:8000.

## 5. Test the Endpoint

```bash
curl http://localhost:8000/health
```

## 6. Run Tests

```bash
pytest --cov=phalanx --cov-report=term-missing --cov-fail-under=70 -x -q
```
