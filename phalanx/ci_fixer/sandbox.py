"""
SandboxProvisioner — selects and provisions an isolated execution environment
for the CI reproducer and fix agents.

Design:
  - Stack detection is pure file-existence: no subprocess, no LLM call
  - Phase 2: provision() returns a SandboxResult describing the target env
    but does NOT actually start a container (real Docker exec is Phase 3)
  - Async signature on provision() is intentional — Phase 3 will make real
    async Docker calls; callers already use `await provisioner.provision()`
  - sandbox_enabled=False fast-path returns None → reproducer uses "skipped"
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

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
    """Docker image that will be used when the container starts (Phase 3)."""

    workspace_path: str
    """Host path that will be bind-mounted into the container."""

    available: bool = True
    """False if the Docker daemon is unreachable — reproducer will skip."""

    extra: dict = field(default_factory=dict)
    """Reserved for Phase 3 metadata (container ID, port map, etc.)."""


class SandboxProvisioner:
    """
    Provisions a sandbox descriptor for a given workspace.

    Phase 2 behaviour: detects stack, builds a SandboxResult, but does NOT
    start any container.  The returned object is immediately usable by
    ReproducerAgent for subprocess-based reproduction (no Docker yet).

    Phase 3 will override provision() to actually start the container and
    set available=False if the daemon is unreachable.
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
            stack_hint: Override stack detection (e.g. if caller already knows
                        the stack from structured_failure).  Must be one of the
                        keys in _STACK_FILES or 'unknown'.

        Returns:
            SandboxResult with available=True, or None when sandbox_enabled=False.
        """
        if not settings.sandbox_enabled:
            log.info("ci_fixer.sandbox_disabled")
            return None

        stack = stack_hint if stack_hint else self.detect_stack(workspace_path)
        image = _STACK_IMAGES.get(stack, _STACK_IMAGES["unknown"])
        sandbox_id = f"phalanx-sandbox-{uuid.uuid4().hex[:8]}"

        result = SandboxResult(
            sandbox_id=sandbox_id,
            stack=stack,
            image=image,
            workspace_path=str(workspace_path),
        )

        log.info(
            "ci_fixer.sandbox_provisioned",
            sandbox_id=sandbox_id,
            stack=stack,
            image=image,
            workspace=str(workspace_path),
        )
        return result
