# Kanban Board — Setup Instructions

## Prerequisites

- Python 3.11+
- PostgreSQL 15+ (for production)

## Install dependencies

```bash
cd kanban-board
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run database migrations

```bash
# Set your database URL (defaults to local postgres)
export DATABASE_URL="postgresql+asyncpg://kanban:kanban_dev_password@localhost:5432/kanban"

# Run Alembic migrations
alembic upgrade head
```

## Run tests

```bash
cd kanban-board
source .venv/bin/activate
PYTHONPATH=backend pytest --cov=app --cov-report=term-missing -x -q
```

## Start the dev server

```bash
cd kanban-board/backend
uvicorn app.main:app --reload --port 8000
```

## Lock files

Do NOT manually create or edit lock files. Generate them with:

```bash
pip freeze > requirements-lock.txt   # if needed
```
