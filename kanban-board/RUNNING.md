# Running the Kanban Board locally

## Prerequisites

- Docker & Docker Compose v2+
- (Optional) Python 3.11+ and Node 20+ for running outside containers

---

## Quick start (Docker)

```bash
# 1. Clone the repo and cd into the project
cd kanban-board

# 2. Start all services (postgres, redis, backend, frontend)
docker compose up --build

# 3. (In another terminal) Seed demo data
docker compose exec backend python -m backend.scripts.seed
```

The app is now running:

| Service  | URL                        |
|----------|----------------------------|
| Frontend | http://localhost:5173       |
| Backend  | http://localhost:8000       |
| API docs | http://localhost:8000/docs  |
| Postgres | localhost:5433              |
| Redis    | localhost:6379              |

---

## Default credentials

| Field    | Value              |
|----------|--------------------|
| Email    | `demo@phalanx.dev` |
| Password | `demo1234`         |

> **Warning:** These credentials are for local development only. Never use them in production.

---

## Environment variables

### Backend

| Variable              | Default                                                                 | Description                     |
|-----------------------|-------------------------------------------------------------------------|---------------------------------|
| `DATABASE_URL`        | `postgresql+asyncpg://kanban:kanban_dev_password@localhost:5433/kanban`  | Async DB connection string      |
| `ALEMBIC_DATABASE_URL`| `postgresql+psycopg2://kanban:kanban_dev_password@localhost:5433/kanban` | Sync DB URL for Alembic         |
| `REDIS_URL`           | `redis://localhost:6379/0`                                              | Redis connection string         |
| `SECRET_KEY`          | `change-me-in-production`                                               | JWT signing key                 |
| `DEBUG`               | `false`                                                                 | Enable SQLAlchemy echo + debug  |

### Frontend

| Variable            | Default                  | Description            |
|---------------------|--------------------------|------------------------|
| `VITE_API_BASE_URL` | `http://localhost:8000`  | Backend API base URL   |

---

## Running without Docker

### 1. Start PostgreSQL and Redis

```bash
# Using the compose file for just infra services:
docker compose up postgres redis -d
```

### 2. Backend

```bash
cd kanban-board

# Create and activate virtualenv
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set env vars
export DATABASE_URL="postgresql+asyncpg://kanban:kanban_dev_password@localhost:5433/kanban"

# Run migrations
python -c "
from alembic.config import Config; from alembic import command
cfg = Config('alembic.ini')
cfg.set_main_option('sqlalchemy.url', 'postgresql+psycopg2://kanban:kanban_dev_password@localhost:5433/kanban')
command.upgrade(cfg, 'head')
"

# Seed demo data
cd backend && python -m scripts.seed && cd ..

# Start the dev server
uvicorn backend.app.main:app --reload --port 8000
```

### 3. Frontend

```bash
cd kanban-board/frontend

npm install
npm run dev
```

---

## Useful commands

```bash
# Stop all containers
docker compose down

# Stop and remove volumes (wipes DB data)
docker compose down -v

# View backend logs
docker compose logs -f backend

# Run backend tests
docker compose exec backend pytest --cov=app -x -q

# Run frontend tests
docker compose exec frontend npx vitest run

# Re-seed (idempotent — skips if demo user exists)
docker compose exec backend python -m backend.scripts.seed
```

---

## Production build (frontend)

To build the frontend with nginx instead of the Vite dev server:

```bash
docker compose build --build-arg target=prod frontend
```

Or override the target in `docker-compose.yml`:

```yaml
frontend:
  build:
    target: prod
  ports:
    - "80:80"
```
