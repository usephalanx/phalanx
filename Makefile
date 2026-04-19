# ─────────────────────────────────────────────────────────────────────────────
# PHALANX — Developer Makefile
# Run `make help` to see all commands
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help up down restart logs shell migrate migrate-new test lint format \
        validate-config validate-skills seed onboard status worker-logs \
        flower clean reset deploy deploy-migrate ssh-server logs-server status-server \
        sim-trigger sim-trigger-fetch sim-trigger-dry

COMPOSE = docker compose
PHALANX_API = $(COMPOSE) exec phalanx-api
PHALANX_WORKER = $(COMPOSE) exec phalanx-worker

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo "PHALANX Development Commands"
	@echo ""
	@echo "  SETUP"
	@echo "  make setup          Copy .env.example → .env, pull images, build"
	@echo "  make up             Start all services"
	@echo "  make down           Stop all services"
	@echo "  make restart        Restart all services"
	@echo "  make reset          Full reset: down, delete volumes, up + migrate"
	@echo ""
	@echo "  DATABASE"
	@echo "  make migrate        Run pending Alembic migrations"
	@echo "  make migrate-new m=name  Create new migration"
	@echo "  make seed           Seed with test team + project config"
	@echo ""
	@echo "  DEVELOPMENT"
	@echo "  make logs           Tail all service logs"
	@echo "  make logs-api       Tail API logs only"
	@echo "  make logs-worker    Tail worker logs"
	@echo "  make shell          Open shell in phalanx-api container"
	@echo "  make flower         Open Flower UI (Celery monitor)"
	@echo ""
	@echo "  QUALITY"
	@echo "  make test           Run all tests"
	@echo "  make test-unit      Run unit tests only"
	@echo "  make test-e2e       Run end-to-end tests"
	@echo "  make lint           Run ruff linter"
	@echo "  make format         Run ruff formatter"
	@echo "  make typecheck      Run mypy"
	@echo ""
	@echo "  VALIDATION"
	@echo "  make validate-config    Validate all YAML config files"
	@echo "  make validate-skills    Validate all skill YAML files"
	@echo "  make skill-gaps team=website-alpha project=acme-website"
	@echo ""
	@echo "  PROJECT"
	@echo "  make onboard project=acme-website  Run onboarding for a project"
	@echo "  make status project=acme-website   Show project status"
	@echo ""
	@echo "  CI FIXER SIMULATION"
	@echo "  make gh-login           Authenticate gh CLI (one-time setup)"
	@echo "  make sim-trigger-fetch  Discover failing PR in trigger.dev + fetch logs"
	@echo "  make sim-trigger-dry    Dry-run fix (real clone/LLM/sandbox, skip push)"
	@echo "  make sim-trigger        Full prod-parity run (pushes fix commit)"
	@echo "  make sim-trigger SIM_REPO=owner/repo  Override target repo"
	@echo ""
	@echo "  DEPLOY"
	@echo "  make deploy             Build locally and deploy to LightSail"
	@echo "  make deploy-migrate     Run DB migrations on server only"
	@echo "  make ssh-server         SSH into the LightSail box"
	@echo "  make logs-server        Tail logs on server"
	@echo "  make status-server      Show container status on server"

# ── Setup ─────────────────────────────────────────────────────────────────────
setup:
	@[ -f .env ] || cp .env.example .env
	@echo "✅ .env created. Fill in API keys before starting."
	$(COMPOSE) pull
	$(COMPOSE) build

# ── Services ──────────────────────────────────────────────────────────────────
up:
	$(COMPOSE) up -d
	@echo "✅ PHALANX running. API: http://localhost:8000 | Flower: http://localhost:5555"

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

clean:
	$(COMPOSE) down --remove-orphans
	docker image prune -f

reset:
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d postgres redis
	sleep 5
	$(MAKE) migrate
	$(COMPOSE) up -d
	@echo "✅ Full reset complete."

# ── Database ──────────────────────────────────────────────────────────────────
migrate:
	$(PHALANX_API) alembic upgrade head

migrate-new:
	@[ -n "$(m)" ] || (echo "Usage: make migrate-new m=migration_name" && exit 1)
	$(PHALANX_API) alembic revision --autogenerate -m "$(m)"

seed:
	$(PHALANX_API) python scripts/seed_team.py

# ── Logs ─────────────────────────────────────────────────────────────────────
logs:
	$(COMPOSE) logs -f --tail=100

logs-api:
	$(COMPOSE) logs -f phalanx-api --tail=100

logs-worker:
	$(COMPOSE) logs -f phalanx-worker phalanx-worker-builder --tail=100

# ── Dev tools ─────────────────────────────────────────────────────────────────
shell:
	$(PHALANX_API) /bin/bash

flower:
	@echo "Opening Flower at http://localhost:5555"
	@open http://localhost:5555 2>/dev/null || echo "Visit http://localhost:5555"

# ── Testing ───────────────────────────────────────────────────────────────────
test:
	$(PHALANX_API) pytest tests/ -v

test-unit:
	$(PHALANX_API) pytest tests/unit/ -v

test-e2e:
	$(PHALANX_API) pytest tests/integration/ -v -s

test-skills:
	$(PHALANX_API) pytest tests/skill_tests/ -v

# ── Quality ───────────────────────────────────────────────────────────────────
lint:
	$(PHALANX_API) ruff check phalanx/ tests/

format:
	$(PHALANX_API) ruff format phalanx/ tests/
	$(PHALANX_API) ruff check --fix phalanx/ tests/

typecheck:
	$(PHALANX_API) mypy phalanx/

# ── Config + Skills ───────────────────────────────────────────────────────────
validate-config:
	$(PHALANX_API) python scripts/validate_config.py

validate-skills:
	$(PHALANX_API) python scripts/validate_skills.py

skill-gaps:
	@[ -n "$(team)" ] || (echo "Usage: make skill-gaps team=website-alpha project=acme-website" && exit 1)
	$(PHALANX_API) python scripts/skill_gap_report.py --team $(team) --project $(project)

# ── Project ops ───────────────────────────────────────────────────────────────
onboard:
	@[ -n "$(project)" ] || (echo "Usage: make onboard project=acme-website" && exit 1)
	$(PHALANX_API) python scripts/onboard_project.py --project $(project)

status:
	@[ -n "$(project)" ] || (echo "Usage: make status project=acme-website" && exit 1)
	$(PHALANX_API) python scripts/project_status.py --project $(project)

# ── CI Fixer simulations ──────────────────────────────────────────────────────
SIM_REPO ?= triggerdotdev/trigger.dev

gh-login:
	gh auth login --web --scopes repo,read:org

sim-trigger-fetch:
	FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --fetch --repo $(SIM_REPO)

sim-trigger-dry:
	FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --dry-run --repo $(SIM_REPO)

sim-trigger:
	FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --repo $(SIM_REPO)

# ── Deploy ────────────────────────────────────────────────────────────────────
SERVER_IP = 44.233.157.41
SSH_KEY   = $(or $(LIGHTSAIL_KEY),$(HOME)/work/LightsailDefaultKey-us-west-2.pem)
SSH_CMD   = ssh -i $(SSH_KEY) -o StrictHostKeyChecking=no ubuntu@$(SERVER_IP)

deploy:
	@[ -f .env.prod ] || (echo "ERROR: .env.prod not found. Copy .env.example and fill in real values." && exit 1)
	chmod +x deploy.sh
	./deploy.sh

deploy-migrate:
	chmod +x deploy.sh
	./deploy.sh --migrate-only

ssh-server:
	$(SSH_CMD)

logs-server:
	$(SSH_CMD) 'cd /home/ubuntu/phalanx && docker compose logs -f --tail=100'

status-server:
	$(SSH_CMD) 'cd /home/ubuntu/phalanx && docker compose ps && echo "" && docker stats --no-stream'
