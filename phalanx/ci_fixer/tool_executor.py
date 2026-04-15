"""
ToolExecutor — allowlist-gated subprocess runner for the CI fixer agent.

The CI fixer agent (Claude) can call run_command() to execute fix and
validation commands against the cloned workspace. Before any subprocess
is spawned, the first token of the command is checked against the
per-integration allowed_tools list.

This is the only safety gate between Claude's tool calls and the OS.

Design:
  - Allowlist checked on the first whitespace-delimited token only
    (e.g. "ruff check src/" → token "ruff")
  - npx-prefixed commands check the second token
    (e.g. "npx eslint src/" → token "eslint")
  - No shell=True — command is split and passed as a list
  - Working directory is always the cloned workspace (never /tmp etc.)
  - Hard timeout per command (_EXEC_TIMEOUT seconds)
  - Returns ToolResult — never raises
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_EXEC_TIMEOUT = 120   # seconds per command
_MAX_OUTPUT = 8_000   # chars of stdout+stderr to return to LLM

# Binaries that are always blocked regardless of allowlist
# (interactive, destructive, or network-exfil risk)
_HARD_BLOCKED = frozenset({
    "rm", "rmdir", "dd", "mkfs", "fdisk",
    "curl", "wget", "nc", "netcat", "ssh",
    "sudo", "su", "chmod", "chown",
    "git",          # agent does git ops separately after fix is verified
    "docker", "kubectl",
    "bash", "sh", "zsh", "fish",    # no shell passthrough
    "python", "python3", "node",    # no interpreter passthrough
})


@dataclass
class ToolResult:
    """Result of a single tool execution."""
    exit_code: int
    stdout: str
    stderr: str
    blocked: bool = False
    block_reason: str = ""

    @property
    def output(self) -> str:
        """Combined stdout+stderr, truncated to _MAX_OUTPUT chars."""
        combined = (self.stdout + "\n" + self.stderr).strip()
        if len(combined) > _MAX_OUTPUT:
            return combined[:_MAX_OUTPUT] + f"\n... (truncated, {len(combined)} total chars)"
        return combined

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.blocked


class ToolExecutor:
    """
    Executes shell commands on behalf of the CI fixer agent.

    All commands are checked against:
      1. Hard-blocked binaries (always rejected)
      2. Per-integration allowed_tools list (must contain the command binary)

    Working directory is always the cloned workspace root.
    """

    def __init__(self, workspace: Path, allowed_tools: list[str]) -> None:
        self._workspace = workspace
        self._allowed = frozenset(t.lower().strip() for t in allowed_tools)

    def run(self, cmd: str) -> ToolResult:
        """
        Execute cmd in the workspace directory.

        Returns ToolResult. Never raises.
        """
        binary = self._extract_binary(cmd)
        if binary is None:
            return ToolResult(
                exit_code=1, stdout="", stderr="",
                blocked=True, block_reason="empty or unparseable command",
            )

        # Hard block first
        if binary in _HARD_BLOCKED:
            log.warning("tool_executor.hard_blocked", cmd=cmd, binary=binary)
            return ToolResult(
                exit_code=1, stdout="", stderr="",
                blocked=True,
                block_reason=f"'{binary}' is hard-blocked for security reasons",
            )

        # Allowlist check
        if binary not in self._allowed:
            log.warning("tool_executor.not_allowed", cmd=cmd, binary=binary, allowed=sorted(self._allowed))
            return ToolResult(
                exit_code=1, stdout="", stderr="",
                blocked=True,
                block_reason=(
                    f"'{binary}' is not in the allowed_tools list for this integration. "
                    f"Allowed: {sorted(self._allowed)}"
                ),
            )

        # Workspace must exist
        if not self._workspace.exists():
            return ToolResult(
                exit_code=1, stdout="", stderr="workspace directory not found",
                blocked=True, block_reason="workspace missing",
            )

        try:
            args = shlex.split(cmd)
        except ValueError as exc:
            return ToolResult(
                exit_code=1, stdout="", stderr=f"command parse error: {exc}",
                blocked=True, block_reason="shlex parse failed",
            )

        log.info("tool_executor.run", binary=binary, cmd=cmd[:120])
        try:
            result = subprocess.run(
                args,
                cwd=str(self._workspace),
                capture_output=True,
                text=True,
                timeout=_EXEC_TIMEOUT,
            )
            log.info(
                "tool_executor.done",
                binary=binary,
                exit_code=result.returncode,
                stdout_len=len(result.stdout),
                stderr_len=len(result.stderr),
            )
            return ToolResult(
                exit_code=result.returncode,
                stdout=result.stdout[:_MAX_OUTPUT],
                stderr=result.stderr[:_MAX_OUTPUT],
            )
        except subprocess.TimeoutExpired:
            log.warning("tool_executor.timeout", cmd=cmd, timeout=_EXEC_TIMEOUT)
            return ToolResult(
                exit_code=1,
                stdout="",
                stderr=f"command timed out after {_EXEC_TIMEOUT}s",
            )
        except FileNotFoundError:
            return ToolResult(
                exit_code=1,
                stdout="",
                stderr=f"binary not found: {binary}",
            )
        except Exception as exc:
            log.warning("tool_executor.error", cmd=cmd, error=str(exc))
            return ToolResult(exit_code=1, stdout="", stderr=str(exc))

    @staticmethod
    def _extract_binary(cmd: str) -> str | None:
        """
        Extract the binary name from a command string.

        Handles:
          "ruff check src/"          → "ruff"
          "npx eslint src/"          → "eslint"  (skip npx wrapper)
          "  ruff  check  "          → "ruff"
        """
        cmd = cmd.strip()
        if not cmd:
            return None
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return None
        if not tokens:
            return None
        binary = tokens[0].lower()
        # npx is a wrapper — check what it's running
        if binary == "npx" and len(tokens) > 1:
            return tokens[1].lower()
        return binary
