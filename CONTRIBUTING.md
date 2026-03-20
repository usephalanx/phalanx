# Contributing to Phalanx

Thanks for your interest in contributing. Phalanx is an open-source AI engineering team OS — specialized agents that coordinate from planning to production with human approval at every gate.

## Ways to Contribute

- **Bug reports** — open an issue with steps to reproduce, expected vs actual behavior, and relevant logs
- **Feature requests** — open an issue describing the use case and why it fits Phalanx's architecture
- **Code contributions** — PRs are welcome; see the workflow below
- **Documentation** — improvements to CLAUDE.md, inline docs, or this guide

## Development Setup

```bash
# 1. Clone and create virtualenv
git clone https://github.com/usephalanx/phalanx.git
cd phalanx
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,qa]"

# 2. Start infra (Postgres on 5433, Redis on 6379)
docker compose up postgres redis -d

# 3. Run migrations
python -c "
from alembic.config import Config; from alembic import command
cfg = Config(); cfg.set_main_option('script_location','alembic')
cfg.set_main_option('sqlalchemy.url','postgresql+psycopg2://forge:forge_dev_password@localhost:5433/forge')
command.upgrade(cfg,'head')
"

# 4. Run tests (must pass ≥70% coverage)
pytest --cov=forge --cov-report=term-missing --cov-fail-under=70 -x -q
```

## Code Standards

- Python 3.11+, type hints throughout
- Async agents use `asyncio.run()` — never `get_event_loop()`
- Celery workers must set `FORGE_WORKER=1` (enforces NullPool)
- All new agents inherit from `BaseAgent` and register a Celery task
- PRs must not drop test coverage below 70%
- Run `ruff check forge/` and `ruff format forge/` before submitting

## PR Process

1. Fork the repo and create a feature branch: `git checkout -b feat/your-feature`
2. Make your changes with tests
3. Ensure `pytest --cov=forge --cov-fail-under=70 -x -q` passes
4. Open a PR against `main` with a clear description of what and why
5. A maintainer will review within a few days

## Architecture Notes

See `CLAUDE.md` for the full architecture overview, key invariants, and file map. Read it before making significant changes — there are several non-obvious invariants (NullPool, QAAgent init, approval model) that are easy to break.

## License

By contributing, you agree your contributions will be licensed under the [MIT License](LICENSE).
