"""CI Fixer v3 — SRE agent. Provisions on-the-fly sandboxes + mimics CI.

Two modes (selected by ci_context['sre_mode']):

  1. 'setup'  — FIRST task in every v3 run.
     Clones the repo, runs env_detector → EnvSpec, runs
     provisioner.provision_on_the_fly → container_id. Writes the
     container_id + workspace_path + env_spec + setup_log to Task.output
     so downstream Tech Lead and Engineer don't have to re-clone or
     re-provision. Kills the "pre-warmed sandbox is a year stale"
     bug class by construction.

  2. 'verify' — LAST task in every iteration.
     Runs the repo's full CI pipeline inside the sandbox: the original
     failing command + every other top-level `run:` command found in
     `.github/workflows/*.yml`. If any fail, reports new_failures so
     cifix_commander can iterate (rewind VERIFYING → EXECUTING and
     dispatch another techlead/engineer/sre_verify round).

Deterministic, NOT LLM-driven. SRE is plumbing, not reasoning.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import structlog
import yaml
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.ci_fixer_v3.env_detector import detect_env
from phalanx.ci_fixer_v3.provisioner import (
    ProvisionedSandbox,
    _exec_in_container,
    provision_on_the_fly,
)
from phalanx.config.settings import get_settings
from phalanx.db.models import CIIntegration, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


# Per-job verify timeout. Linters are fast; tests can run for minutes.
_VERIFY_JOB_TIMEOUT_S = 900

# Max number of workflow commands to run during verify mode. Caps blast
# radius if a workflow has dozens of steps — we'd rather skip noise than
# spend 10 minutes on docs builds.
_VERIFY_MAX_JOBS = 6


@celery_app.task(
    name="phalanx.agents.cifix_sre.execute_task",
    bind=True,
    queue="cifix_sre",
    max_retries=1,
    soft_time_limit=1200,
    time_limit=1320,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:  # pragma: no cover
    from phalanx.ci_fixer_v3.task_lifecycle import persist_task_completion  # noqa: PLC0415

    agent = CIFixSREAgent(run_id=run_id, agent_id="cifix_sre", task_id=task_id)
    result = asyncio.run(agent.execute())
    asyncio.run(persist_task_completion(task_id, result))
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixSREAgent(BaseAgent):
    AGENT_ROLE = "cifix_sre"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_sre.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            ci_context = _parse_ci_context(task.description)
            integration = await self._load_integration(session, ci_context.get("repo"))

        mode = ci_context.get("sre_mode") or "setup"
        if mode == "setup":
            return await self._execute_setup(ci_context, integration)
        elif mode == "verify":
            return await self._execute_verify(ci_context)
        else:
            return AgentResult(
                success=False,
                output={},
                error=f"unknown sre_mode={mode!r}; expected 'setup' or 'verify'",
            )

    # ─────────────────────────────────────────────────────────────────────
    # Setup mode — clone + detect + provision
    # ─────────────────────────────────────────────────────────────────────

    async def _execute_setup(
        self, ci_context: dict, integration: CIIntegration | None
    ) -> AgentResult:
        # Fast-fail on missing must-haves BEFORE any Docker work.
        for field in ("repo", "branch"):
            if not ci_context.get(field):
                return AgentResult(
                    success=False, output={}, error=f"sre_setup: ci_context missing {field!r}"
                )

        # Step A: clone
        try:
            workspace_path = await _clone_workspace(
                run_id=self.run_id,
                repo_full_name=ci_context["repo"],
                branch=ci_context["branch"],
                github_token=_resolve_github_token(integration),
            )
        except Exception as exc:
            self._log.exception("cifix_sre.setup.clone_failed", error=str(exc))
            return AgentResult(success=False, output={}, error=f"clone_failed: {exc}")

        # Step B: detect env
        env_spec = detect_env(workspace_path)
        self._log.info(
            "cifix_sre.setup.env_detected",
            stack=env_spec.stack,
            base_image=env_spec.base_image,
            install_cmds=len(env_spec.install_commands),
            system_deps=env_spec.system_deps,
        )

        # Step C: provision
        provisioned: ProvisionedSandbox = await provision_on_the_fly(Path(workspace_path), env_spec)

        if not provisioned.available:
            return AgentResult(
                success=False,
                output={
                    "mode": "setup",
                    "env_spec": env_spec.to_json(),
                    "setup_log": provisioned.setup_log,
                    "error": provisioned.error,
                },
                error=f"sandbox_provisioning_failed: {provisioned.error}",
            )

        self._log.info(
            "cifix_sre.setup.ready",
            container_id=provisioned.container_id,
            workspace=workspace_path,
            setup_steps=len(provisioned.setup_log),
        )
        return AgentResult(
            success=True,
            output={
                "mode": "setup",
                "container_id": provisioned.container_id,
                "workspace_path": provisioned.workspace_path,
                "env_spec": provisioned.env_spec,
                "setup_log": provisioned.setup_log,
            },
            tokens_used=0,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Verify mode — re-run the repo's CI against the sandbox
    # ─────────────────────────────────────────────────────────────────────

    async def _execute_verify(self, ci_context: dict) -> AgentResult:
        async with get_db() as session:
            setup = await self._load_upstream_sre_setup(session)
        if not setup or not setup.get("container_id"):
            return AgentResult(
                success=False,
                output={"mode": "verify"},
                error="no upstream cifix_sre setup task found with container_id",
            )

        container_id = setup["container_id"]
        workspace_path = setup["workspace_path"]

        # Build the job list: original failing command + workflow YAML commands.
        commands = _collect_verify_commands(
            workspace_path=Path(workspace_path),
            original_failing_command=ci_context.get("failing_command") or "",
        )
        if not commands:
            # No commands — nothing to verify. This is suspicious (we expect at
            # least the failing command) but not a hard failure.
            self._log.warning("cifix_sre.verify.no_commands_found", workspace=workspace_path)
            return AgentResult(
                success=True,
                output={
                    "mode": "verify",
                    "verdict": "all_green",
                    "jobs": [],
                    "new_failures": [],
                    "note": "no verification commands found — trusting engineer's sandbox gate",
                },
            )

        jobs: list[dict] = []
        for label, cmd in commands:
            exec_result = await _exec_in_container(
                container_id=container_id,
                cmd=cmd,
                as_root=True,
                workdir="/workspace",
                timeout_s=_VERIFY_JOB_TIMEOUT_S,
            )
            # Preserve the REAL exit code so downstream signal isn't lost:
            #   0    = success
            #   1    = generic failure (assertion, lint violation, etc.)
            #   2    = command parse error (invalid argument)
            #   5    = pytest: no tests collected
            #   137  = OOM killed
            #   139  = segfault
            #   -1   = we couldn't spawn/timeout (infrastructure)
            jobs.append(
                {
                    "name": label,
                    "cmd": cmd,
                    "exit_code": exec_result.exit_code,
                    "stderr_tail": (exec_result.stderr_tail or "")[-500:],
                }
            )
            self._log.info(
                "cifix_sre.verify.job_done",
                name=label,
                cmd=cmd[:120],
                exit_code=exec_result.exit_code,
            )

        new_failures = [j for j in jobs if j["exit_code"] != 0]
        verdict = "all_green" if not new_failures else "new_failures"

        self._log.info(
            "cifix_sre.verify.done",
            verdict=verdict,
            total_jobs=len(jobs),
            failed_jobs=len(new_failures),
        )
        return AgentResult(
            success=True,
            output={
                "mode": "verify",
                "verdict": verdict,
                "jobs": jobs,
                "new_failures": new_failures,
                "container_id": container_id,
                "workspace_path": workspace_path,
            },
        )

    # ─────────────────────────────────────────────────────────────────────
    # DB helpers
    # ─────────────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_integration(self, session, repo: str | None) -> CIIntegration | None:
        if not repo:
            return None
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == repo)
        )
        return result.scalar_one_or_none()

    async def _load_upstream_sre_setup(self, session) -> dict | None:
        """Find the most recent COMPLETED cifix_sre task whose output.mode == 'setup'."""
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "cifix_sre",
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.asc())
        )
        for (output,) in result.all():
            if isinstance(output, dict) and output.get("mode") == "setup":
                return output
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _clone_workspace(
    run_id: str, repo_full_name: str, branch: str, github_token: str | None
) -> str:
    """Shallow clone at the PR head branch. SRE owns the workspace for the
    whole run — TL and Engineer read workspace_path from SRE's output."""
    if not github_token:
        raise RuntimeError("no github token available for clone")
    import git  # noqa: PLC0415

    base = Path(get_settings().git_workspace) / f"v3-{run_id}-sre"
    if base.exists():
        import shutil  # noqa: PLC0415

        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    git.Repo.clone_from(url, base, branch=branch, depth=1)
    return str(base)


def _resolve_github_token(integration: CIIntegration | None) -> str | None:
    if integration and integration.github_token:
        return integration.github_token
    return get_settings().github_token or None


def _parse_ci_context(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Workflow command extraction (verify mode)
# ─────────────────────────────────────────────────────────────────────────────


# Heuristic: only consider workflow commands that look like test/lint/typecheck
# invocations. A full implementation would run every step, but most workflows
# have setup / cache / login / post-job steps that don't represent the real CI
# verification surface. Filtering keeps the blast radius bounded.
_INTERESTING_COMMAND_PREFIXES: tuple[str, ...] = (
    "ruff ",
    "mypy",
    "pytest",
    "python -m pytest",
    "python -m unittest",
    "npm test",
    "npm run test",
    "npm run lint",
    "yarn test",
    "yarn lint",
    "pnpm test",
    "pnpm run lint",
    "eslint",
    "tsc",
    "mvn ",
    "gradle ",
    "./gradlew ",
    "go test",
    "go vet",
    "go build",
    "cargo test",
    "cargo clippy",
    "cargo build",
    "dotnet test",
    "dotnet build",
    "uvx ",
    "uv run",
    "tox",
    "prek",
)


def _collect_verify_commands(
    workspace_path: Path, original_failing_command: str
) -> list[tuple[str, str]]:
    """Return [(label, command), ...] to run in verify mode.

    Always includes the original_failing_command first (if non-empty). Then
    walks .github/workflows/*.yml looking for interesting `run:` steps.
    De-duplicates exact matches.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    if original_failing_command.strip():
        out.append(("original_failing_command", original_failing_command.strip()))
        seen.add(original_failing_command.strip())

    wf_dir = workspace_path / ".github" / "workflows"
    if not wf_dir.is_dir():
        return out

    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        try:
            text = wf.read_text(encoding="utf-8", errors="replace")
            doc = yaml.safe_load(text)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run_cmd = step.get("run")
                if not isinstance(run_cmd, str):
                    continue
                # Join shell line-continuations FIRST so a multi-line
                # `pytest \\\n  --cov=...` block becomes one logical command,
                # not "pytest \\" run literally (bug #9, 2026-04-25 lint canary).
                joined = re.sub(r"\\\n[ \t]*", " ", run_cmd)
                # Take the FIRST non-empty line of the joined script (shell
                # scripts spanning multiple LOGICAL lines are common but only
                # the first is usually the test invocation).
                first_line = next(
                    (line.strip() for line in joined.splitlines() if line.strip()),
                    "",
                )
                if not first_line or first_line in seen:
                    continue
                if not any(first_line.startswith(p) for p in _INTERESTING_COMMAND_PREFIXES):
                    continue
                out.append((f"{wf.stem}.{job_name}", first_line))
                seen.add(first_line)
                if len(out) >= _VERIFY_MAX_JOBS:
                    return out

    return out


# Expose for unit tests
_collect_verify_commands_for_test = _collect_verify_commands
