"""Schemas for agentic SRE setup tools (Phase 0).

Dataclasses + enums shared across tools. Kept narrow on purpose — see the
full design at docs/ci-fixer-v3-agentic-sre.md §6 for the wider Task.output
schema (which composes these).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from phalanx.ci_fixer_v3.provisioner import ExecResult


class BlockedReason(StrEnum):
    """Why SRE setup couldn't proceed.

    Closed enum (not free-form string). Each value maps to a concrete failure
    mode in the design doc §8. Keeps cross-agent contracts machine-readable —
    commander matches on the enum to decide how to terminate the run.
    """

    GHA_CONTEXT_REQUIRED = "gha_context_required"
    """Workflow uses ${{ matrix.* }} or similar GHA-only expressions essential
    to command execution. Cannot replicate outside GitHub Actions."""

    SERVICES_REQUIRED = "services_required"
    """Workflow has `services:` block (sidecar databases, redis, etc.).
    Out of scope for v3 sandbox."""

    CUSTOM_CONTAINER = "custom_container"
    """Workflow has `container:` directive (custom upstream image).
    Out of scope for v3 — would need base-image swap."""

    SUDO_DENIED = "sudo_denied"
    """System install needed but sudo unavailable in the sandbox."""

    TOOL_UNAVAILABLE = "tool_unavailable"
    """Tool can't be installed via any available method (apt + pip + curl
    all failed)."""

    LOOP_EXHAUSTED = "loop_exhausted"
    """Iteration or token budget hit before all gaps closed AND fallback
    isn't available."""

    EVIDENCE_MISSING = "evidence_missing"
    """LLM tried to install something without valid repo evidence. The
    install tool refused; the agent gave up rather than try alternatives."""

    TOOL_CHAIN_BLOCKED = "tool_chain_blocked"
    """Install A succeeded, but A needs B at runtime, and B isn't
    installable via any method we have."""


@dataclass(frozen=True)
class Capability:
    """One installed tool that the agentic SRE put into the sandbox.

    Recorded in Task.output.capabilities_installed so commander, scorecard,
    and operators can audit what was added per run.
    """

    tool: str
    """Canonical tool name as exposed on the shell PATH (e.g., 'uv', 'tox')."""

    version: str
    """Resolved version string from `<tool> --version` after install.
    Empty string if probing failed (still an INSTALLED capability though)."""

    install_method: str
    """How the tool was installed: 'apt' | 'pip' | 'curl' | 'preinstalled'."""

    evidence_ref: str
    """`<file>:<line>` pointing to where in the repo the tool was evidenced
    (workflow YAML `uses:`, pyproject deps, etc.). Required — see §4 of
    the design doc on the no-evidence-no-install constraint."""


@dataclass(frozen=True)
class ObservedTokenStatus:
    """Per-failing-command first-token availability check.

    The agentic SRE's CHECK_GAPS phase iterates over observed_failing_commands,
    extracts the first shell token, and probes each via
    `check_command_available`. The result is recorded so commander can see
    whether the sandbox is genuinely ready before dispatching TL.
    """

    cmd: str
    """Original failing command, e.g., 'uvx --with tox-uv tox -e mypy'."""

    first_token: str
    """First shell token of cmd, e.g., 'uvx'."""

    found: bool
    """True iff `command -v <first_token>` returns 0 in the sandbox."""


@dataclass
class SREToolContext:
    """Runtime context handed to every tool handler.

    Holds the sandbox the tool acts on plus a callable for sandbox exec
    (so tier-1 tests can inject a mock without monkey-patching globals).
    Mutable on purpose — tools append to install_log over the loop's lifetime.
    """

    container_id: str
    """Active sandbox provisioned by `provision_bare_sandbox`. Tools that
    install things target this container."""

    workspace_path: str
    """Absolute path on the HOST where the repo is cloned. Read-only to
    tools (no editing); used by read_file and list_workflows."""

    exec_in_sandbox: Callable[..., Awaitable[ExecResult]]
    """Async callable: (container_id, cmd, *, as_root=False, timeout_s=...)
    → ExecResult. Production wires this to provisioner._exec_in_container.
    Tests inject a fake."""

    install_log: list[dict] = None  # type: ignore[assignment]
    """Append-only list of structured tool-call records (each tool emits one
    entry on every invocation). Becomes Task.output.setup_log[] at end of
    setup."""

    def __post_init__(self) -> None:
        if self.install_log is None:
            self.install_log = []
