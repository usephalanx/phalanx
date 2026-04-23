"""On-the-fly sandbox provisioner for CI Fixer v3.

Given an EnvSpec (produced by env_detector), boot a fresh Docker container
from a MINIMAL base image (e.g. `python:3.12-slim`, not the pre-warmed
`phalanx-sandbox-python:latest`), copy the workspace in, install system
deps + language deps exactly as the repo asks. Hand back a container_id
the agents can exec into.

Trades 10-60s of provisioning latency per run for version parity with the
repo's real CI. Kills the whole class of "sandbox has stale tool X" bugs
that blocked the humanize canary.

Scope:
  - Runs side-by-side with the pre-warmed pool (ci_fixer/sandbox_pool.py).
    v2 keeps using the pool; v3 calls provision_on_the_fly().
  - Uses the same Docker CLI (settings.sandbox_docker_cmd) the pool uses.
  - Does NOT manage a lifecycle — callers are responsible for stopping
    containers when the run finishes. Stop helper is here too.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from phalanx.ci_fixer_v3.env_detector import EnvSpec
from phalanx.config.settings import get_settings

log = structlog.get_logger(__name__)
_settings = get_settings()


# Per-command subprocess timeout. Pip installs on a cold image can be slow —
# 600s is generous but not unbounded.
_CMD_TIMEOUT_S = 600

# Always install these regardless of what env_detector found. git is needed
# by agents for diff / blame; ca-certificates + curl are handy bootstrap
# utilities that many post-install scripts need.
_BASELINE_APT_DEPS: tuple[str, ...] = (
    "git",
    "ca-certificates",
    "curl",
)


@dataclass
class ProvisionedSandbox:
    """Result of one on-the-fly provision attempt.

    Callers should check `available` — False means the provisioner couldn't
    boot the container or installs failed. `setup_log` is always populated
    so callers can surface the failure to the agent / scorecard.
    """

    available: bool
    container_id: str | None = None
    workspace_path: str | None = None
    env_spec: dict | None = None
    setup_log: list[dict] = field(default_factory=list)
    error: str | None = None


async def provision_on_the_fly(
    workspace_path: Path, env_spec: EnvSpec
) -> ProvisionedSandbox:
    """Boot + install. Returns a ProvisionedSandbox.

    Never raises — errors are captured on the returned object so caller
    can record them without wrapping in try/except.
    """
    log.info(
        "v3.provisioner.start",
        stack=env_spec.stack,
        base_image=env_spec.base_image,
        workspace=str(workspace_path),
        n_install_cmds=len(env_spec.install_commands),
        n_system_deps=len(env_spec.system_deps),
    )

    # ── Step 1: docker run the minimal base image ────────────────────────────
    container_id, err = await _docker_run_detached(env_spec.base_image)
    if err or not container_id:
        return ProvisionedSandbox(
            available=False,
            env_spec=env_spec.to_json(),
            error=f"docker_run_failed: {err}",
        )

    setup_log: list[dict] = []

    # ── Step 2: make /workspace and copy the repo in ─────────────────────────
    ok, err = await _exec_in_container(
        container_id,
        "mkdir -p /workspace && chmod 777 /workspace",
        as_root=True,
    )
    setup_log.append({"step": "mkdir_workspace", "ok": ok, "error": err})
    if not ok:
        await stop_sandbox(container_id)
        return ProvisionedSandbox(
            available=False,
            env_spec=env_spec.to_json(),
            setup_log=setup_log,
            error=f"mkdir_workspace: {err}",
        )

    ok, err = await _docker_cp_workspace(workspace_path, container_id)
    setup_log.append({"step": "docker_cp_workspace", "ok": ok, "error": err})
    if not ok:
        await stop_sandbox(container_id)
        return ProvisionedSandbox(
            available=False,
            env_spec=env_spec.to_json(),
            setup_log=setup_log,
            error=f"docker_cp: {err}",
        )

    # Fix /workspace ownership so non-root installs work. `|| true` guards
    # images where chown surfaces a harmless warning on symlinks.
    await _exec_in_container(
        container_id,
        "chown -R root:root /workspace || true",
        as_root=True,
    )
    # Also mark /workspace as a git safe.directory — otherwise git commands
    # inside the sandbox complain about dubious ownership (hit in the
    # humanize canary; that's why the workaround is baked in here).
    await _exec_in_container(
        container_id,
        "git config --system --add safe.directory /workspace || true",
        as_root=True,
    )

    # ── Step 3: apt-get install system deps (baseline + env_spec) ────────────
    apt_deps = list(_BASELINE_APT_DEPS) + [
        d for d in env_spec.system_deps if d not in _BASELINE_APT_DEPS
    ]
    if apt_deps:
        apt_cmd = (
            "apt-get update -qq && "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
            f"{' '.join(apt_deps)}"
        )
        ok, err = await _exec_in_container(container_id, apt_cmd, as_root=True)
        setup_log.append(
            {
                "step": "apt_install",
                "packages": apt_deps,
                "ok": ok,
                "error": err,
            }
        )
        if not ok:
            # Non-fatal: some base images (e.g. alpine) don't have apt. We log
            # and keep going; language installs may still succeed.
            log.warning(
                "v3.provisioner.apt_install_skipped_or_failed",
                container_id=container_id,
                error=err,
            )

    # ── Step 4: run env_spec.install_commands in order ───────────────────────
    for cmd in env_spec.install_commands:
        ok, err = await _exec_in_container(
            container_id, cmd, as_root=True, workdir="/workspace"
        )
        setup_log.append({"step": "install_command", "cmd": cmd, "ok": ok, "error": err})
        if not ok:
            # Hard fail: install command failure means the sandbox can't
            # reliably reproduce CI. Better to surface than to commit an
            # unverified patch.
            await stop_sandbox(container_id)
            return ProvisionedSandbox(
                available=False,
                container_id=container_id,
                workspace_path=str(workspace_path),
                env_spec=env_spec.to_json(),
                setup_log=setup_log,
                error=f"install_command_failed: {cmd}",
            )

    log.info(
        "v3.provisioner.ready",
        container_id=container_id,
        stack=env_spec.stack,
        base_image=env_spec.base_image,
        setup_steps=len(setup_log),
    )

    return ProvisionedSandbox(
        available=True,
        container_id=container_id,
        workspace_path=str(workspace_path),
        env_spec=env_spec.to_json(),
        setup_log=setup_log,
    )


async def stop_sandbox(container_id: str) -> None:
    """Best-effort stop + remove. Called when the run ends or provisioning fails.

    Silent on errors — we never want cleanup to cascade into a failure.
    """
    cmd = _settings.sandbox_docker_cmd
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, "rm", "-f", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.debug("v3.provisioner.stop_error", container_id=container_id, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers — docker subprocess wrappers
# ─────────────────────────────────────────────────────────────────────────────


async def _docker_run_detached(base_image: str) -> tuple[str | None, str | None]:
    """Boot a long-running container from `base_image` using `sleep infinity`.

    Returns (container_id, error_message). One of the two is always None.
    """
    docker_cmd = _settings.sandbox_docker_cmd
    run_tag = f"cifix-v3-{uuid.uuid4().hex[:8]}"
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_cmd,
            "run",
            "-d",
            "--rm",
            "--name", run_tag,
            "--network", "bridge",  # bridge needed for pip/apt downloads
            "--memory", "2g",       # on-the-fly installs can spike memory
            "--cpus", "2",
            "--entrypoint", "sleep",
            base_image,
            "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            return (None, stderr.decode(errors="replace").strip()[:500])
        return (stdout.decode().strip()[:12], None)
    except asyncio.TimeoutError:
        return (None, "docker_run_timeout")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


async def _docker_cp_workspace(
    workspace: Path, container_id: str
) -> tuple[bool, str | None]:
    """`docker cp <workspace>/. <container>:/workspace` — same pattern as v2."""
    docker_cmd = _settings.sandbox_docker_cmd
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_cmd,
            "cp",
            f"{workspace}/.",
            f"{container_id}:/workspace",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            return (False, stderr.decode(errors="replace").strip()[:500])
        return (True, None)
    except asyncio.TimeoutError:
        return (False, "docker_cp_timeout")
    except Exception as exc:  # noqa: BLE001
        return (False, f"{type(exc).__name__}: {exc}")


async def _exec_in_container(
    container_id: str,
    cmd: str,
    *,
    as_root: bool = False,
    workdir: str | None = None,
    timeout_s: int = _CMD_TIMEOUT_S,
) -> tuple[bool, str | None]:
    """Run `sh -c cmd` inside an existing container.

    Returns (ok, error_tail). error_tail is the last 500 chars of stderr on
    non-zero exit. On timeout we return (False, 'timeout').
    """
    docker_cmd = _settings.sandbox_docker_cmd
    args = [docker_cmd, "exec"]
    if as_root:
        args += ["--user", "0"]
    if workdir:
        args += ["--workdir", workdir]
    args += [
        "--env", "HOME=/root",
        "--env", "DEBIAN_FRONTEND=noninteractive",
        "--env", "PIP_CACHE_DIR=/tmp/pip-cache",
        "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
        container_id,
        "sh",
        "-c",
        cmd,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-500:]
            log.log(
                logging.WARNING,
                "v3.provisioner.exec_failed",
                container_id=container_id,
                cmd=cmd[:120],
                exit_code=proc.returncode,
                stderr_tail=tail,
            )
            return (False, tail)
        return (True, None)
    except asyncio.TimeoutError:
        return (False, "timeout")
    except Exception as exc:  # noqa: BLE001
        return (False, f"{type(exc).__name__}: {exc}")
