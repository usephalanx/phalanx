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
from phalanx.ci_fixer_v3.env_detector import EnvSpec, detect_env
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

        # ── v1.7.1 — Tier 0/Cache lookup BEFORE detect_env ─────────────────
        # Cache hit → use stored commands. Tier 0 (workflow YAML extract) →
        # if successful, use those commands. Tier 1 (existing detect_env)
        # is the fallback. Tier 2 (agentic gap-fill) catches any remaining
        # gaps after the deterministic provision attempt.
        env_spec, tier_source = self._select_env_spec_v171(
            workspace_path=workspace_path,
            ci_context=ci_context,
        )
        self._log.info(
            "cifix_sre.setup.env_selected",
            tier=tier_source["tier"],
            source=tier_source.get("source", ""),
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
            "cifix_sre.setup.deterministic_done",
            container_id=provisioned.container_id,
            workspace=workspace_path,
            setup_steps=len(provisioned.setup_log),
        )

        # Agentic gap-fill (Phase 3). The deterministic provision is the
        # KERNEL — most repos resolve here. For repos using setup-uv,
        # setup-go, custom curl installers, etc., env_detector misses tools
        # that upstream CI installs. We probe the sandbox for the first-
        # tokens of the failing CI commands; gaps trigger the agentic loop
        # (or a cached install plan replay).
        sre_output = await _agentic_gap_fill(
            container_id=provisioned.container_id,
            workspace_path=provisioned.workspace_path,
            ci_context=ci_context,
            det_spec=provisioned.env_spec,
            det_setup_log=provisioned.setup_log,
            log=self._log,
        )

        # BLOCKED → fail the SRE task so commander short-circuits the DAG.
        if sre_output["final_status"] == "BLOCKED":
            return AgentResult(
                success=False,
                output=sre_output,
                error=f"sre_blocked: {sre_output.get('blocked_reason')}",
            )

        # READY or PARTIAL → continue (TL gets prior_sre_partial flag for the
        # PARTIAL case via Task.output → ci_context propagation, when commander
        # plumbs it. For now, PARTIAL still attempts; future TL-prompt update
        # acknowledges gaps explicitly).

        # v1.7.1 — annotate the output with tier provenance + persist to cache
        # if the run produced a working recipe. Cache is keyed by repo +
        # workflow_path + dep file hashes; next run with same deps can skip
        # tiers entirely.
        sre_output["v171_tier"] = tier_source
        if sre_output["final_status"] == "READY" and env_spec.install_commands:
            self._maybe_cache_recipe(
                workspace_path=workspace_path,
                ci_context=ci_context,
                tier=tier_source["tier"],
                commands=env_spec.install_commands,
                source=tier_source.get("source", ""),
            )

        return AgentResult(
            success=True,
            output=sre_output,
            tokens_used=sre_output.get("tokens_used", 0),
        )

    # ─────────────────────────────────────────────────────────────────────
    # v1.7.1 — tier selection helpers
    # ─────────────────────────────────────────────────────────────────────

    def _select_env_spec_v171(
        self, *, workspace_path: str, ci_context: dict
    ) -> tuple["EnvSpec", dict]:
        """Pick install commands using the v1.7.1 tier ladder:

          1. Cache lookup — if a validated recipe exists for the same
             (repo, workflow, dep-file-content) hash, use it.
          2. Tier 0 — parse .github/workflows/<job>.yml; render the steps.
          3. Tier 1 (existing v1.4.0 detect_env) — file-fingerprint fallback.

        Returns (env_spec, tier_source) where tier_source records which
        path produced the commands for telemetry / cache write-back.
        """
        from phalanx.agents._v171_setup_cache import lookup as cache_lookup
        from phalanx.agents._v171_workflow_extractor import extract_recipe
        from phalanx.config.settings import get_settings

        cache_dir = self._cache_dir_path()
        repo = ci_context.get("repo") or ""
        failing_job_name = ci_context.get("failing_job_name") or ""
        # Workflow path is informational here; the cache key uses it
        # directly so two different workflow files get distinct entries.
        workflow_path: str | None = None

        # 1. Tier 0 first (workflow YAML extraction). We do this BEFORE the
        # cache lookup because the workflow_path it discovers is part of
        # the cache key. Tier 0 is fast (just YAML parse) so the order is
        # cheap.
        tier0 = extract_recipe(
            workspace_path=workspace_path,
            failing_job_name=failing_job_name,
        )
        if tier0 is not None:
            workflow_path = tier0.workflow_file

        # 2. Cache lookup with whatever workflow_path we have
        cached = cache_lookup(
            cache_dir=cache_dir,
            repo_full_name=repo,
            workflow_path=workflow_path,
            workspace_path=workspace_path,
        )
        if cached is not None:
            return (
                self._env_spec_from_cached(workspace_path, cached),
                {"tier": "cache", "source": cached.source},
            )

        # 3. Use Tier 0 commands if we got them
        if tier0 is not None and tier0.commands:
            return (
                self._env_spec_from_commands(
                    workspace_path=workspace_path,
                    commands=tier0.commands,
                    source_label=f"workflow:{tier0.workflow_file}::{tier0.job_key}",
                ),
                {"tier": "0", "source": tier0.workflow_file},
            )

        # 4. Tier 1 — existing v1.4.0 detect_env path
        env_spec = detect_env(workspace_path)
        return (env_spec, {"tier": "1", "source": "detect_env"})

    def _env_spec_from_cached(self, workspace_path: str, cached) -> "EnvSpec":
        """Build an EnvSpec around cached install_commands."""
        return EnvSpec(
            stack="python",  # cached recipes assumed Python for v1.7.1
            base_image="python:3.12-slim",
            workspace_path=str(workspace_path),
            install_commands=list(cached.commands),
            detected_from=[f"v171_cache:{cached.source}"],
            notes=[f"v1.7.1 cache hit; tier={cached.tier}"],
        )

    def _env_spec_from_commands(
        self, *, workspace_path: str, commands: list[str], source_label: str
    ) -> "EnvSpec":
        """Build an EnvSpec around commands rendered from Tier 0."""
        return EnvSpec(
            stack="python",
            base_image="python:3.12-slim",
            workspace_path=str(workspace_path),
            install_commands=list(commands),
            detected_from=[source_label],
            notes=["v1.7.1 Tier 0 (workflow YAML extraction)"],
        )

    def _cache_dir_path(self) -> str:
        from phalanx.config.settings import get_settings
        settings = get_settings()
        return f"{settings.git_workspace}/_v171_setup_cache"

    def _maybe_cache_recipe(
        self,
        *,
        workspace_path: str,
        ci_context: dict,
        tier: str,
        commands: list[str],
        source: str,
    ) -> None:
        """Best-effort write-back to the per-repo JSONL cache."""
        from phalanx.agents._v171_setup_cache import store as cache_store
        try:
            cache_store(
                cache_dir=self._cache_dir_path(),
                repo_full_name=ci_context.get("repo") or "",
                workflow_path=ci_context.get("v171_workflow_path"),
                workspace_path=workspace_path,
                tier=tier if tier in {"0", "1", "2"} else "1",
                commands=commands,
                source=source,
                validated=True,
                validation_evidence={"final_status": "READY"},
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("cifix_sre.setup.cache_write_failed", error=str(exc))

    # ─────────────────────────────────────────────────────────────────────
    # Verify mode — re-run the repo's CI against the sandbox
    # ─────────────────────────────────────────────────────────────────────

    async def _execute_verify(self, ci_context: dict) -> AgentResult:
        async with get_db() as session:
            setup = await self._load_upstream_sre_setup(session)
            tl_fix_spec = await self._load_upstream_tl_fix_spec(session)
        if not setup or not setup.get("container_id"):
            return AgentResult(
                success=False,
                output={"mode": "verify"},
                error="no upstream cifix_sre setup task found with container_id",
            )

        container_id = setup["container_id"]
        workspace_path = setup["workspace_path"]

        # v1.7.2.2: prefer TL's narrow verify_command. The broad workflow
        # enumeration finds unrelated failures elsewhere in the repo, masking
        # a correct fix. Only fall back to enumeration when TL didn't emit a
        # usable verify_command.
        tl_verify_command = (tl_fix_spec or {}).get("verify_command") or ""
        tl_verify_success = (tl_fix_spec or {}).get("verify_success") or {}
        if isinstance(tl_verify_command, str) and tl_verify_command.strip():
            return await self._execute_verify_narrow(
                container_id=container_id,
                workspace_path=workspace_path,
                verify_command=tl_verify_command.strip(),
                verify_success=tl_verify_success if isinstance(tl_verify_success, dict) else {},
            )

        # Fallback: legacy broad enumeration (used when no TL fix_spec or
        # TL omitted verify_command).
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

    async def _execute_verify_narrow(
        self,
        *,
        container_id: str,
        workspace_path: str,
        verify_command: str,
        verify_success: dict,
    ) -> AgentResult:
        """v1.7.2.2: run TL's narrow verify_command and apply its
        verify_success matcher. Single command, single verdict — no broad
        workflow enumeration that risks finding unrelated lint elsewhere.
        """
        exec_result = await _exec_in_container(
            container_id=container_id,
            cmd=verify_command,
            as_root=True,
            workdir="/workspace",
            timeout_s=_VERIFY_JOB_TIMEOUT_S,
        )

        exit_codes = verify_success.get("exit_codes") or [0]
        if not isinstance(exit_codes, list) or not all(
            isinstance(c, int) for c in exit_codes
        ):
            exit_codes = [0]
        stderr_excludes = verify_success.get("stderr_excludes") or []
        if not isinstance(stderr_excludes, list):
            stderr_excludes = []

        stderr_tail = (exec_result.stderr_tail or "")[-500:]
        exit_ok = exec_result.exit_code in exit_codes
        excluded_hit: str | None = None
        for needle in stderr_excludes:
            if isinstance(needle, str) and needle and needle in stderr_tail:
                excluded_hit = needle
                break

        passed = exit_ok and excluded_hit is None
        job = {
            "name": "tl_verify_command",
            "cmd": verify_command,
            "exit_code": exec_result.exit_code,
            "stderr_tail": stderr_tail,
        }
        new_failures = [] if passed else [job]
        verdict = "all_green" if passed else "new_failures"

        self._log.info(
            "cifix_sre.verify.done",
            mode="narrow",
            verdict=verdict,
            cmd=verify_command[:120],
            exit_code=exec_result.exit_code,
            exit_ok=exit_ok,
            excluded_hit=excluded_hit,
        )
        return AgentResult(
            success=True,
            output={
                "mode": "verify",
                "verdict": verdict,
                "jobs": [job],
                "new_failures": new_failures,
                "container_id": container_id,
                "workspace_path": workspace_path,
                "verify_scope": "narrow_from_tl",
                "verify_command": verify_command,
                "verify_success": {
                    "exit_codes": exit_codes,
                    "stderr_excludes": stderr_excludes,
                },
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
                Task.agent_role.in_(
                    ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                ),
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.asc())
        )
        for (output,) in result.all():
            if isinstance(output, dict) and output.get("mode") == "setup":
                return output
        return None

    async def _load_upstream_tl_fix_spec(self, session) -> dict | None:
        """Latest COMPLETED cifix_techlead Task.output for this run.

        Used by verify mode to read TL's narrow verify_command +
        verify_success matcher. Returns None if no TL task has completed
        yet (e.g., first SRE setup before TL has run).
        """
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "cifix_techlead",
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        row = result.one_or_none()
        if row is None or row[0] is None:
            return None
        output = row[0]
        return output if isinstance(output, dict) else None


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
                # Bug #14 (2026-04-30 humanize canary): GHA expressions like
                # `${{ matrix.python-version }}` ONLY expand inside GitHub
                # Actions. Running such commands in the sandbox produces
                # `sh: 1: Bad substitution`. Skip them — they're not
                # meaningfully runnable outside GHA, and verify-mode treats
                # their failure as a real CI fail (which it isn't).
                if "${{" in first_line:
                    log.info(
                        "v3.sre.skipping_gha_only_command",
                        cmd=first_line[:200],
                        workflow=wf.stem,
                        job=job_name,
                    )
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


# ─────────────────────────────────────────────────────────────────────────────
# Agentic gap-fill (Phase 3 hybrid integration)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_first_token(cmd: str) -> str:
    """Best-effort first token of a shell command.

    `'pytest --cov=src/calc'` → `'pytest'`. `'sudo apt install gettext'`
    → `'sudo'` (we don't try to skip privilege wrappers — the LLM can
    handle those if the sandbox lacks them, which it does for sudo).
    """
    parts = cmd.strip().split(maxsplit=1)
    return parts[0] if parts else ""


async def _check_first_tokens_available(
    container_id: str, tokens: list[str]
) -> dict[str, bool]:
    """Probe the sandbox for each first-token; True = command exists on PATH."""
    out: dict[str, bool] = {}
    for tok in tokens:
        if not tok or tok in out:
            continue
        # Single shell-safe probe; reuses provisioner._exec_in_container
        # which we already trust.
        result = await _exec_in_container(
            container_id, f"command -v {tok!r} >/dev/null 2>&1", as_root=False
        )
        out[tok] = result.exit_code == 0
    return out


def _det_spec_summary(env_spec: dict | None) -> dict:
    """Compact summary for the LLM prompt — what the deterministic step
    already installed."""
    if not env_spec:
        return {}
    return {
        "stack": env_spec.get("stack"),
        "base_image": env_spec.get("base_image"),
        "system_deps": env_spec.get("system_deps") or [],
        "install_commands": env_spec.get("install_commands") or [],
        "tool_versions": env_spec.get("tool_versions") or {},
    }


async def _agentic_gap_fill(
    *,
    container_id: str,
    workspace_path: str,
    ci_context: dict,
    det_spec: dict,
    det_setup_log: list[dict],
    log,
) -> dict:
    """Hybrid decision tree: deterministic-first, agentic-on-gaps.

    Returns the new Task.output schema (backwards-compatible — keeps the
    old env_spec + setup_log fields, adds capabilities_installed,
    final_status, etc.).
    """
    from phalanx.ci_fixer_v3.sre_setup.cache import (  # noqa: PLC0415
        cache_lookup,
        cache_write,
        compute_cache_key,
    )
    from phalanx.ci_fixer_v3.sre_setup.loop import run_sre_setup_subagent  # noqa: PLC0415
    from phalanx.ci_fixer_v3.sre_setup.schemas import SREToolContext  # noqa: PLC0415

    # 1. Collect the failing-command first-tokens we need available.
    failing_cmd = ci_context.get("failing_command") or ""
    observed: list[str] = []
    if failing_cmd:
        observed.append(failing_cmd)

    # Augment with workflow-derived interesting commands so we cover
    # ancillary tools the upstream CI invokes.
    extra = _collect_verify_commands(
        Path(workspace_path), original_failing_command=""
    )
    for _label, cmd in extra:
        if cmd not in observed:
            observed.append(cmd)

    first_tokens = [_extract_first_token(c) for c in observed if _extract_first_token(c)]
    token_status = await _check_first_tokens_available(container_id, first_tokens)
    gaps = [t for t in first_tokens if not token_status.get(t, False)]

    log.info(
        "cifix_sre.setup.gap_check",
        first_tokens=first_tokens,
        token_status=token_status,
        gaps=gaps,
    )

    # 2. No gaps → READY fast path (no LLM call).
    if not gaps:
        return {
            "mode": "setup",
            "container_id": container_id,
            "workspace_path": workspace_path,
            "env_spec": det_spec,  # backwards-compat
            "setup_log": det_setup_log,
            "capabilities_installed": [],  # deterministic only
            "final_status": "READY",
            "blocked_reason": None,
            "observed_token_status": [
                {"cmd": c, "first_token": _extract_first_token(c),
                 "found": token_status.get(_extract_first_token(c), False)}
                for c in observed
            ],
            "tokens_used": 0,
            "fallback_used": False,
            "cache_hit": False,
            "agentic_iterations": 0,
        }

    # 3. Cache lookup (memoized plan replay — Phase 2).
    repo = ci_context.get("repo") or ""
    cache_key = compute_cache_key(workspace_path)
    cached_plan = await cache_lookup(cache_key, repo_full_name=repo)
    if cached_plan is not None:
        log.info(
            "cifix_sre.setup.cache_hit",
            cache_key=cache_key[:16],
            capabilities=len(cached_plan.get("capabilities", [])),
        )
        # Bug #15 (2026-04-30 self-found): cache hit must REPLAY the install
        # steps in the fresh sandbox before claiming READY. Earlier impl
        # just returned the cached plan metadata, but the sandbox itself
        # is fresh — none of the cached tools are actually present.
        replay_log: list[dict] = []
        replay_failed = False
        for cap in cached_plan.get("capabilities", []):
            method = cap.get("install_method")
            tool = cap.get("tool")
            if method == "preinstalled" or not tool:
                continue
            if method == "pip":
                cmd = f"pip install --quiet --no-cache-dir {tool}"
            elif method == "apt":
                cmd = (
                    f"apt-get update -qq && apt-get install -y --no-install-recommends {tool}"
                )
            else:
                replay_log.append({"step": "skip_unknown_method", "tool": tool, "method": method})
                replay_failed = True
                continue
            r = await _exec_in_container(container_id, cmd, as_root=(method == "apt"))
            replay_log.append({
                "step": "cache_replay_install",
                "tool": tool,
                "method": method,
                "exit_code": r.exit_code,
            })
            if r.exit_code != 0:
                replay_failed = True
                log.warning(
                    "cifix_sre.setup.cache_replay_install_failed",
                    tool=tool,
                    method=method,
                    exit_code=r.exit_code,
                )

        # Re-verify first-tokens FRESH after replay (don't trust cached status).
        fresh_status = await _check_first_tokens_available(container_id, first_tokens)
        all_present = all(fresh_status.get(t, False) for t in first_tokens)

        if not replay_failed and all_present:
            return {
                "mode": "setup",
                "container_id": container_id,
                "workspace_path": workspace_path,
                "env_spec": det_spec,
                "setup_log": det_setup_log
                + [{"step": "cache_hit", "cache_key": cache_key[:16]}]
                + replay_log,
                "capabilities_installed": cached_plan.get("capabilities", []),
                "final_status": "READY",
                "blocked_reason": None,
                "observed_token_status": [
                    {"cmd": c, "first_token": _extract_first_token(c),
                     "found": fresh_status.get(_extract_first_token(c), False)}
                    for c in observed
                ],
                "tokens_used": 0,
                "fallback_used": False,
                "cache_hit": True,
                "agentic_iterations": 0,
            }
        # Cache replay didn't actually fix the gaps — fall through to agentic
        # loop. Add a marker so observability shows we tried the cache.
        log.info(
            "cifix_sre.setup.cache_replay_insufficient",
            cache_key=cache_key[:16],
            replay_failed=replay_failed,
            all_present=all_present,
            fresh_status=fresh_status,
        )
        det_setup_log = det_setup_log + [
            {"step": "cache_hit_replay_insufficient", "cache_key": cache_key[:16]}
        ] + replay_log

    # 4. Agentic loop (Phase 1).
    # Build the LLM call from v2's existing Sonnet provider — same wiring
    # the engineer uses.
    from phalanx.ci_fixer_v2.prompts import CODER_SUBAGENT_SYSTEM_PROMPT  # noqa: PLC0415, F401
    from phalanx.ci_fixer_v2.providers import build_sonnet_coder_callable  # noqa: PLC0415
    from phalanx.ci_fixer_v3.sre_setup.tools import SRE_SETUP_TOOLS  # noqa: PLC0415

    settings = get_settings()
    # We pass our own SRE tool schemas — Sonnet's tool list matches what's
    # actually allowed in the loop. The system prompt is the SRE one, not
    # the coder one (built into the loop's seed prompt).
    sonnet_llm = build_sonnet_coder_callable(
        model=settings.anthropic_model_ci_fixer_coder,
        api_key=settings.anthropic_api_key,
        system_prompt="You are the CI Fixer v3 SRE Agent. Follow the user message's instructions exactly.",
        tool_schemas=[s for s, _ in SRE_SETUP_TOOLS],
    )

    async def exec_in_sandbox(_container_id, cmd, **kwargs):
        return await _exec_in_container(container_id, cmd, **kwargs)

    sre_ctx = SREToolContext(
        container_id=container_id,
        workspace_path=workspace_path,
        exec_in_sandbox=exec_in_sandbox,
    )
    sre_result = await run_sre_setup_subagent(
        sre_ctx,
        gaps=gaps,
        det_spec_summary=_det_spec_summary(det_spec),
        observed_failing_commands=observed,
        llm_call=sonnet_llm,
    )

    log.info(
        "cifix_sre.setup.agentic_done",
        final_status=sre_result.final_status,
        iterations=sre_result.iterations_used,
        tokens=sre_result.tokens_used,
        fallback_used=sre_result.fallback_used,
    )

    # 5. Write cache on READY.
    if sre_result.final_status == "READY":
        try:
            await cache_write(
                cache_key,
                repo_full_name=repo,
                install_plan={
                    "capabilities": sre_result.capabilities,
                    "observed_token_status": sre_result.observed_token_status,
                },
                final_status="READY",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cifix_sre.setup.cache_write_failed", error=str(exc)[:200])

    return {
        "mode": "setup",
        "container_id": container_id,
        "workspace_path": workspace_path,
        "env_spec": det_spec,
        "setup_log": det_setup_log + sre_result.setup_log,
        "capabilities_installed": sre_result.capabilities,
        "final_status": sre_result.final_status,
        "blocked_reason": sre_result.blocked_reason,
        "blocked_evidence": sre_result.blocked_evidence,
        "observed_token_status": sre_result.observed_token_status,
        "gaps_remaining": sre_result.gaps_remaining,
        "tokens_used": sre_result.tokens_used,
        "fallback_used": sre_result.fallback_used,
        "cache_hit": False,
        "agentic_iterations": sre_result.iterations_used,
        "notes": sre_result.notes,
    }
