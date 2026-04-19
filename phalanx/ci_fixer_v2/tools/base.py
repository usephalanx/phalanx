"""Tool interface for CI Fixer v2.

A tool is a small, deterministic Python callable exposed to the LLM via
its JSON Schema. Tools never contain LLM calls themselves; the single
exception is the `delegate_to_coder` tool, which spawns the Sonnet
subagent (itself a scoped agent, not a black-box LLM call).

Design:
  - Tools are registered once at import time in `phalanx.ci_fixer_v2.tools`.
  - Each tool declares its JSON Schema (OpenAI-compatible; also accepted
    by Anthropic's tool-use API).
  - Tools return structured `ToolResult`s; errors become `ToolError`-wrapped
    results rather than exceptions crossing the loop boundary.
  - Tools receive an `AgentContext` so they can record side effects
    (cost, workspace changes) without hidden globals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from phalanx.ci_fixer_v2.context import AgentContext


@dataclass
class ToolSchema:
    """OpenAI/Anthropic-compatible tool descriptor.

    Matches the "tool" shape both providers consume for tool-use loops.
    Per-provider formatting differences (e.g., OpenAI's `type: function`
    envelope) are handled at registration time — tools themselves only
    own the name, description, and input schema.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    """JSON Schema for `parameters` (OpenAI) / `input_schema` (Anthropic)."""


@dataclass
class ToolResult:
    """Structured result of a tool invocation.

    Always JSON-serializable — becomes the `tool_result` message content
    in the conversation history. Errors are surfaced via `error` rather
    than raised, so the agent can decide how to respond.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_tool_message_content(self) -> dict[str, Any]:
        """Shape consumed by the LLM-provider tool_result message slot."""
        if self.ok:
            return {"ok": True, **self.data}
        return {"ok": False, "error": self.error or "unknown_error"}


ToolCallable = Callable[["AgentContext", dict[str, Any]], Awaitable[ToolResult]]
"""Signature every tool must implement.

Tools are async because most of them do I/O (git, HTTP, subprocess) and
the loop is fully async. Sync tools wrap themselves in a thin async shim.
"""


class Tool(Protocol):
    """Structural contract — any object with these attrs is a valid tool.

    We use a Protocol rather than an ABC because tools are often tiny
    functions plus a schema; forcing class inheritance adds boilerplate
    without value.
    """

    schema: ToolSchema
    handler: ToolCallable


# ── Registry ──────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    """Register a tool under its schema name. Idempotent on re-import."""
    _REGISTRY[tool.schema.name] = tool
    return tool


def get(name: str) -> Tool:
    """Look up a registered tool by name. Raises KeyError on miss —
    the loop should guard with `is_registered` and map misses to a
    ToolResult error message to the agent, not let exceptions escape.
    """
    return _REGISTRY[name]


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def all_schemas() -> list[ToolSchema]:
    """Ordered schema list for passing to the LLM provider."""
    return [tool.schema for tool in _REGISTRY.values()]


def clear_registry_for_testing() -> None:
    """Test-only helper — resets the registry so each test starts clean."""
    _REGISTRY.clear()


class ToolError(Exception):
    """Raised only by registry-level bugs (missing tool, bad schema).

    Tool-level failures (HTTP errors, git failures, etc.) must NOT raise
    this — they become `ToolResult(ok=False, error=...)` so the agent
    can see the error and decide what to do.
    """
