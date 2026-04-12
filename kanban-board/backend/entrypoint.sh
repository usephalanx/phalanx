#!/usr/bin/env bash
set -euo pipefail

echo "Running Alembic migrations..."

# Override alembic sqlalchemy.url with the sync driver URL for migrations
if [ -n "${ALEMBIC_DATABASE_URL:-}" ]; then
    export SQLALCHEMY_URL="$ALEMBIC_DATABASE_URL"
    python -c "
from alembic.config import Config
from alembic import command

cfg = Config('alembic.ini')
cfg.set_main_option('sqlalchemy.url', '$ALEMBIC_DATABASE_URL')
command.upgrade(cfg, 'head')
"
else
    python -c "
from alembic.config import Config
from alembic import command

cfg = Config('alembic.ini')
command.upgrade(cfg, 'head')
"
fi

echo "Migrations complete. Starting uvicorn..."

exec uvicorn backend.app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --reload-dir backend
