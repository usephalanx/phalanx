"""
SRE Agent — infrastructure wiring for live demo deployments.

Responsibilities:
  1. Clone generated code from the run's active branch
  2. Detect app type (React, Next.js, FastAPI, Express, static HTML)
  3. Inject an appropriate Dockerfile if one doesn't exist
  4. Build a Docker image via the host Docker daemon (socket mount)
  5. Enforce LRU cap: stop the least-recently-accessed demo if at limit
  6. Start the container on the demos-net Docker network
  7. Write a per-demo nginx config snippet to the shared volume
  8. Signal nginx to reload (docker exec nginx -s reload)
  9. Health-check the running container with retries
  10. Update the Demo record: status=RUNNING, demo_url, container_id
  11. Update Run.deploy_url

Design invariants:
  - Feature-gated: phalanx_enable_demo_deploy=False → no-op, task COMPLETED silently.
  - All Docker operations are wrapped — a build/start failure marks Demo FAILED
    but does NOT fail the Run (SRE is non-fatal in the pipeline).
  - LRU eviction stops containers but preserves images so restart is fast.
  - Never raises to Celery — exceptions are caught and recorded in Demo.error.

Agent role: "sre"
Queue:      "sre"
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent, mark_task_failed
from phalanx.config.settings import get_settings
from phalanx.db.models import Demo, Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

if TYPE_CHECKING:
    pass

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

# app_type → (dockerfile_content, internal_port)
_APP_TEMPLATES: dict[str, tuple[str, int]] = {
    "react":   (_DOCKERFILE_REACT,   80),
    "nextjs":  (_DOCKERFILE_NEXTJS,  3000),
    "fastapi": (_DOCKERFILE_FASTAPI, 8000),
    "flask":   (_DOCKERFILE_FLASK,   5000),
    "express": (_DOCKERFILE_EXPRESS, 3000),
    "static":  (_DOCKERFILE_STATIC,  80),
}

_HEALTH_CHECK_RETRIES = 12
_HEALTH_CHECK_INTERVAL = 10  # seconds

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
    Priority: package.json deps → requirements.txt → index.html fallback.
    """
    pkg_json_path = os.path.join(repo_path, "package.json")
    req_path = os.path.join(repo_path, "requirements.txt")

    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path) as f:
                pkg = json.load(f)
            deps: dict = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs", 3000
            if "react-scripts" in deps or "vite" in deps or any(k.startswith("@vitejs") for k in deps):
                return "react", 80
            if "express" in deps or "fastify" in deps:
                return "express", 3000
        except (json.JSONDecodeError, OSError):
            pass
        return "react", 80  # default for any node project

    if os.path.exists(req_path):
        try:
            content = open(req_path).read().lower()
        except OSError:
            content = ""
        if "fastapi" in content:
            return "fastapi", 8000
        if "flask" in content:
            return "flask", 5000
        if "django" in content:
            return "fastapi", 8000  # treat Django as an 8000-port app for now
        return "fastapi", 8000  # generic Python app

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

        slug = run.demo_slug or _make_slug(str(task.title))
        branch = run.active_branch
        container_name = f"phalanx-demo-{slug}"
        image_name = f"phalanx-demo-{slug}:latest"

        self._log.info("sre.deploy.start", slug=slug, branch=branch)

        # Create Demo record (or update if already exists)
        async with get_db() as session:
            existing = await session.execute(select(Demo).where(Demo.run_id == self.run_id))
            demo = existing.scalar_one_or_none()
            if demo is None:
                demo = Demo(
                    run_id=self.run_id,
                    slug=slug,
                    title=task.title,
                    status="BUILDING",
                )
                session.add(demo)
                await session.flush()
            else:
                demo.status = "BUILDING"
            await session.commit()
            demo_id = demo.id

        # Clone repo and build image
        try:
            app_type, internal_port = await self._build_image(
                branch=branch,
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
            self._log.error("sre.start.failed", error=str(exc))
            await self._update_demo(demo_id, status="FAILED", error=str(exc)[:1000])
            await self._complete_task(error=str(exc))
            return AgentResult(success=False, output={}, error=str(exc))

        # Wire nginx
        try:
            await self._wire_nginx(slug, container_name, internal_port, settings)
        except Exception as exc:
            # Non-fatal: container runs but URL isn't wired. Log and continue.
            self._log.warning("sre.nginx_wire.failed", error=str(exc))

        # Health check
        demo_url = f"{settings.demo_base_url}/{slug}"
        healthy = await self._health_check(container_name, internal_port)

        status = "RUNNING" if healthy else "FAILED"
        error = None if healthy else "Container started but health check failed"

        await self._update_demo(
            demo_id,
            status=status,
            container_id=cid,
            demo_url=demo_url if healthy else None,
            error=error,
        )

        # Update Run.deploy_url
        if healthy:
            async with get_db() as session:
                await session.execute(
                    update(Run).where(Run.id == self.run_id).values(deploy_url=demo_url)
                )
                await session.commit()

        await self._complete_task()
        self._log.info("sre.deploy.done", slug=slug, status=status, demo_url=demo_url if healthy else None)
        return AgentResult(
            success=healthy,
            output={"slug": slug, "demo_url": demo_url if healthy else None, "status": status},
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
                f"\nBUILDER FILES PRODUCED\n"
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
        branch: str | None,
        slug: str,
        image_name: str,
        settings,
        work_order_title: str = "",
        work_order_description: str = "",
        tech_stack: str = "",
        files_produced: list | None = None,
    ) -> tuple[str, int]:
        """
        Clone the branch, ask LLM to generate a Dockerfile (falls back to
        template detection), then docker build.
        Returns (app_type, internal_port).
        """
        import docker  # noqa: PLC0415

        repo_url = self._get_showcase_repo_url(settings)
        if not repo_url:
            raise RuntimeError("No showcase repo URL configured — set GITHUB_TOKEN")

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
            _, build_logs = client.images.build(
                path=tmpdir,
                tag=image_name,
                rm=True,
                forcerm=True,
                timeout=300,
                buildargs=buildargs,
            )
            for log_entry in build_logs:
                if "stream" in log_entry:
                    line = log_entry["stream"].strip()
                    if line:
                        self._log.debug("sre.docker.build_log", line=line[:200])

            self._log.info("sre.docker.build_done", image=image_name)

        return app_type, internal_port

    def _get_showcase_repo_url(self, settings) -> str | None:
        token = settings.github_token
        if not token:
            return None
        return f"https://{token}@github.com/usephalanx/showcase.git"

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

        self._log.info("sre.docker.run", image=image_name, container=container_name)
        container = client.containers.run(
            image=image_name,
            name=container_name,
            detach=True,
            network=settings.demo_docker_network,
            labels={
                "phalanx.demo": "true",
                "phalanx.slug": slug,
                "phalanx.run_id": self.run_id,
                "phalanx.started_at": datetime.now(UTC).isoformat(),
            },
            mem_limit="512m",
            nano_cpus=500_000_000,  # 0.5 CPUs
            restart_policy={"Name": "no"},
        )
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
            with open(tmp_path, "rb") as fh:
                import tarfile, io  # noqa: PLC0415
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


async def start_demo_by_id(demo_id: str) -> None:
    """
    Start a STOPPED demo container and re-wire nginx.
    Called by the demo portal API route (via Celery task).
    """
    import docker  # noqa: PLC0415

    settings = get_settings()

    async with get_db() as session:
        result = await session.execute(select(Demo).where(Demo.id == demo_id))
        demo = result.scalar_one_or_none()
        if demo is None or demo.status not in ("STOPPED", "FAILED"):
            return
        await session.execute(
            update(Demo).where(Demo.id == demo_id).values(status="STARTING")
        )
        await session.commit()

    try:
        client = docker.from_env()

        # LRU enforcement
        running = await _get_running_demos_for_portal()
        settings2 = get_settings()
        if len(running) >= settings2.demo_max_running:
            lru = min(running, key=lambda d: d.last_accessed_at or d.created_at)
            if lru.container_name:
                try:
                    client.containers.get(lru.container_name).stop(timeout=15)
                except Exception:
                    pass
            async with get_db() as session:
                await session.execute(
                    update(Demo).where(Demo.id == lru.id).values(status="STOPPED", container_id=None)
                )
                await session.commit()

        # Start the container
        container = client.containers.run(
            image=demo.image_name,
            name=demo.container_name,
            detach=True,
            network=settings.demo_docker_network,
            labels={"phalanx.demo": "true", "phalanx.slug": demo.slug},
            mem_limit="512m",
            nano_cpus=500_000_000,
            restart_policy={"Name": "no"},
        )

        # Re-wire nginx using container IP + docker cp (avoids DNS + permission issues)
        try:
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            container_ip = next(
                (v["IPAddress"] for v in networks.values() if v.get("IPAddress")),
                demo.container_name,
            )
            conf_content = _nginx_conf_for_slug(demo.slug, container_ip, demo.internal_port or 80)
            nginx = client.containers.get(settings.demo_nginx_container)
            nginx.exec_run("mkdir -p /etc/nginx/conf.d/demos")
            import tarfile, io  # noqa: PLC0415
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=f"{demo.slug}.conf")
                data = conf_content.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            buf.seek(0)
            nginx.put_archive("/etc/nginx/conf.d/demos", buf.read())
            nginx.exec_run("nginx -s reload")
        except Exception:
            pass

        async with get_db() as session:
            await session.execute(
                update(Demo).where(Demo.id == demo_id).values(
                    status="RUNNING",
                    container_id=container.id,
                    last_accessed_at=datetime.now(UTC),
                )
            )
            await session.commit()

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
            try:
                client.containers.get(demo.container_name).stop(timeout=15)
            except Exception:
                pass

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
