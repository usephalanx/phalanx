# FORGE — Deployment Guide

## Overview

**Build locally (Mac) → transfer to LightSail → load and run.**

The server never runs `docker build`. All images are built on your Mac,
saved as tarballs, SCP'd to the box, and loaded via `docker load`.
This keeps the server lean and deployments fast.

---

## Infrastructure

| Resource | Value |
|---|---|
| Server | AWS LightSail — us-west-2 (Oregon) |
| Static IP | `44.233.157.41` |
| App directory | `/home/ubuntu/forge` |
| SSH user | `ubuntu` |
| SSH key | `~/work/LightsailDefaultKey-us-west-2.pem` |
| S3 bucket | `forge-artifacts-teamworks` (us-west-2) |
| S3 IAM user | `forge-service` |

---

## Prerequisites (one-time local setup)

### 1. SSH key

Place your LightSail key at one of these paths (the deploy script checks them in order):
```
~/work/LightsailDefaultKey-us-west-2.pem
~/work/aws/LightsailDefaultKey-us-west-2.pem
~/.ssh/LightsailDefaultKey-us-west-2.pem
```

Fix permissions:
```bash
chmod 400 ~/work/LightsailDefaultKey-us-west-2.pem
```

### 2. Create `.env.prod` locally

```bash
cp .env.example .env.prod
# Fill in all real values — this file is NEVER committed to git
```

Key production values to set:
```
FORGE_ENV=production
POSTGRES_PASSWORD=<strong password>
DATABASE_URL=postgresql+asyncpg://forge:<password>@postgres:5432/forge
REDIS_PASSWORD=<strong password>
ANTHROPIC_API_KEY=sk-ant-...
GROQ_API_KEY=gsk_...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
GITHUB_TOKEN=ghp_...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-west-2
FORGE_S3_BUCKET=forge-artifacts-teamworks
LOG_LEVEL=INFO
LOG_FORMAT=json
API_WORKERS=2
```

### 3. One-time server bootstrap

```bash
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 'bash -s' < scripts/bootstrap_server.sh
```

This installs Docker + Docker Compose, creates `/home/ubuntu/forge/` with
all required subdirectories, and opens ports 80 and 8000.

---

## Daily Workflow

### Build and deploy

```bash
# Full deploy (auto-bumps patch version)
./deploy.sh

# Deploy with explicit version tag
./deploy.sh v1.2.0

# Run DB migrations only (no image rebuild)
./deploy.sh --migrate-only
```

### What deploy.sh does

1. **Builds** `forge-api` and `forge-worker` images locally (`linux/amd64`)
2. **Saves** images as `.tar.gz` to `/tmp/`
3. **Uploads** images + `docker-compose.yml` + `nginx/nginx.conf` + `skill-registry/` + `configs/` + `.env.prod` → server
4. **On server**: loads images → runs `alembic upgrade head` → restarts all containers
5. **Verifies** `GET /health` returns 200
6. **Tags** the git release

### Make shortcuts

```bash
make deploy          # same as ./deploy.sh
make deploy-migrate  # same as ./deploy.sh --migrate-only
make ssh             # open shell on server
make logs            # tail all container logs on server
make status          # show container status on server
```

---

## Server Layout

```
/home/ubuntu/forge/
├── docker-compose.yml      # uploaded by deploy.sh
├── .env                    # uploaded from .env.prod by deploy.sh
├── nginx/
│   └── nginx.conf          # uploaded by deploy.sh
├── skill-registry/         # rsynced by deploy.sh
├── configs/                # rsynced by deploy.sh
├── postgres-data/          # Docker volume (persistent)
└── forge-repos/            # git clones for agent workspaces
```

---

## SSH Access

```bash
# Direct SSH
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41

# View logs
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 \
  'cd /home/ubuntu/forge && docker compose logs -f forge-api'

# Run migrations manually
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 \
  'cd /home/ubuntu/forge && docker compose run --rm forge-migrate'

# Restart a single service
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 \
  'cd /home/ubuntu/forge && docker compose restart forge-api'
```

---

## Health Checks

| Endpoint | Expected | What it checks |
|---|---|---|
| `GET http://44.233.157.41:8000/health` | `200 {"status":"ok"}` | API + DB + Redis alive |
| `GET http://44.233.157.41:8000/health/db` | `200` | Postgres connection |
| `GET http://44.233.157.41:8000/health/redis` | `200` | Redis connection |

---

## Rollback

```bash
# List available image versions on server
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 \
  'docker images | grep forge'

# Roll back to a specific version
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 bash -s <<'EOF'
  cd /home/ubuntu/forge
  docker compose down
  # re-tag old version as latest
  docker tag forge-api:v0.1.0 forge-api:latest
  docker tag forge-worker:v0.1.0 forge-worker:latest
  docker compose up -d --no-build
EOF
```

---

## LightSail Firewall

Ensure these ports are open in the LightSail console (Networking tab):

| Port | Protocol | Purpose |
|---|---|---|
| 22 | TCP | SSH |
| 80 | TCP | nginx (HTTP) |
| 8000 | TCP | forge-api (direct, for health checks) |

Port 443 (HTTPS) — add when you attach a domain + SSL cert.

---

## Environment Files — Rules

| File | Committed? | Purpose |
|---|---|---|
| `.env.example` | ✅ Yes | Template with placeholder values only |
| `.env` | ❌ Never | Local dev — real values |
| `.env.prod` | ❌ Never | Production — real values, uploaded by deploy.sh |

`detect-secrets` in the pre-commit hook will block accidental credential commits.
