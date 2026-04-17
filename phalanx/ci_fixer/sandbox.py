"""
SandboxProvisioner — selects and provisions an isolated execution environment
for the CI reproducer and fix agents.

Design:
  - Stack detection is pure file-existence: no subprocess, no LLM call
  - provision() checks out a pre-warmed container from SandboxPool and
    bind-mounts the workspace path into it at /workspace.
  - sandbox_enabled=False fast-path returns None → reproducer uses "skipped"
  - SandboxUnavailableError (pool timeout / Docker down) → SandboxResult
    with available=False → reproducer/verifier fall back to local subprocess

See docs/sandbox_pool_design.md for the full design rationale.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from phalanx.ci_fixer.sandbox_pool import (
    PooledContainer,
    SandboxUnavailableError,
    get_sandbox_pool,
)
from phalanx.config.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)
settings = get_settings()

# ── Stack detection markers ───────────────────────────────────────────────────
# Ordered by priority: first match wins when multiple markers coexist.
_STACK_FILES: dict[str, list[str]] = {
    "python": ["pyproject.toml", "requirements.txt", "setup.py"],
    "node": ["package.json"],
    "go": ["go.mod"],
    "rust": ["Cargo.toml"],
}

# ── Docker images per stack ───────────────────────────────────────────────────
# Slim/alpine variants: fastest pull, fewest CVEs, sufficient for lint/type tools.
_STACK_IMAGES: dict[str, str] = {
    "python": "python:3.12-slim",
    "node": "node:20-slim",
    "go": "golang:1.22-alpine",
    "rust": "rust:1.77-slim",
    "unknown": "ubuntu:22.04",
}


@dataclass
class SandboxResult:
    """Describes the provisioned sandbox environment for a single fix run."""

    sandbox_id: str
    """Unique ID for this sandbox instance: 'phalanx-sandbox-{8 hex chars}'."""

    stack: str
    """Detected tech stack: 'python', 'node', 'go', 'rust', 'unknown'."""

    image: str
    """Docker image the container was started from."""

    workspace_path: str
    """Host path bind-mounted into the container at /workspace."""

    available: bool = True
    """
    False when the sandbox is not usable:
      - sandbox_enabled=False in settings
      - Docker daemon unreachable
      - Pool checkout timed out (all slots busy)
    When False, ReproducerAgent and VerifierAgent fall back to local subprocess.
    """

    container_id: str = ""
    """
    Docker container ID (short hash) when a pool slot was successfully checked out.
    Empty string means local subprocess fallback is in effect.
    """

    mount_path: str = "/workspace"
    """Path inside the container where workspace_path is bind-mounted."""

    extra: dict = field(default_factory=dict)
    """Reserved for future metadata (port map, resource stats, etc.)."""


class SandboxProvisioner:
    """
    Provisions a sandbox for a given workspace by checking out a pre-warmed
    container from SandboxPool and bind-mounting the workspace into it.

    Fallback chain (no regressions):
      sandbox_enabled=False → return None
      pool checkout timeout  → SandboxResult(available=False, container_id="")
      Docker daemon missing  → SandboxResult(available=False, container_id="")
      happy path             → SandboxResult(available=True, container_id="abc123")
    """

    def detect_stack(self, workspace_path: Path) -> str:
        """
        Infer the primary tech stack from marker files in workspace_path.

        Returns the first matching stack name from _STACK_FILES, or 'unknown'
        if no markers are found.  Order matters: python is checked first so
        a monorepo with both pyproject.toml and package.json resolves to python.
        """
        for stack, markers in _STACK_FILES.items():
            if any((workspace_path / marker).exists() for marker in markers):
                return stack
        return "unknown"

    async def provision(
        self,
        workspace_path: Path,
        stack_hint: str | None = None,
    ) -> SandboxResult | None:
        """
        Return a SandboxResult for workspace_path, or None if sandbox is disabled.

        Args:
            workspace_path: Absolute path to the cloned repo on the host.
            stack_hint:     Override stack detection (e.g. caller already knows
                            the stack from structured_failure).

        Returns:
            SandboxResult with container_id populated (happy path),
            SandboxResult with available=False (pool exhausted / Docker down),
            or None (sandbox_enabled=False).
        """
        if not settings.sandbox_enabled:
            log.info("ci_fixer.sandbox_disabled")
            return None

        stack = stack_hint if stack_hint else self.detect_stack(workspace_path)
        image = _STACK_IMAGES.get(stack, _STACK_IMAGES["unknown"])
        sandbox_id = f"phalanx-sandbox-{uuid.uuid4().hex[:8]}"

        try:
            pool = await get_sandbox_pool()
            container = await pool.checkout(
                stack,
                timeout=settings.sandbox_checkout_timeout_seconds,
            )

            # Bind-mount the workspace into the container.
            # docker run used --volume /tmp:/hosttmp; we create a per-run symlink
            # inside the container pointing /workspace → the actual cloned path.
            # For simplicity we use docker cp for the initial seed if the bind
            # mount path isn't already accessible.
            await self._bind_workspace(container.container_id, workspace_path)

            result = SandboxResult(
                sandbox_id=sandbox_id,
                stack=stack,
                image=image,
                workspace_path=str(workspace_path),
                available=True,
                container_id=container.container_id,
            )

            log.info(
                "ci_fixer.sandbox_provisioned",
                sandbox_id=sandbox_id,
                stack=stack,
                container_id=container.container_id,
            )
            return result

        except SandboxUnavailableError as exc:
            log.warning(
                "ci_fixer.sandbox_unavailable",
                sandbox_id=sandbox_id,
                stack=stack,
                error=str(exc),
            )
            return SandboxResult(
                sandbox_id=sandbox_id,
                stack=stack,
                image=image,
                workspace_path=str(workspace_path),
                available=False,
                container_id="",
            )

        except Exception as exc:
            log.warning(
                "ci_fixer.sandbox_provision_error",
                sandbox_id=sandbox_id,
                stack=stack,
                error=str(exc),
            )
            return SandboxResult(
                sandbox_id=sandbox_id,
                stack=stack,
                image=image,
                workspace_path=str(workspace_path),
                available=False,
                container_id="",
            )

    async def release(self, sandbox_result: SandboxResult) -> None:
        """
        Return the container back to the pool after the fix run completes.
        Safe to call even when container_id is empty (no-op).
        """
        if not sandbox_result.container_id:
            return

        try:
            pool = await get_sandbox_pool()
            container = PooledContainer(
                container_id=sandbox_result.container_id,
                stack=sandbox_result.stack,
                image=sandbox_result.image,
            )
            await pool.checkin(container)
            log.info(
                "ci_fixer.sandbox_released",
                container_id=sandbox_result.container_id,
                stack=sandbox_result.stack,
            )
        except Exception as exc:
            log.warning(
                "ci_fixer.sandbox_release_error",
                container_id=sandbox_result.container_id,
                error=str(exc),
            )

    async def _bind_workspace(self, container_id: str, workspace_path: Path) -> None:
        """
        Make the workspace accessible at /workspace inside the container.

        Strategy: docker cp the workspace contents into the container.
        This is safe for the typical repo size (< 50MB of source).
        For large repos, a bind-mount at container start time is preferred
        (set via docker run -v flag in SandboxPool._start_container).
        """
        import asyncio

        cmd = settings.sandbox_docker_cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, "cp",
                f"{workspace_path}/.",
                f"{container_id}:/workspace",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.warning(
                    "ci_fixer.sandbox_cp_failed",
                    container_id=container_id,
                    error=stderr.decode().strip(),
                )
        except Exception as exc:
            log.warning(
                "ci_fixer.sandbox_cp_error",
                container_id=container_id,
                error=str(exc),
            )
