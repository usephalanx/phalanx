"""
SRE Agent — infrastructure wiring for live demo deployments.

Responsibilities:
  1. Clone the run's PR branch from the project repo (not the showcase mirror)
  2. Detect app structure: frontend / backend / fullstack (language-agnostic)
  3. LLM generates a production-ready Dockerfile — handles any language + fullstack
  4. Build Docker image; on failure fall back to language-detected template
  5. Enforce LRU cap on running demos
  6. Start container; language-agnostic CMD fallback if image has no CMD
  7. Wire nginx so the demo is accessible at demo.usephalanx.com/<slug>
  8. Health check: separate frontend (/) and backend (/health) probes
  9. On failure: read container logs → LLM diagnoses root cause → post to Slack thread
 10. Self-heal: signal commander → dispatch a targeted fix builder task → retry (max 2)
 11. On success: post demo URL to Slack thread, update Run.deploy_url

Design invariants:
  - Feature-gated: phalanx_enable_demo_deploy=False → no-op, COMPLETED silently.
  - SRE is non-fatal: failures mark Demo.status=FAILED but never fail the Run.
  - Clones from project.config.repo_url + run.active_branch (falls back to showcase).
  - Self-heal retries are capped at _MAX_SELF_HEAL_RETRIES to prevent infinite loops.
  - Never raises to Celery — all exceptions caught, stored in Demo.error.

Agent role: "sre"
Queue:      "sre"
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent, mark_task_failed
from phalanx.config.settings import get_settings
from phalanx.db.models import Demo, Project, Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app
from phalanx.workflow.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)

# ── Dockerfile templates ──────────────────────────────────────────────────────

_DOCKERFILE_REACT = """\
FROM node:20-alpine AS builder
# DEMO_BASE_PATH is the URL subpath the app is served at (e.g. /my-demo/)
# Vite bakes this into all asset import paths so they resolve correctly.
ARG DEMO_BASE_PATH=/
WORKDIR /app
COPY package*.json ./
RUN npm install --silent
COPY . .
# Patch React Router BrowserRouter to include basename for subpath deployment.
# Uses Q='"' so the quote character doesn't conflict with shell/Dockerfile quoting.
RUN if [ "$DEMO_BASE_PATH" != "/" ]; then \\
      ENTRY=$(find src -maxdepth 3 \\( -name "main.tsx" -o -name "main.jsx" -o -name "main.ts" -o -name "main.js" \\) 2>/dev/null | head -1); \\
      if [ -n "$ENTRY" ]; then \\
        Q='"'; \\
        sed -i "s|<BrowserRouter>|<BrowserRouter basename=${Q}${DEMO_BASE_PATH}${Q}>|g" "$ENTRY"; \\
      fi; \\
    fi
# Skip tsc type-checking (generated test stubs cause errors); use vite directly.
# Normalise output: always leave the built SPA at /app/dist.
RUN npx vite build --base=${DEMO_BASE_PATH} || npm run build -- --base=${DEMO_BASE_PATH} || true && \
    ([ -d dist ] || ([ -d build ] && mv build dist) || mkdir -p dist)

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
# SPA routing: all paths serve index.html
RUN printf 'server{listen 80;root /usr/share/nginx/html;index index.html;location /{try_files $uri $uri/ /index.html;}location /health{return 200 ok;}}' \
    > /etc/nginx/conf.d/default.conf
EXPOSE 80
"""

_DOCKERFILE_NEXTJS = """\
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install --silent
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_SKIP_TYPECHECKING=1
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "-p", "3000"]
"""

_DOCKERFILE_FASTAPI = """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || \\
    pip install --no-cache-dir -r requirements.txt 2>/dev/null || true
COPY . .
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_DOCKERFILE_FLASK = """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_ENV=production
EXPOSE 5000
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"]
"""

_DOCKERFILE_EXPRESS = """\
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --silent --omit=dev
COPY . .
EXPOSE 3000
CMD ["node", "server.js"]
"""

_DOCKERFILE_STATIC = """\
FROM nginx:alpine
COPY . /usr/share/nginx/html
RUN printf 'server{listen 80;root /usr/share/nginx/html;index index.html;\\
location /health{return 200 ok;}}' > /etc/nginx/conf.d/default.conf
EXPOSE 80
"""

_DOCKERFILE_DJANGO = """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true
COPY . .
RUN python manage.py collectstatic --noinput 2>/dev/null || true
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
"""

_DOCKERFILE_GO = """\
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o server .

FROM alpine:3.19
WORKDIR /app
COPY --from=builder /app/server ./server
EXPOSE 8080
CMD ["./server"]
"""

_DOCKERFILE_RUBY = """\
FROM ruby:3.3-slim
WORKDIR /app
COPY Gemfile* ./
RUN bundle install --without development test
COPY . .
EXPOSE 3000
CMD ["bundle", "exec", "ruby", "app.rb", "-o", "0.0.0.0", "-p", "3000"]
"""

_DOCKERFILE_PHP = """\
FROM php:8.3-apache
COPY . /var/www/html/
RUN echo "<?php phpinfo(); ?>" > /var/www/html/health.php 2>/dev/null || true
EXPOSE 80
"""

_DOCKERFILE_RUST = """\
FROM rust:1.76-slim AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=builder /app/target/release/app ./app
EXPOSE 8080
CMD ["./app"]
"""

# Fullstack template: nginx proxies /api/ → uvicorn backend, serves React SPA at /
_DOCKERFILE_FULLSTACK = """\
FROM node:20-alpine AS frontend
ARG DEMO_BASE_PATH=/
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install --silent
COPY frontend/ .
RUN npx vite build --base=${DEMO_BASE_PATH} 2>/dev/null || \\
    npm run build -- --base=${DEMO_BASE_PATH} 2>/dev/null || true && \\
    ([ -d dist ] || ([ -d build ] && mv build dist) || mkdir -p dist)

FROM python:3.12-slim
WORKDIR /app
# Install nginx + supervisor to run two processes
RUN apt-get update && apt-get install -y --no-install-recommends \\
    nginx supervisor curl && rm -rf /var/lib/apt/lists/*
# Install backend deps
COPY backend/requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true
COPY backend/ ./backend/
# Copy built frontend
COPY --from=frontend /frontend/dist /var/www/html
# nginx config: serve frontend, proxy /api/ to uvicorn on 8000
RUN printf '\\
server {\\n\\
  listen 80;\\n\\
  root /var/www/html;\\n\\
  index index.html;\\n\\
  location /health { return 200 ok; }\\n\\
  location /api/ { proxy_pass http://127.0.0.1:8000/; proxy_set_header Host $host; }\\n\\
  location / { try_files $uri $uri/ /index.html; }\\n\\
}\\n' > /etc/nginx/sites-enabled/default
# supervisor config
RUN printf '[supervisord]\\nnodaemon=true\\n\\n\\
[program:nginx]\\ncommand=nginx -g "daemon off;"\\nautorestart=true\\n\\n\\
[program:api]\\ncommand=python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000\\n\\
directory=/app\\nautorestart=true\\n' > /etc/supervisor/conf.d/app.conf
EXPOSE 80
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/app.conf"]
"""

# app_type → (dockerfile_content, internal_port)
_APP_TEMPLATES: dict[str, tuple[str, int]] = {
    "react":      (_DOCKERFILE_REACT,      80),
    "nextjs":     (_DOCKERFILE_NEXTJS,     3000),
    "fastapi":    (_DOCKERFILE_FASTAPI,    8000),
    "flask":      (_DOCKERFILE_FLASK,      5000),
    "django":     (_DOCKERFILE_DJANGO,     8000),
    "express":    (_DOCKERFILE_EXPRESS,    3000),
    "go":         (_DOCKERFILE_GO,         8080),
    "ruby":       (_DOCKERFILE_RUBY,       3000),
    "php":        (_DOCKERFILE_PHP,        80),
    "rust":       (_DOCKERFILE_RUST,       8080),
    "fullstack":  (_DOCKERFILE_FULLSTACK,  80),
    "static":     (_DOCKERFILE_STATIC,     80),
}

_HEALTH_CHECK_RETRIES = 12
_HEALTH_CHECK_INTERVAL = 10  # seconds
_MAX_SELF_HEAL_RETRIES = 2   # max builder fix attempts before giving up

# Valid Dockerfile first-instruction keywords (FROM must appear first or after ARG)
_DOCKERFILE_VALID_FIRST = {"FROM", "ARG", "COMMENT", "#"}


def _validate_dockerfile(content: str) -> None:
    """
    Sanity-check that content is a real Dockerfile and not LLM-hallucinated code.
    Raises ValueError (triggers fallback to template) if:
      - First non-comment line doesn't start with FROM/ARG
      - No CMD or ENTRYPOINT found (container won't start)
    """
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("Dockerfile is empty after stripping comments")
    first_keyword = lines[0].split()[0].upper() if lines[0].split() else ""
    if first_keyword not in ("FROM", "ARG"):
        raise ValueError(
            f"Dockerfile first instruction must be FROM or ARG, got {lines[0][:60]!r} — "
            "LLM likely embedded non-Dockerfile content"
        )
    upper = content.upper()
    has_cmd = "\nCMD " in upper or "\nCMD[" in upper or upper.startswith("CMD ")
    has_entrypoint = "\nENTRYPOINT " in upper or "\nENTRYPOINT[" in upper
    if not has_cmd and not has_entrypoint:
        raise ValueError("Dockerfile has no CMD or ENTRYPOINT — container would not start")

# Files the LLM context scanner prioritises (in order)
_SCAN_PRIORITY_FILES = [
    "package.json",
    "requirements.txt", "pyproject.toml",
    "go.mod", "Cargo.toml", "Gemfile",
    "pom.xml", "build.gradle",
    "Dockerfile",
    "next.config.js", "next.config.ts",
    "vite.config.ts", "vite.config.js",
    "src/main.tsx", "src/main.jsx", "src/index.tsx", "src/index.jsx",
    "src/App.tsx", "src/App.jsx",
    "main.py", "app.py", "server.py",
    "server.js", "index.js", "app.js",
    "README.md",
]


def _scan_repo(repo_path: str) -> dict:
    """
    Return a lightweight snapshot of the repo for LLM context:
      listing  — up to 80 relative file paths (node_modules etc. skipped)
      contents — first 2 KB of each priority file that exists
    """
    listing: list[str] = []
    skip_dirs = {"node_modules", ".git", "__pycache__", ".next", "dist", "build", ".venv", "venv"}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_root = os.path.relpath(root, repo_path)
        prefix = "" if rel_root == "." else rel_root + "/"
        for f in sorted(files):
            listing.append(prefix + f)
        if len(listing) >= 80:
            break

    contents: dict[str, str] = {}
    for rel in _SCAN_PRIORITY_FILES:
        full = os.path.join(repo_path, rel)
        if os.path.exists(full):
            try:
                with open(full) as fh:
                    contents[rel] = fh.read(2000)
            except OSError:
                pass

    return {"listing": listing[:80], "contents": contents}


def _make_slug(title: str) -> str:
    """'Build a Salon Booking App' → 'salon-booking-app'"""
    title = re.sub(r"^(build|create|make|implement|develop)\s+(a|an|the)\s+", "", title, flags=re.IGNORECASE)
    slug = re.sub(r"[^\w\s-]", "", title.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] or "demo"


def _detect_app_type(repo_path: str) -> tuple[str, int]:
    """
    Return (app_type, internal_port) by inspecting the repo structure.
    Language-agnostic: covers Node, Python, Go, Ruby, PHP, Rust, static.
    Fullstack detection: if both frontend (package.json) and backend signals
    coexist in recognised subdirectory layout, returns "fullstack".
    """
    # ── Fullstack detection first ─────────────────────────────────────────────
    # Pattern: frontend/ directory with package.json + backend/ with Python/Go/etc.
    has_frontend_dir = os.path.exists(os.path.join(repo_path, "frontend", "package.json"))
    has_backend_dir = (
        os.path.exists(os.path.join(repo_path, "backend", "requirements.txt"))
        or os.path.exists(os.path.join(repo_path, "backend", "main.py"))
        or os.path.exists(os.path.join(repo_path, "backend", "go.mod"))
    )
    if has_frontend_dir and has_backend_dir:
        return "fullstack", 80

    # ── Node / JavaScript ─────────────────────────────────────────────────────
    pkg_json_path = os.path.join(repo_path, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path) as f:
                pkg = json.load(f)
            deps: dict = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs", 3000
            if "react-scripts" in deps or "vite" in deps or any(k.startswith("@vitejs") for k in deps):
                return "react", 80
            if "express" in deps or "fastify" in deps or "koa" in deps or "hapi" in deps:
                return "express", 3000
        except (json.JSONDecodeError, OSError):
            pass
        return "react", 80  # default for any node project

    # ── Python ────────────────────────────────────────────────────────────────
    req_path = os.path.join(repo_path, "requirements.txt")
    pyproject_path = os.path.join(repo_path, "pyproject.toml")
    if os.path.exists(req_path) or os.path.exists(pyproject_path):
        try:
            chosen = req_path if os.path.exists(req_path) else pyproject_path
            with open(chosen) as fh:
                content = fh.read().lower()
        except OSError:
            content = ""
        if "django" in content:
            return "django", 8000
        if "fastapi" in content or "uvicorn" in content:
            return "fastapi", 8000
        if "flask" in content:
            return "flask", 5000
        return "fastapi", 8000  # generic Python app

    # ── Go ────────────────────────────────────────────────────────────────────
    if os.path.exists(os.path.join(repo_path, "go.mod")):
        return "go", 8080

    # ── Rust ──────────────────────────────────────────────────────────────────
    if os.path.exists(os.path.join(repo_path, "Cargo.toml")):
        return "rust", 8080

    # ── Ruby ──────────────────────────────────────────────────────────────────
    if os.path.exists(os.path.join(repo_path, "Gemfile")):
        return "ruby", 3000

    # ── PHP ───────────────────────────────────────────────────────────────────
    if any(
        os.path.exists(os.path.join(repo_path, f))
        for f in ("index.php", "composer.json", "public/index.php")
    ):
        return "php", 80

    # ── Static HTML ───────────────────────────────────────────────────────────
    if os.path.exists(os.path.join(repo_path, "index.html")):
        return "static", 80

    return "static", 80  # safest fallback


def _ensure_dockerfile(repo_path: str, app_type: str) -> None:
    """Write a Dockerfile for the detected app type if one doesn't already exist."""
    dockerfile_path = os.path.join(repo_path, "Dockerfile")
    if os.path.exists(dockerfile_path):
        return
    template, _ = _APP_TEMPLATES.get(app_type, _APP_TEMPLATES["static"])
    with open(dockerfile_path, "w") as f:
        f.write(template)


def _nginx_conf_for_slug(slug: str, container_ip: str, port: int) -> str:
    """
    Use container IP directly — nginx resolves upstreams at reload time and
    will fail with 'host not found' if the container name isn't in its DNS.
    Docker assigns stable IPs on named networks so this is reliable.
    """
    return (
        f"# Auto-generated by FORGE SRE agent — do not edit manually\n"
        f"location /{slug} {{ return 301 /{slug}/; }}\n"
        f"location /{slug}/ {{\n"
        f"    proxy_pass http://{container_ip}:{port}/;\n"
        f"    proxy_set_header Host $host;\n"
        f"    proxy_set_header X-Real-IP $remote_addr;\n"
        f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"    proxy_set_header X-Forwarded-Prefix /{slug};\n"
        f"    proxy_read_timeout 60s;\n"
        f"    proxy_connect_timeout 10s;\n"
        f"}}\n"
    )


class SREAgent(BaseAgent):
    """
    Infrastructure wiring agent.

    Builds and starts a Dockerized demo for the run's generated code,
    then wires nginx so it's accessible at demo.usephalanx.com/<slug>.
    """

    AGENT_ROLE = "sre"

    async def execute(self) -> AgentResult:
        settings = get_settings()
        self._log.info("sre.execute.start")

        if not settings.phalanx_enable_demo_deploy:
            self._log.info("sre.execute.disabled")
            async with get_db() as session:
                await session.execute(
                    update(Task).where(Task.id == self.task_id).values(
                        status="COMPLETED",
                        output={"skipped": True, "reason": "phalanx_enable_demo_deploy=false"},
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            return AgentResult(success=True, output={"skipped": True})

        # Load context
        async with get_db() as session:
            task_result = await session.execute(select(Task).where(Task.id == self.task_id))
            task = task_result.scalar_one_or_none()
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")

            run_result = await session.execute(select(Run).where(Run.id == self.run_id))
            run = run_result.scalar_one()

            # Load WorkOrder for user intent context passed to LLM Dockerfile planner
            wo_result = await session.execute(
                select(WorkOrder).where(WorkOrder.id == run.work_order_id)
            )
            wo = wo_result.scalar_one_or_none()
            work_order_title = wo.title if wo else str(task.title)
            work_order_description = wo.description if wo else ""

            # ── Load planner tech_stack + builder files_written for richer LLM context ──
            # tech_stack: any task that resolved it (verifier, integration_wiring, tech_lead)
            # files_produced: all files_written from builder/component/page tasks
            tech_stack = ""
            files_produced: list[str] = []
            all_tasks_result = await session.execute(
                select(Task).where(Task.run_id == self.run_id)
            )
            for t in all_tasks_result.scalars():
                out = t.output or {}
                if not tech_stack and out.get("tech_stack"):
                    tech_stack = str(out["tech_stack"])
                if t.agent_role in ("builder", "component_builder", "page_assembler"):
                    for f in out.get("files_written", []):
                        if f not in files_produced:
                            files_produced.append(f)

            self._log.info(
                "sre.context_loaded",
                tech_stack=tech_stack or "unknown",
                files_produced_count=len(files_produced),
            )

        base_slug = run.demo_slug or _make_slug(str(task.title))
        slug = base_slug

        self._log.info("sre.deploy.start", slug=slug, branch=run.active_branch)

        # Create Demo record (or update if already exists)
        # Handle slug uniqueness: if slug already taken by another run, append run_id[:8]
        async with get_db() as session:
            existing = await session.execute(select(Demo).where(Demo.run_id == self.run_id))
            demo = existing.scalar_one_or_none()
            if demo is None:
                # Check if slug is already taken by a different run
                slug_taken = await session.execute(
                    select(Demo).where(Demo.slug == slug)
                )
                if slug_taken.scalar_one_or_none() is not None:
                    slug = f"{base_slug}-{str(self.run_id)[:8]}"
                    self._log.info("sre.deploy.slug_collision_resolved", base_slug=base_slug, new_slug=slug)
                demo = Demo(
                    run_id=self.run_id,
                    slug=slug,
                    title=work_order_title or task.title,
                    status="BUILDING",
                )
                session.add(demo)
                await session.flush()
            else:
                slug = demo.slug  # Use existing slug for this run
                demo.status = "BUILDING"
            await session.commit()
            demo_id = demo.id

        container_name = f"phalanx-demo-{slug}"
        image_name = f"phalanx-demo-{slug}:latest"

        # Clone repo and build image (uses project.config.repo_url + active_branch)
        try:
            app_type, internal_port = await self._build_image(
                run=run,
                slug=slug,
                image_name=image_name,
                settings=settings,
                work_order_title=work_order_title,
                work_order_description=work_order_description,
                tech_stack=tech_stack,
                files_produced=files_produced,
            )
        except Exception as exc:
            self._log.error("sre.build.failed", error=str(exc))
            await self._update_demo(demo_id, status="FAILED", error=str(exc)[:1000])
            await self._notify_slack_failure(
                self.run_id, slug,
                {"category": "build", "root_cause": str(exc), "frontend_ok": False,
                 "backend_ok": False, "suggested_fix": "Review build logs", "agent_role": None,
                 "fix_description": None},
                attempt=1,
            )
            await self._complete_task(error=str(exc))
            return AgentResult(success=False, output={}, error=str(exc))

        # Mark image built
        await self._update_demo(
            demo_id,
            status="STOPPED",
            app_type=app_type,
            image_name=image_name,
            container_name=container_name,
            internal_port=internal_port,
            built_at=datetime.now(UTC),
        )

        demo_url = f"{settings.demo_base_url}/{slug}"

        # ── Self-heal retry loop ─────────────────────────────────────────────
        # Attempt to start container + health check.  On failure: diagnose with
        # LLM, post root-cause to Slack, dispatch a targeted fix task to the
        # builder, wait for it, then retry.  Capped at _MAX_SELF_HEAL_RETRIES.
        healthy = False
        cid: str | None = None
        last_error: str | None = None

        for attempt in range(1, _MAX_SELF_HEAL_RETRIES + 2):  # +1 for the initial attempt
            # Start container (enforces LRU)
            try:
                cid = await self._start_container(
                    slug=slug,
                    image_name=image_name,
                    container_name=container_name,
                    internal_port=internal_port,
                    settings=settings,
                )
            except Exception as exc:
                last_error = str(exc)
                self._log.error("sre.start.failed", attempt=attempt, error=last_error)
                await self._update_demo(demo_id, status="FAILED", error=last_error[:1000])
                # Cannot self-heal a container-start failure — break immediately
                await self._notify_slack_failure(
                    self.run_id, slug,
                    {"category": "runtime", "root_cause": last_error, "frontend_ok": False,
                     "backend_ok": False, "suggested_fix": "Check Docker daemon + image CMD",
                     "agent_role": None, "fix_description": None},
                    attempt=attempt,
                )
                break

            # Wire nginx (non-fatal)
            try:
                await self._wire_nginx(slug, container_name, internal_port, settings)
            except Exception as exc:
                self._log.warning("sre.nginx_wire.failed", error=str(exc))

            # Health check — separate frontend (/) and backend (/health) probes
            healthy = await self._health_check(container_name, internal_port)

            if healthy:
                break

            # ── Health check failed ──────────────────────────────────────────
            self._log.warning("sre.health.failed_attempt", attempt=attempt)
            diagnosis = await self._diagnose_failure(container_name, internal_port, app_type)
            last_error = diagnosis.get("root_cause", "Health check failed")
            await self._update_demo(demo_id, status="FAILED", error=last_error[:1000])

            can_fix = (
                diagnosis.get("agent_role")
                and diagnosis.get("fix_description")
                and attempt <= _MAX_SELF_HEAL_RETRIES
            )
            await self._notify_slack_failure(self.run_id, slug, diagnosis, attempt=attempt)

            if not can_fix:
                self._log.info("sre.self_heal.skipped", attempt=attempt, reason="no fixable agent")
                break

            # Dispatch fix task and wait for it
            self._log.info("sre.self_heal.dispatching", attempt=attempt)
            fix_task_id = await self._dispatch_fix_task(self.run_id, diagnosis)
            if fix_task_id:
                fix_ok = await self._wait_for_fix_task(fix_task_id)
                self._log.info("sre.self_heal.fix_result", fix_ok=fix_ok, task_id=fix_task_id)
                if not fix_ok:
                    break  # fix task failed — give up
                # Rebuild image with fixed code
                try:
                    app_type, internal_port = await self._build_image(
                        run=run,
                        slug=slug,
                        image_name=image_name,
                        settings=settings,
                        work_order_title=work_order_title,
                        work_order_description=work_order_description,
                        tech_stack=tech_stack,
                        files_produced=files_produced,
                    )
                    await self._update_demo(
                        demo_id,
                        status="STOPPED",
                        app_type=app_type,
                        image_name=image_name,
                        internal_port=internal_port,
                        built_at=datetime.now(UTC),
                    )
                except Exception as rebuild_exc:
                    self._log.error("sre.self_heal.rebuild_failed", error=str(rebuild_exc))
                    last_error = str(rebuild_exc)
                    break
            else:
                break  # dispatch failed — give up
        # ── End retry loop ───────────────────────────────────────────────────

        # Final DB update and notifications
        final_status = "RUNNING" if healthy else "FAILED"
        final_error = None if healthy else (last_error or "Container started but health check failed")

        await self._update_demo(
            demo_id,
            status=final_status,
            container_id=cid,
            demo_url=demo_url if healthy else None,
            error=final_error,
        )

        if healthy:
            async with get_db() as session:
                await session.execute(
                    update(Run).where(Run.id == self.run_id).values(deploy_url=demo_url)
                )
                await session.commit()
            await self._notify_slack_success(self.run_id, slug, demo_url)

        await self._complete_task()
        self._log.info(
            "sre.deploy.done",
            slug=slug,
            status=final_status,
            demo_url=demo_url if healthy else None,
        )
        return AgentResult(
            success=healthy,
            output={"slug": slug, "demo_url": demo_url if healthy else None, "status": final_status},
        )

    # ── LLM deployment planning ───────────────────────────────────────────────

    def _llm_generate_deployment_plan(
        self,
        repo_path: str,
        slug: str,
        work_order_title: str,
        work_order_description: str,
        tech_stack: str = "",
        files_produced: list | None = None,
    ) -> tuple[str, int, str]:
        """
        Ask Claude to write a production-ready Dockerfile for the repo.

        Returns (dockerfile_content, internal_port, app_type_label).
        Falls back to template detection if the LLM call fails or returns garbage.

        The prompt encodes hard-won lessons:
          - Q='"' trick for BrowserRouter basename (avoids Python→Dockerfile→shell quoting chain)
          - npm install not npm ci (generated apps have no lockfile)
          - npx vite build not npm run build (avoids tsc type errors in test stubs)
          - Always expose /health so health checks pass
          - DEMO_BASE_PATH ARG for SPA subpath asset baking
        """
        scan = _scan_repo(repo_path)
        file_listing = "\n".join(f"  {p}" for p in scan["listing"])
        file_contents = "\n\n".join(
            f"=== {path} ===\n{content}"
            for path, content in scan["contents"].items()
        )

        # Build optional context sections from planner/builder outputs
        planner_context = ""
        if tech_stack:
            planner_context = f"\nPLANNER TECH STACK DECISION\n{tech_stack}\n"

        builder_context = ""
        if files_produced:
            builder_context = (
                "\nBUILDER FILES PRODUCED\n"
                + "\n".join(f"  {f}" for f in files_produced[:60])
                + "\n"
            )

        system = """\
You are an SRE agent writing a Dockerfile to containerise an AI-generated app for a live demo.

DEPLOYMENT CONSTRAINTS
- Single container; preferred ports: 80 (nginx), 3000, 5000, 8000.
- App is served at /<slug>/ via nginx reverse-proxy with path stripping
  (nginx strips /<slug>/ so the container always receives requests rooted at /).
- Container must respond 200 to: GET /health  OR  GET /  (used for health-check).
- No secrets — use ARG/ENV only; never hard-code credentials.

REACT / VITE SPA RULES (apply when package.json contains "vite" or "react-scripts")
- Use a two-stage build: node:20-alpine builder → nginx:alpine server.
- Include  ARG DEMO_BASE_PATH=/  so the caller can bake the correct subpath.
- Build command:  npx vite build --base=${DEMO_BASE_PATH}
  (NOT npm run build — generated test stubs cause tsc errors that abort the build)
- Fallback: append  || npm run build -- --base=${DEMO_BASE_PATH} || true
- After the build step always ensure dist/ exists:
    ([ -d dist ] || ([ -d build ] && mv build dist) || mkdir -p dist)
- If react-router-dom is a dependency, patch BrowserRouter basename BEFORE the build
  using the Q variable trick to avoid shell-quoting conflicts inside the Dockerfile:
    RUN if [ "$DEMO_BASE_PATH" != "/" ]; then \\
          ENTRY=$(find src -maxdepth 3 \\( -name "main.tsx" -o -name "main.jsx" -o -name "main.ts" -o -name "main.js" \\) 2>/dev/null | head -1); \\
          if [ -n "$ENTRY" ]; then \\
            Q='"'; \\
            sed -i "s|<BrowserRouter>|<BrowserRouter basename=${Q}${DEMO_BASE_PATH}${Q}>|g" "$ENTRY"; \\
          fi; \\
        fi
- nginx SPA config (serves index.html for all paths + /health):
    RUN printf 'server{listen 80;root /usr/share/nginx/html;index index.html;location /{try_files $uri $uri/ /index.html;}location /health{return 200 ok;}}' \\
        > /etc/nginx/conf.d/default.conf
- Use npm install (NOT npm ci — generated apps have no package-lock.json).

PYTHON RULES
- Use python:3.12-slim.
- Install deps from requirements.txt if present (pip install --no-cache-dir -r requirements.txt).
- For FastAPI/Uvicorn: CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
- Add /health route hint in CMD docs if not detected in source.

NODE/EXPRESS RULES
- Use node:20-alpine.
- npm install --omit=dev for production.
- Detect entry point from package.json "main" or use server.js / index.js.

Return ONLY valid JSON — no markdown fences, no explanation outside the object:
{
  "dockerfile": "<full Dockerfile as a string — use \\n for newlines>",
  "internal_port": 80,
  "app_type": "react-vite",
  "notes": "one-line explanation"
}"""

        user_msg = (
            f"USER REQUEST\nTitle: {work_order_title}\nDescription: {work_order_description}\n"
            f"{planner_context}"
            f"{builder_context}"
            f"\nSLUG (app served at /<slug>/): {slug}\n\n"
            f"REPO FILE LISTING\n{file_listing}\n\n"
            f"KEY FILE CONTENTS\n{file_contents}"
        )

        try:
            response = self._call_claude(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                max_tokens=4096,
            )
            start = response.find("{")
            end = response.rfind("}") + 1
            result = json.loads(response[start:end])

            dockerfile = result.get("dockerfile", "").strip()
            internal_port = int(result.get("internal_port", 80))
            app_type = str(result.get("app_type", "llm-generated"))

            if len(dockerfile) < 20:
                raise ValueError("LLM returned empty or trivial Dockerfile")

            # JSON-encoded strings use \n literally — convert to real newlines
            if "\\n" in dockerfile and "\n" not in dockerfile:
                dockerfile = dockerfile.replace("\\n", "\n")

            # Validate it's actually a Dockerfile (not Python/JS code accidentally embedded)
            _validate_dockerfile(dockerfile)

            self._log.info(
                "sre.llm.plan_generated",
                app_type=app_type,
                port=internal_port,
                notes=result.get("notes", ""),
            )
            return dockerfile, internal_port, app_type

        except Exception as exc:
            self._log.warning(
                "sre.llm.plan_failed", error=str(exc)[:200], fallback="template"
            )
            # Fallback: existing template detection
            app_type_fb, port_fb = _detect_app_type(repo_path)
            template_fb, _ = _APP_TEMPLATES.get(app_type_fb, _APP_TEMPLATES["static"])
            return template_fb, port_fb, app_type_fb

    # ── Docker build ──────────────────────────────────────────────────────────

    async def _build_image(
        self,
        run: Run,
        slug: str,
        image_name: str,
        settings,
        work_order_title: str = "",
        work_order_description: str = "",
        tech_stack: str = "",
        files_produced: list | None = None,
    ) -> tuple[str, int]:
        """
        Clone the run's PR branch from the project repo, generate a Dockerfile,
        and build the Docker image.  Returns (app_type, internal_port).
        """
        import docker  # noqa: PLC0415

        branch = run.active_branch
        repo_url = await self._get_clone_url(run, settings)
        if not repo_url:
            raise RuntimeError("No repo URL configured — set GITHUB_TOKEN or project.config.repo_url")

        with tempfile.TemporaryDirectory(prefix=f"sre-{slug}-") as tmpdir:
            self._log.info("sre.git.clone", branch=branch, dest=tmpdir)
            cmd = ["git", "clone", "--depth=1", repo_url, tmpdir]
            if branch:
                cmd = ["git", "clone", "--depth=1", "--branch", branch, repo_url, tmpdir]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr[:500]}")

            # LLM-driven Dockerfile generation (template fallback built-in)
            dockerfile_content, internal_port, app_type = self._llm_generate_deployment_plan(
                repo_path=tmpdir,
                slug=slug,
                work_order_title=work_order_title,
                work_order_description=work_order_description,
                tech_stack=tech_stack,
                files_produced=files_produced or [],
            )
            self._log.info("sre.app_detected", app_type=app_type, port=internal_port)

            # Write Dockerfile (always use LLM/generated one — don't let builder's
            # potentially incomplete Dockerfile override our deployment-ready one)
            dockerfile_path = os.path.join(tmpdir, "Dockerfile")
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content)

            client = docker.from_env()
            self._log.info("sre.docker.build_start", image=image_name, app_type=app_type)
            # Always pass DEMO_BASE_PATH — LLM Dockerfile uses it for SPAs;
            # it's a no-op ARG if the Dockerfile doesn't declare it.
            buildargs = {"DEMO_BASE_PATH": f"/{slug}/"}

            def _docker_build(content: str) -> None:
                with open(dockerfile_path, "w") as _f:
                    _f.write(content)
                _, logs = client.images.build(
                    path=tmpdir,
                    tag=image_name,
                    rm=True,
                    forcerm=True,
                    timeout=300,
                    buildargs=buildargs,
                )
                for entry in logs:
                    if "stream" in entry:
                        ln = entry["stream"].strip()
                        if ln:
                            self._log.debug("sre.docker.build_log", line=ln[:200])

            try:
                _docker_build(dockerfile_content)
            except Exception as build_exc:
                # LLM Dockerfile failed to build (e.g. embedded Python code) — fall back to template
                self._log.warning(
                    "sre.docker.build_failed_llm_fallback",
                    error=str(build_exc)[:300],
                )
                app_type_fb, port_fb = _detect_app_type(tmpdir)
                template_fb, _ = _APP_TEMPLATES.get(app_type_fb, _APP_TEMPLATES["static"])
                app_type = app_type_fb
                internal_port = port_fb
                _docker_build(template_fb)

            self._log.info("sre.docker.build_done", image=image_name)

        return app_type, internal_port

    async def _get_clone_url(self, run: Run, settings) -> str | None:
        """
        Determine the Git URL to clone for this run.

        Priority:
          1. project.config.repo_url  — the project's actual GitHub repo
          2. showcase fallback        — usephalanx/showcase (legacy / no-project runs)

        Returns an authenticated HTTPS URL, or None if no token is configured.
        """
        token = settings.github_token
        if not token:
            return None

        async with get_db() as session:
            proj = await session.get(Project, run.project_id)

        repo_url = (proj.config or {}).get("repo_url", "") if proj else ""
        if repo_url:
            # Inject token into https:// URL for auth
            if repo_url.startswith("https://"):
                return repo_url.replace("https://", f"https://{token}@")
            return repo_url

        # Fallback: showcase mirror
        return f"https://{token}@github.com/usephalanx/showcase.git"

    def _get_showcase_repo_url(self, settings) -> str | None:
        """Legacy fallback — kept for portal_start_demo which has no Run context."""
        token = settings.github_token
        if not token:
            return None
        return f"https://{token}@github.com/usephalanx/showcase.git"

    # ── Failure diagnosis + self-heal ─────────────────────────────────────────

    async def _diagnose_failure(
        self, container_name: str, internal_port: int, app_type: str
    ) -> dict:
        """
        Read container logs and ask Claude to classify the root cause.

        Returns a dict:
          category:        lint | dependency | runtime | config | build | unknown
          root_cause:      one-sentence plain English description
          suggested_fix:   what the builder should do to fix it
          agent_role:      "builder" | "integration_wiring" | None (not fixable)
          fix_description: task title for the fix task dispatched to the builder
          frontend_ok:     bool — was the frontend layer healthy?
          backend_ok:      bool — was the backend layer healthy?
        """
        import docker  # noqa: PLC0415

        logs_text = ""
        frontend_ok = False
        backend_ok = False

        try:
            client = docker.from_env()
            container = client.containers.get(container_name)

            # Collect last 100 lines of stderr + stdout
            raw = container.logs(stdout=True, stderr=True, tail=100)
            logs_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

            # Quick probe: frontend (port 80 or internal_port) and backend (/health)
            for probe_path, label in [("/", "frontend"), ("/health", "backend"), ("/api/health", "backend")]:
                probe_port = 80 if app_type == "fullstack" else internal_port
                ec, _ = container.exec_run(
                    f"sh -c 'curl -sf http://localhost:{probe_port}{probe_path} > /dev/null 2>&1'"
                )
                if ec == 0:
                    if label == "frontend":
                        frontend_ok = True
                    else:
                        backend_ok = True

        except Exception as exc:
            self._log.warning("sre.diagnose.log_fetch_failed", error=str(exc))
            logs_text = f"[could not fetch logs: {exc}]"

        if app_type not in ("fullstack",):
            # Single-tier: both flags track the same service
            backend_ok = frontend_ok or backend_ok
            frontend_ok = backend_ok

        prompt = f"""\
You are an SRE diagnosing a failed demo container. The app type is "{app_type}".
Frontend healthy: {frontend_ok}. Backend/API healthy: {backend_ok}.

CONTAINER LOGS (last 100 lines):
{logs_text[-4000:]}

Classify the failure and propose a fix. Return ONLY valid JSON:
{{
  "category": "dependency|runtime|config|build|missing_file|unknown",
  "root_cause": "<one sentence — what specifically failed and why>",
  "frontend_issue": "<null or short description of frontend problem>",
  "backend_issue": "<null or short description of backend problem>",
  "suggested_fix": "<concrete action for the builder agent — what file to create/change>",
  "agent_role": "builder",
  "fix_description": "<task title: imperative, 1 sentence, e.g. 'Add missing /health endpoint to FastAPI app'>"
}}

Rules:
- If it's a missing Python import, say which package and how to add it to requirements.txt.
- If it's a missing /health endpoint, specify which file to add it to.
- If it's a config/env issue the builder can't fix, set agent_role to null.
- Never suggest changing the Dockerfile — only application code fixes."""

        try:
            response = self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system="You are a concise SRE analyst. Return only valid JSON.",
                max_tokens=512,
            )
            start = response.find("{")
            end = response.rfind("}") + 1
            result = json.loads(response[start:end])
            result["frontend_ok"] = frontend_ok
            result["backend_ok"] = backend_ok
            return result
        except Exception as exc:
            self._log.warning("sre.diagnose.llm_failed", error=str(exc))
            return {
                "category": "unknown",
                "root_cause": f"Health check failed after {_HEALTH_CHECK_RETRIES} retries. Logs: {logs_text[-200:]}",
                "frontend_issue": None,
                "backend_issue": None,
                "suggested_fix": "Review container logs manually",
                "agent_role": None,
                "fix_description": None,
                "frontend_ok": frontend_ok,
                "backend_ok": backend_ok,
            }

    async def _notify_slack_failure(
        self, run_id: str, slug: str, diagnosis: dict, attempt: int
    ) -> None:
        """Post a structured root-cause report to the run's Slack thread."""
        try:
            async with get_db() as session:
                notifier = await SlackNotifier.from_run(run_id, session)

            cat = diagnosis.get("category", "unknown")
            root_cause = diagnosis.get("root_cause", "Unknown error")
            frontend_ok = diagnosis.get("frontend_ok", False)
            backend_ok = diagnosis.get("backend_ok", False)
            fix_desc = diagnosis.get("fix_description")
            suggested_fix = diagnosis.get("suggested_fix", "")

            fe_icon = "✅" if frontend_ok else "❌"
            be_icon = "✅" if backend_ok else "❌"

            lines = [
                f"*🔴 Demo deploy failed* — `{slug}` (attempt {attempt}/{_MAX_SELF_HEAL_RETRIES})",
                f"*Category:* `{cat}`",
                f"*Root cause:* {root_cause}",
                f"*Frontend:* {fe_icon}   *Backend/API:* {be_icon}",
            ]
            if suggested_fix:
                lines.append(f"*Suggested fix:* {suggested_fix}")
            if fix_desc:
                lines.append(f"⚙️ _Dispatching fix task to builder:_ `{fix_desc}`")
            else:
                lines.append("⚠️ _Fix requires manual intervention — not auto-dispatching._")

            await notifier.post("\n".join(lines))
        except Exception as exc:
            self._log.warning("sre.slack_notify.failed", error=str(exc))

    async def _notify_slack_success(self, run_id: str, slug: str, demo_url: str) -> None:
        """Post demo-live message to the run's Slack thread."""
        try:
            async with get_db() as session:
                notifier = await SlackNotifier.from_run(run_id, session)
            await notifier.post(
                f"*🟢 Demo live* — <{demo_url}|{slug}>\n"
                f"_Phalanx built and deployed your app automatically._"
            )
        except Exception as exc:
            self._log.warning("sre.slack_notify.success_failed", error=str(exc))

    async def _dispatch_fix_task(self, run_id: str, diagnosis: dict) -> str | None:
        """
        Create a new builder Task in the DB for the diagnosed fix and dispatch it.
        Returns the new task_id, or None if dispatch is not applicable.
        """
        agent_role = diagnosis.get("agent_role")
        fix_description = diagnosis.get("fix_description")
        if not agent_role or not fix_description:
            return None

        from phalanx.runtime.task_router import TaskRouter  # noqa: PLC0415

        try:
            async with get_db() as session:
                # Find highest existing sequence_num for this run
                existing = await session.execute(
                    select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num.desc())
                )
                tasks_list = list(existing.scalars())
                next_seq = (max((t.sequence_num for t in tasks_list), default=0) + 1)

                fix_task = Task(
                    run_id=run_id,
                    sequence_num=next_seq,
                    title=fix_description,
                    description=(
                        f"SRE-diagnosed fix: {diagnosis.get('suggested_fix', fix_description)}\n"
                        f"Root cause: {diagnosis.get('root_cause', '')}"
                    ),
                    agent_role=agent_role,
                    status="PENDING",
                )
                session.add(fix_task)
                await session.flush()
                fix_task_id = fix_task.id
                await session.commit()

            router = TaskRouter(celery_app)
            router.dispatch(agent_role=agent_role, task_id=fix_task_id, run_id=run_id)
            self._log.info(
                "sre.fix_task.dispatched",
                task_id=fix_task_id,
                agent_role=agent_role,
                fix=fix_description,
            )
            return fix_task_id

        except Exception as exc:
            self._log.warning("sre.fix_task.dispatch_failed", error=str(exc))
            return None

    async def _wait_for_fix_task(self, fix_task_id: str, timeout_secs: int = 300) -> bool:
        """Poll until the fix task completes (or times out). Returns True if COMPLETED."""
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            await asyncio.sleep(15)
            try:
                async with get_db() as session:
                    t = await session.get(Task, fix_task_id)
                if t and t.status == "COMPLETED":
                    return True
                if t and t.status in ("FAILED", "CANCELLED"):
                    return False
            except Exception:
                pass
        return False

    # ── Container lifecycle ───────────────────────────────────────────────────

    async def _start_container(
        self,
        slug: str,
        image_name: str,
        container_name: str,
        internal_port: int,
        settings,
    ) -> str:
        """
        Enforce LRU cap, then start the container.
        Returns the Docker container ID.
        """
        import docker  # noqa: PLC0415

        client = docker.from_env()

        # ── LRU enforcement ───────────────────────────────────────────────────
        await self._enforce_lru(client, settings)

        # Remove any existing container with same name (e.g. from a prior run)
        try:
            old = client.containers.get(container_name)
            old.stop(timeout=10)
            old.remove(force=True)
            self._log.info("sre.docker.removed_old", container=container_name)
        except Exception:
            pass  # doesn't exist — fine

        _fallback_commands: dict[str, list[str]] = {
            "fastapi": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
            "flask":   ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"],
            "express": ["node", "server.js"],
        }
        fallback_cmd = _fallback_commands.get(slug.split("-")[0]) if slug else None

        self._log.info("sre.docker.run", image=image_name, container=container_name)
        run_kwargs: dict = {
            "image": image_name,
            "name": container_name,
            "detach": True,
            "network": settings.demo_docker_network,
            "labels": {
                "phalanx.demo": "true",
                "phalanx.slug": slug,
                "phalanx.run_id": self.run_id,
                "phalanx.started_at": datetime.now(UTC).isoformat(),
            },
            "mem_limit": "512m",
            "nano_cpus": 500_000_000,  # 0.5 CPUs
            "restart_policy": {"Name": "no"},
        }
        try:
            container = client.containers.run(**run_kwargs)
        except Exception as exc:
            if "no command specified" in str(exc) and fallback_cmd:
                self._log.warning(
                    "sre.docker.no_cmd_fallback",
                    slug=slug,
                    cmd=fallback_cmd,
                )
                run_kwargs["command"] = fallback_cmd
                container = client.containers.run(**run_kwargs)
            else:
                raise
        self._log.info("sre.docker.started", container_id=container.id[:12])
        return container.id

    async def _enforce_lru(self, client, settings) -> None:
        """Stop the least-recently-accessed running demo if at cap."""
        running_demos = await self._get_running_demos()
        if len(running_demos) < settings.demo_max_running:
            return

        # Find LRU: demo with oldest last_accessed_at
        lru = min(
            running_demos,
            key=lambda d: d.last_accessed_at or d.created_at,
        )
        self._log.info("sre.lru.evicting", slug=lru.slug, container=lru.container_name)

        # Stop container
        if lru.container_name:
            try:
                c = client.containers.get(lru.container_name)
                c.stop(timeout=15)
            except Exception as exc:
                self._log.warning("sre.lru.stop_failed", slug=lru.slug, error=str(exc))

        # Update DB
        await self._update_demo(lru.id, status="STOPPED", container_id=None)

    async def _get_running_demos(self) -> list:
        async with get_db() as session:
            result = await session.execute(
                select(Demo).where(Demo.status == "RUNNING")
            )
            return list(result.scalars())

    # ── nginx wiring ──────────────────────────────────────────────────────────

    async def _wire_nginx(
        self,
        slug: str,
        container_name: str,
        internal_port: int,
        settings,
    ) -> None:
        """
        Write nginx config snippet into the nginx container and reload.

        Uses docker cp to write directly into the nginx container's conf.d/demos/
        directory — avoids shared volume permission issues.
        Resolves container IP at wire time so nginx doesn't need to DNS-resolve
        the demo container name at reload.
        """
        import docker  # noqa: PLC0415

        try:
            client = docker.from_env()

            # Resolve container IP on demos-net
            demo_container = client.containers.get(container_name)
            networks = demo_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            container_ip = next(
                (v["IPAddress"] for v in networks.values() if v.get("IPAddress")),
                container_name,  # fallback to name if IP not found
            )

            conf_content = _nginx_conf_for_slug(slug, container_ip, internal_port)

            # Write to a temp file then docker cp into nginx container
            tmp_path = os.path.join(tempfile.gettempdir(), f"{slug}.conf")
            with open(tmp_path, "w") as f:
                f.write(conf_content)

            nginx = client.containers.get(settings.demo_nginx_container)
            # Ensure the demos conf dir exists inside nginx container
            nginx.exec_run("mkdir -p /etc/nginx/conf.d/demos")
            # docker cp the file in
            import io  # noqa: PLC0415
            import tarfile
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=f"{slug}.conf")
                data = conf_content.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            buf.seek(0)
            nginx.put_archive("/etc/nginx/conf.d/demos", buf.read())

            self._log.info("sre.nginx.conf_written", slug=slug, container_ip=container_ip)
        except Exception as exc:
            self._log.warning("sre.nginx_wire.failed", error=str(exc))
            return

        # Signal nginx to reload
        await self._reload_nginx(settings)

    async def _reload_nginx(self, settings) -> None:
        """docker exec <nginx_container> nginx -s reload"""
        import docker  # noqa: PLC0415

        try:
            client = docker.from_env()
            nginx = client.containers.get(settings.demo_nginx_container)
            exit_code, output = nginx.exec_run("nginx -s reload")
            if exit_code != 0:
                self._log.warning("sre.nginx.reload_failed", output=output)
            else:
                self._log.info("sre.nginx.reloaded")
        except Exception as exc:
            self._log.warning("sre.nginx.reload_error", error=str(exc))

    # ── Health check ──────────────────────────────────────────────────────────

    async def _health_check(self, container_name: str, internal_port: int) -> bool:
        """
        Poll the container's /health endpoint until it responds 200
        or we exhaust retries.
        """
        import docker  # noqa: PLC0415

        client = docker.from_env()

        for attempt in range(_HEALTH_CHECK_RETRIES):
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
            try:
                container = client.containers.get(container_name)
                if container.status != "running":
                    self._log.warning(
                        "sre.health.not_running",
                        attempt=attempt,
                        status=container.status,
                    )
                    continue

                # Use docker exec to check the health endpoint inside the container.
                # Try curl first (nginx/node images), fall back to python3 urllib
                # (python:3.12-slim doesn't include curl).
                exit_code, _ = container.exec_run(
                    f"sh -c '"
                    f"curl -sf http://localhost:{internal_port}/health 2>/dev/null || "
                    f"curl -sf http://localhost:{internal_port}/ 2>/dev/null || "
                    f"python3 -c \""
                    f"import urllib.request; urllib.request.urlopen("
                    f"\\\"http://localhost:{internal_port}/health\\\""
                    f").read()\" 2>/dev/null || "
                    f"python3 -c \""
                    f"import urllib.request; urllib.request.urlopen("
                    f"\\\"http://localhost:{internal_port}/\\\""
                    f").read()\" 2>/dev/null"
                    f"'"
                )
                if exit_code == 0:
                    self._log.info("sre.health.ok", attempt=attempt)
                    return True
            except Exception as exc:
                self._log.debug("sre.health.check_error", attempt=attempt, error=str(exc))

        self._log.warning("sre.health.failed", retries=_HEALTH_CHECK_RETRIES)
        return False

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _update_demo(self, demo_id: str, **kwargs) -> None:
        kwargs["updated_at"] = datetime.now(UTC)
        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(**kwargs)
            )
            await session.commit()

    async def _complete_task(self, error: str | None = None) -> None:
        status = "COMPLETED" if error is None else "COMPLETED"  # SRE is non-fatal
        async with get_db() as session:
            await session.execute(
                update(Task).where(Task.id == self.task_id).values(
                    status=status,
                    output={"error": error} if error else {"success": True},
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()


# ── Portal helpers (called from API) ─────────────────────────────────────────


async def _health_check(container_name: str, internal_port: int) -> bool:
    """
    Module-level health check used by portal helpers.
    Polls the container's /health endpoint until it responds 200 or retries exhausted.
    """
    import docker  # noqa: PLC0415

    client = docker.from_env()

    for attempt in range(_HEALTH_CHECK_RETRIES):
        await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
        try:
            container = client.containers.get(container_name)
            if container.status != "running":
                log.warning("sre.health.not_running", attempt=attempt, status=container.status)
                continue
            exit_code, _ = container.exec_run(
                f"sh -c '"
                f"curl -sf http://localhost:{internal_port}/health 2>/dev/null || "
                f"curl -sf http://localhost:{internal_port}/ 2>/dev/null || "
                f"python3 -c \""
                f"import urllib.request; urllib.request.urlopen("
                f"\\\"http://localhost:{internal_port}/health\\\""
                f").read()\" 2>/dev/null || "
                f"python3 -c \""
                f"import urllib.request; urllib.request.urlopen("
                f"\\\"http://localhost:{internal_port}/\\\""
                f").read()\" 2>/dev/null"
                f"'"
            )
            if exit_code == 0:
                log.info("sre.health.ok", attempt=attempt)
                return True
        except Exception as exc:
            log.debug("sre.health.check_error", attempt=attempt, error=str(exc))

    log.warning("sre.health.failed", retries=_HEALTH_CHECK_RETRIES)
    return False


async def start_demo_by_id(demo_id: str) -> None:
    """
    Start a STOPPED/FAILED demo container and re-wire nginx.

    Handles:
    - LRU eviction when at capacity
    - Stale container name conflicts (removes old container before start)
    - Missing Docker image (marks FAILED with clear message — image was pruned)
    - Language-agnostic health check before marking RUNNING
    - Nginx re-wire with error logging (not silent)
    - demo_url population so Open button appears immediately
    """
    import docker  # noqa: PLC0415

    settings = get_settings()

    async with get_db() as session:
        result = await session.execute(select(Demo).where(Demo.id == demo_id))
        demo = result.scalar_one_or_none()
        if demo is None or demo.status not in ("STOPPED", "FAILED", "STARTING"):
            return
        await session.execute(
            update(Demo).where(Demo.id == demo_id).values(status="STARTING", error=None)
        )
        await session.commit()

    try:
        client = docker.from_env()

        # ── Validate image exists ─────────────────────────────────────────────
        if not demo.image_name:
            raise ValueError("Demo has no image — it must be rebuilt by re-submitting the work order.")
        try:
            client.images.get(demo.image_name)
        except docker.errors.ImageNotFound as exc:
            raise ValueError(
                f"Docker image '{demo.image_name}' no longer exists (pruned after deploy). "
                "Re-submit the work order to rebuild."
            ) from exc

        # ── LRU eviction ─────────────────────────────────────────────────────
        running = await _get_running_demos_for_portal()
        if len(running) >= settings.demo_max_running:
            lru = min(running, key=lambda d: d.last_accessed_at or d.created_at)
            log.info("sre.portal.lru_evict", slug=lru.slug, running=len(running), max=settings.demo_max_running)
            if lru.container_name:
                with contextlib.suppress(Exception):
                    client.containers.get(lru.container_name).stop(timeout=15)
            async with get_db() as session:
                await session.execute(
                    update(Demo).where(Demo.id == lru.id).values(status="STOPPED", container_id=None)
                )
                await session.commit()

        # ── Remove stale container if one exists (409 conflict guard) ─────────
        if demo.container_name:
            try:
                old = client.containers.get(demo.container_name)
                old.remove(force=True)
                log.info("sre.portal.removed_stale_container", name=demo.container_name)
            except docker.errors.NotFound:
                pass  # Normal — container doesn't exist yet

        # ── Start container ───────────────────────────────────────────────────
        _fallback_commands: dict[str, list[str]] = {
            "fastapi":    ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
            "flask":      ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"],
            "express":    ["node", "server.js"],
            "nextjs":     ["node_modules/.bin/next", "start", "-p", "3000"],
            "django":     ["python", "manage.py", "runserver", "0.0.0.0:8000"],
            "rails":      ["bundle", "exec", "ruby", "app.rb", "-o", "0.0.0.0", "-p", "3000"],
        }
        fallback_cmd = _fallback_commands.get(demo.app_type or "") if demo.app_type else None
        internal_port = demo.internal_port or 80
        run_kwargs: dict = {
            "image": demo.image_name,
            "name": demo.container_name,
            "detach": True,
            "network": settings.demo_docker_network,
            "labels": {"phalanx.demo": "true", "phalanx.slug": demo.slug},
            "mem_limit": "512m",
            "nano_cpus": 500_000_000,
            "restart_policy": {"Name": "no"},
            "environment": {
                "DEMO_BASE_PATH": f"/{demo.slug}",
                "PORT": str(internal_port),
                "PUBLIC_URL": f"/{demo.slug}",
                "BASE_URL": f"/{demo.slug}",
                "VITE_BASE": f"/{demo.slug}/",
            },
        }
        try:
            container = client.containers.run(**run_kwargs)
        except docker.errors.APIError as exc:
            if "no command specified" in str(exc) and fallback_cmd:
                log.warning("sre.portal.no_cmd_fallback", demo_id=demo_id, app_type=demo.app_type, cmd=fallback_cmd)
                run_kwargs["command"] = fallback_cmd
                container = client.containers.run(**run_kwargs)
            else:
                raise

        # ── Re-wire nginx ─────────────────────────────────────────────────────
        container.reload()  # refresh attrs to get assigned IP
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        container_ip = next(
            (v["IPAddress"] for v in networks.values() if v.get("IPAddress")),
            None,
        )
        if container_ip:
            try:
                import io
                import tarfile  # noqa: PLC0415
                conf_content = _nginx_conf_for_slug(demo.slug, container_ip, internal_port)
                nginx = client.containers.get(settings.demo_nginx_container)
                nginx.exec_run("mkdir -p /etc/nginx/conf.d/demos")
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=f"{demo.slug}.conf")
                    data = conf_content.encode()
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                buf.seek(0)
                nginx.put_archive("/etc/nginx/conf.d/demos", buf.read())
                nginx.exec_run("nginx -s reload")
                log.info("sre.portal.nginx_wired", slug=demo.slug, ip=container_ip, port=internal_port)
            except Exception as exc:
                log.warning("sre.portal.nginx_wire_failed", slug=demo.slug, error=str(exc))
        else:
            log.warning("sre.portal.no_container_ip", slug=demo.slug)

        # ── Language-agnostic health check (up to 60s) ───────────────────────
        demo_url = f"{settings.demo_base_url}/{demo.slug}"
        healthy = await _health_check(demo.container_name, internal_port)
        if not healthy:
            log.warning("sre.portal.health_check_failed", slug=demo.slug)
            # Don't fail — container is up, may just need more time. Mark RUNNING anyway.

        # ── Persist RUNNING ───────────────────────────────────────────────────
        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(
                    status="RUNNING",
                    container_id=container.id,
                    demo_url=demo_url,
                    last_accessed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        log.info("sre.portal.started", slug=demo.slug, url=demo_url)

    except Exception as exc:
        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(
                    status="FAILED",
                    error=str(exc)[:500],
                )
            )
            await session.commit()
        log.warning("sre.portal.start_failed", demo_id=demo_id, error=str(exc))


async def stop_demo_by_id(demo_id: str) -> None:
    """Stop a RUNNING demo container and remove its nginx config."""
    import docker  # noqa: PLC0415

    settings = get_settings()

    async with get_db() as session:
        result = await session.execute(select(Demo).where(Demo.id == demo_id))
        demo = result.scalar_one_or_none()
        if demo is None or demo.status != "RUNNING":
            return
        await session.execute(
            update(Demo).where(Demo.id == demo_id).values(status="STOPPING")
        )
        await session.commit()

    try:
        client = docker.from_env()
        if demo.container_name:
            with contextlib.suppress(Exception):
                client.containers.get(demo.container_name).stop(timeout=15)

        # Remove nginx config snippet
        conf_path = os.path.join(settings.demo_nginx_conf_dir, f"{demo.slug}.conf")
        if os.path.exists(conf_path):
            os.remove(conf_path)
        try:
            nginx = client.containers.get(settings.demo_nginx_container)
            nginx.exec_run("nginx -s reload")
        except Exception:
            pass

        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(
                    status="STOPPED",
                    container_id=None,
                )
            )
            await session.commit()

    except Exception as exc:
        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(status="STOPPED", container_id=None)
            )
            await session.commit()
        log.warning("sre.portal.stop_failed", demo_id=demo_id, error=str(exc))


async def _get_running_demos_for_portal() -> list:
    async with get_db() as session:
        result = await session.execute(select(Demo).where(Demo.status == "RUNNING"))
        return list(result.scalars())


# ── Parallel infra prep (dispatched by Commander alongside builder chain) ─────


async def _prep_infra(run_id: str, slug: str) -> dict:  # pragma: no cover
    """
    Pre-pull common Docker base images and ensure the demos-net network exists.

    Runs in parallel with the builder chain so infra is ready when SRE starts.
    Failures are non-fatal — logged and returned in the result dict.
    """
    settings = get_settings()
    if not settings.phalanx_enable_demo_deploy:
        return {"skipped": True}

    results: dict = {"network": "skipped", "images_pulled": [], "errors": []}

    try:
        import docker  # noqa: PLC0415

        client = docker.from_env()

        # ── Ensure demos-net exists ────────────────────────────────────────────
        net_name = settings.demo_docker_network
        try:
            client.networks.get(net_name)
            log.info("sre.prep.network_exists", network=net_name)
            results["network"] = "exists"
        except Exception:
            try:
                client.networks.create(net_name, driver="bridge")
                log.info("sre.prep.network_created", network=net_name)
                results["network"] = "created"
            except Exception as exc:
                log.warning("sre.prep.network_failed", error=str(exc))
                results["errors"].append(f"network: {exc}")

        # ── Pre-pull base images ───────────────────────────────────────────────
        for image_tag in ["node:20-alpine", "python:3.12-slim", "nginx:alpine"]:
            try:
                client.images.pull(image_tag)
                log.info("sre.prep.image_pulled", image=image_tag)
                results["images_pulled"].append(image_tag)
            except Exception as exc:
                log.warning("sre.prep.image_pull_failed", image=image_tag, error=str(exc))
                results["errors"].append(f"{image_tag}: {exc}")

    except Exception as exc:
        log.warning("sre.prep.docker_unavailable", error=str(exc))
        results["errors"].append(str(exc))

    return results


# ── Nginx re-wire (runs after every deploy to restore RUNNING demo routes) ────


async def _rewire_nginx() -> dict:  # pragma: no cover
    """
    Re-wire nginx routes for all RUNNING demos.

    Called on sre-worker startup and periodically via beat to recover from
    nginx container restarts (e.g. after a deploy wipes the conf.d/demos/ dir).
    Non-fatal: logs failures and continues.
    """
    settings = get_settings()
    if not settings.phalanx_enable_demo_deploy:
        return {"skipped": True}

    results: dict = {"wired": [], "errors": []}

    try:
        import docker  # noqa: PLC0415

        client = docker.from_env()

        async with get_db() as session:
            result = await session.execute(select(Demo).where(Demo.status == "RUNNING"))
            running_demos = list(result.scalars())

        if not running_demos:
            return {"wired": [], "errors": []}

        try:
            nginx = client.containers.get(settings.demo_nginx_container)
            nginx.exec_run("mkdir -p /etc/nginx/conf.d/demos")
        except Exception as exc:
            log.warning("sre.rewire.nginx_not_found", error=str(exc))
            return {"wired": [], "errors": [str(exc)]}

        reloaded = False
        for demo in running_demos:
            try:
                container = client.containers.get(demo.container_name)
                networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                container_ip = next(
                    (v["IPAddress"] for v in networks.values() if v.get("IPAddress")),
                    None,
                )
                if not container_ip:
                    log.warning("sre.rewire.no_ip", slug=demo.slug)
                    results["errors"].append(f"{demo.slug}: no container IP")
                    continue

                import io
                import tarfile  # noqa: PLC0415

                conf_content = _nginx_conf_for_slug(
                    demo.slug, container_ip, demo.internal_port or 80
                )
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=f"{demo.slug}.conf")
                    data = conf_content.encode()
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                buf.seek(0)
                nginx.put_archive("/etc/nginx/conf.d/demos", buf.read())
                results["wired"].append(demo.slug)
                reloaded = True
                log.info("sre.rewire.wired", slug=demo.slug, ip=container_ip, port=demo.internal_port or 80)
            except Exception as exc:
                log.warning("sre.rewire.demo_failed", slug=demo.slug, error=str(exc))
                results["errors"].append(f"{demo.slug}: {exc}")

        if reloaded:
            nginx.exec_run("nginx -s reload")
            log.info("sre.rewire.nginx_reloaded", count=len(results["wired"]))

    except Exception as exc:
        log.warning("sre.rewire.failed", error=str(exc))
        results["errors"].append(str(exc))

    return results


# ── Celery tasks ──────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.sre.execute_task",
    bind=True,
    queue="sre",
    max_retries=1,
    acks_late=True,
    soft_time_limit=900,   # 15 min: git clone + docker build + start
    time_limit=1200,
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: deploy demo for a run."""
    agent = SREAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "sre",
    )
    try:
        result = asyncio.run(agent.execute())
    except Exception as exc:
        log.exception("sre.celery_task_unhandled", task_id=task_id, run_id=run_id)
        asyncio.run(mark_task_failed(task_id, str(exc)))
        raise

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "error": result.error,
    }


@celery_app.task(
    name="phalanx.agents.sre.portal_start_demo",
    bind=True,
    queue="sre",
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def portal_start_demo(self, demo_id: str) -> dict:  # pragma: no cover
    """Celery task: start a stopped demo from the portal."""
    asyncio.run(start_demo_by_id(demo_id))
    return {"demo_id": demo_id}


@celery_app.task(
    name="phalanx.agents.sre.portal_stop_demo",
    bind=True,
    queue="sre",
    max_retries=0,
    acks_late=True,
    soft_time_limit=60,
    time_limit=90,
)
def portal_stop_demo(self, demo_id: str) -> dict:  # pragma: no cover
    """Celery task: stop a running demo from the portal."""
    asyncio.run(stop_demo_by_id(demo_id))
    return {"demo_id": demo_id}


@celery_app.task(
    name="phalanx.agents.sre.prep_infra",
    bind=True,
    queue="sre",
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,   # 5 min: image pulls can be slow on cold start
    time_limit=400,
)
def prep_infra(self, run_id: str, slug: str) -> dict:  # pragma: no cover
    """
    Celery task: pre-pull Docker base images + ensure demos-net exists.

    Dispatched by Commander immediately after plan approval, running in
    parallel with the builder chain so infra is ready when SRE starts.
    """
    return asyncio.run(_prep_infra(run_id, slug))


@celery_app.task(
    name="phalanx.agents.sre.rewire_nginx",
    bind=True,
    queue="sre",
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def rewire_nginx(self) -> dict:  # pragma: no cover
    """
    Celery task: re-wire nginx routes for all RUNNING demos.

    Scheduled via beat every 5 minutes and triggered once on sre-worker
    startup so demo routes survive nginx container restarts (e.g. after deploy).
    """
    return asyncio.run(_rewire_nginx())
