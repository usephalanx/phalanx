# ─────────────────────────────────────────────────────────────────────────────
# PHALANX — Developer Makefile
# Run `make help` to see all commands
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help up down restart logs shell migrate migrate-new test lint format \
        validate-config validate-skills seed onboard status worker-logs \
        flower clean reset deploy deploy-migrate ssh-server logs-server status-server \
        sim-trigger sim-trigger-fetch sim-trigger-dry \
        backup backup-list backup-verify backup-restore backup-offhost backup-install backup-uninstall \
        ledger-tail ledger-verify ledger-stats ledger-replay \
        preflight migration-check beat-health

COMPOSE = docker compose
PHALANX_API = $(COMPOSE) exec phalanx-api
PHALANX_WORKER = $(COMPOSE) exec phalanx-worker

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo "PHALANX Development Commands"
	@echo ""
	@echo "  SETUP"
	@echo "  make setup          Copy .env.example → .env, pull images, build"
	@echo "  make preflight      Run safety checks without starting anything"
	@echo "  make up             Run preflight, start all services, verify beat-alive"
	@echo "                       PHALANX_ALLOW_FRESH_BOOT=1  allow a clean first boot"
	@echo "                       PHALANX_ALLOW_EXTERNAL_DB=1 allow non-compose DATABASE_URL"
	@echo "                       PHALANX_SKIP_PREFLIGHT=1     emergency bypass (pre-up)"
	@echo "                       PHALANX_SKIP_BEAT_HEALTH=1   emergency bypass (post-up)"
	@echo "  make beat-health    Standalone: verify celery-beat is dispatching scheduled tasks"
	@echo "  make down           Stop all services"
	@echo "  make restart        Restart all services"
	@echo "  make reset          Full reset: down, delete volumes, up + migrate"
	@echo ""
	@echo "  DATABASE"
	@echo "  make migrate        Run pending Alembic migrations"
	@echo "  make migrate-new m=name  Create new migration"
	@echo "  make migration-check    Bootstrap a fresh DB + round-trip migrations (P0-4)"
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
	@echo ""
	@echo "  BACKUPS (P0-1)"
	@echo "  make backup             Take a pg_dump now (with self-check + retention)"
	@echo "  make backup-list        List local dump files"
	@echo "  make backup-verify      Restore latest dump to a throwaway container, check row counts"
	@echo "  make backup-restore dump=... PHALANX_PG_TARGET=<container>"
	@echo "                          Restore <dump> into <container> (refuses forge-postgres without PHALANX_RESTORE_ALLOW_PROD=1)"
	@echo "  make backup-offhost     Sync dumps to PHALANX_BACKUP_REMOTE via rclone"
	@echo "  make backup-install     Install macOS LaunchAgent (runs every 6h)"
	@echo "  make backup-uninstall   Remove the LaunchAgent"
	@echo ""
	@echo "  LEDGER JSONL (P0-2)"
	@echo "  make ledger-tail        Pretty-print last 20 ledger.jsonl entries"
	@echo "  make ledger-stats       Summary stats (counts, verdicts, schema versions)"
	@echo "  make ledger-verify      Strict integrity check (exit 1 if corrupt lines)"
	@echo "  make ledger-replay      LEDGER_REPLAY_CONFIRM=1 [LEDGER_REPLAY_LIMIT=N] — backfill from DB"

# ── Setup ─────────────────────────────────────────────────────────────────────
setup:
	@[ -f .env ] || cp .env.example .env
	@echo "✅ .env created. Fill in API keys before starting."
	$(COMPOSE) pull
	$(COMPOSE) build

# ── Services ──────────────────────────────────────────────────────────────────
preflight:
	@./scripts/preflight_check.sh

up: preflight
	$(COMPOSE) up -d
	@./scripts/beat_health.sh
	@echo "✅ PHALANX running. API: http://localhost:8000 | Flower: http://localhost:5555"

beat-health:
	@./scripts/beat_health.sh

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

clean:
	$(COMPOSE) down --remove-orphans
	docker image prune -f

reset:
	$(COMPOSE) down -v --remove-orphans
	@PHALANX_ALLOW_FRESH_BOOT=1 $(COMPOSE) up -d postgres redis
	sleep 5
	$(MAKE) migrate
	@PHALANX_ALLOW_FRESH_BOOT=1 $(COMPOSE) up -d
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

# ── Backups (P0-1) ────────────────────────────────────────────────────────────
backup:
	@./scripts/backup_postgres.sh

backup-list:
	@ls -lh backups/postgres/ 2>/dev/null || echo "no backups yet — run 'make backup'"

backup-verify:
	@./scripts/backup_postgres_verify.sh

backup-restore:
	@if [ -z "$(dump)" ]; then \
	  echo "Usage: make backup-restore dump=backups/postgres/forge-YYYYMMDDTHHMMSSZ.dump"; \
	  echo "  Override target with: PHALANX_PG_TARGET=<container> make backup-restore dump=..."; \
	  exit 1; \
	fi
	@./scripts/backup_postgres_restore.sh "$(dump)"

backup-offhost:
	@./scripts/backup_postgres_offhost.sh

backup-install:
	@mkdir -p backups/postgres ~/Library/LaunchAgents
	@sed "s|PHALANX_REPO_ROOT|$(CURDIR)|g" scripts/com.phalanx.backup.plist \
	  > ~/Library/LaunchAgents/com.phalanx.backup.plist
	@launchctl unload ~/Library/LaunchAgents/com.phalanx.backup.plist 2>/dev/null || true
	@launchctl load ~/Library/LaunchAgents/com.phalanx.backup.plist
	@echo "✅ LaunchAgent installed. Schedule: 00:00, 06:00, 12:00, 18:00 local time."
	@echo "   Verify with: launchctl list | grep com.phalanx.backup"
	@echo "   Logs:        tail -f backups/postgres/launchagent.log"

backup-uninstall:
	@launchctl unload ~/Library/LaunchAgents/com.phalanx.backup.plist 2>/dev/null || true
	@rm -f ~/Library/LaunchAgents/com.phalanx.backup.plist
	@echo "✅ LaunchAgent uninstalled."

# ── Migrations bootstrap (P0-4) ───────────────────────────────────────────────
migration-check:
	@./scripts/migration_bootstrap_check.sh

# ── Ledger JSONL (P0-2) ───────────────────────────────────────────────────────
ledger-tail:
	@if [ ! -f ledger.jsonl ]; then echo "no ledger.jsonl yet"; exit 0; fi
	@tail -n 20 ledger.jsonl | python3 -c 'import json,sys;\
[print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin if l.strip()]'

ledger-stats:
	@./scripts/ledger_jsonl_verify.py ledger.jsonl || true

ledger-verify:
	@./scripts/ledger_jsonl_verify.py ledger.jsonl

ledger-replay:
	@if [ -z "$(LEDGER_REPLAY_CONFIRM)" ]; then \
	  echo "Replay APPENDS to ledger.jsonl from the live DB. To run:"; \
	  echo "  LEDGER_REPLAY_CONFIRM=1 make ledger-replay"; \
	  echo "  (optionally LEDGER_REPLAY_LIMIT=10 to cap)"; \
	  exit 1; \
	fi
	@.venv/bin/python scripts/ledger_jsonl_replay.py \
	  $(if $(LEDGER_REPLAY_LIMIT),--limit $(LEDGER_REPLAY_LIMIT))
