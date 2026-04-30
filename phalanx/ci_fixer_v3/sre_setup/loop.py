"""Agentic SRE setup loop — Phase 1.

`run_sre_setup_subagent` drives a Sonnet plan→act→observe loop bounded by
token budget (50_000), iteration cap (10), and a 3-strikes provider-
failure counter. Mirrors the v2 `run_coder_subagent` shape so the LLM
provider wiring (build_sonnet_coder_callable) plugs in directly.

The loop is invoked by `cifix_sre._execute_setup` (Phase 3 wires this in)
ONLY when the deterministic `env_detector` path leaves gaps that the
sandbox can't satisfy. Most runs never enter the loop.

See docs/ci-fixer-v3-agentic-sre.md §4-§7 for the design contract.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from phalanx.ci_fixer_v2.tools.base import ToolResult, ToolSchema
from phalanx.ci_fixer_v3.sre_setup.tools import SRE_SETUP_TOOLS, is_terminal_result

if TYPE_CHECKING:
    from phalanx.ci_fixer_v3.sre_setup.schemas import SREToolContext

log = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Loop configuration
# ────────────────────────────────────────────────────────────────────────

MAX_SETUP_ITERATIONS: int = 10
"""Hard cap on tool-use turns. Beyond this we PARTIAL-out with whatever
was installed. Matches design doc §4."""

MAX_SETUP_TOKENS: int = 50_000
"""Token budget (input+output combined) per setup invocation. Matches
design doc §4 — keeps a single setup under ~$0.20 on Sonnet."""

PROVIDER_STRIKES_LIMIT: int = 3
"""How many consecutive LLM provider errors (5xx, rate-limit, timeout)
before we bail to the deterministic fallback. Design doc §4 — keeps the
deterministic path as a hot fallback."""

_TOOL_DISPATCH_TIMEOUT_S: float = 60.0
"""Per-tool wall-clock cap. Generous — install_apt + apt-get update can
take 30s+ on a cold sandbox."""


# ────────────────────────────────────────────────────────────────────────
# Result type
# ────────────────────────────────────────────────────────────────────────


@dataclass
class SREResult:
    """Terminal outcome of one agentic SRE setup invocation.

    final_status mirrors the design doc §6 Task.output schema:
      READY    — every gap closed, sandbox can run all observed_failing_commands
      PARTIAL  — some gaps closed (loop exhausted, or provider degraded)
      BLOCKED  — explicit report_blocked or unrecoverable structural reason
    """

    final_status: str
    """READY | PARTIAL | BLOCKED."""

    capabilities: list[dict[str, Any]] = field(default_factory=list)
    """Final capabilities list, from report_ready/partial. Each item shape
    matches Capability dataclass (tool, version, install_method, evidence_ref)."""

    gaps_remaining: list[str] = field(default_factory=list)
    """First-tokens still missing (PARTIAL only)."""

    blocked_reason: str | None = None
    """BlockedReason enum value (BLOCKED only)."""

    blocked_evidence: dict[str, Any] | None = None
    """{file, line} dict if the LLM provided one (BLOCKED only)."""

    observed_token_status: list[dict[str, Any]] = field(default_factory=list)
    """Per-failing-command first-token availability check (READY only)."""

    setup_log: list[dict[str, Any]] = field(default_factory=list)
    """Audit trail of every tool call. Becomes Task.output.setup_log[]."""

    iterations_used: int = 0
    tokens_used: int = 0
    provider_strikes: int = 0
    fallback_used: bool = False
    """True iff we abandoned the LLM loop on provider strikes and surfaced
    PARTIAL with the deterministic det_spec only."""

    notes: str = ""
    """Free-form trailing context — e.g., why we bailed out."""


# ────────────────────────────────────────────────────────────────────────
# LLM call seam
# ────────────────────────────────────────────────────────────────────────

# The loop is provider-agnostic — caller passes in any Sonnet-compatible
# callable. Production: build_sonnet_coder_callable(...). Tests: scripted fake.
SonnetCallable = "Callable[[list[dict[str, Any]]], Awaitable[LLMResponse]]"


def _is_provider_error(exc: Exception) -> bool:
    """Distinguish "LLM provider degraded" from "real bug in our code".

    These three classes count toward strikes:
      - asyncio.TimeoutError
      - TimeoutError (the wrapped form raised by anthropic_sonnet)
      - Anything whose str() mentions rate_limit / overloaded / 5xx

    Other exceptions (TypeError, ValueError, etc.) are real code bugs;
    they propagate and the loop fails loudly. We never silently swallow.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    s = str(exc).lower()
    return any(t in s for t in ("rate_limit", "overloaded", "5xx", "503", "502", "504"))


# ────────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────────


def _build_seed_prompt(
    workspace_path: str,
    container_id: str,
    gaps: list[str],
    det_spec_summary: dict[str, Any],
    observed_failing_commands: list[str],
) -> str:
    """First user message for the loop — concrete inputs the agent acts on."""
    return (
        "You are the CI Fixer v3 SRE Agent. Your charter: prepare the sandbox "
        "to run the customer's failing CI commands. The deterministic env_detector "
        "has already run and provisioned a base sandbox; YOUR job is to close "
        "the GAPS the determinist couldn't.\n\n"
        f"Workspace path: {workspace_path}\n"
        f"Sandbox container_id: {container_id}\n\n"
        f"Gaps (first-tokens not yet available): {gaps}\n\n"
        f"Already installed by deterministic provisioner:\n{json.dumps(det_spec_summary, indent=2)}\n\n"
        f"Observed failing CI commands:\n"
        + "\n".join(f"  - {c}" for c in observed_failing_commands)
        + "\n\n"
        "Workflow you MUST follow:\n"
        "  1. INVESTIGATE: read .github/workflows/*.yml + pyproject.toml + "
        "package.json + .pre-commit-config.yaml as relevant.\n"
        "  2. PLAN: list the install steps you intend (in order).\n"
        "  3. EXECUTE: install one at a time; verify each with "
        "check_command_available before proceeding.\n"
        "  4. VERIFY: confirm every observed-failing-command's first-token exists.\n"
        "  5. REPORT: terminal tool — report_ready, report_partial, or report_blocked.\n\n"
        "HARD CONSTRAINTS:\n"
        "  - Every install_* call REQUIRES evidence_file + evidence_line "
        "pointing to where the package/tool is mentioned in the repo. "
        "The tool verifies the evidence is real; bad evidence rejects.\n"
        '  - Do NOT install packages without evidence. "Common Python repos '
        'use X" is NOT evidence.\n'
        "  - Do NOT run failing CI commands themselves (next agent's job).\n"
        "  - Do NOT edit files in the workspace.\n"
        "  - install_via_curl is restricted to a closed domain whitelist; "
        "arbitrary URLs reject.\n\n"
        "ESCALATE (call report_blocked) WHEN:\n"
        "  - Workflow needs ${{ matrix.* }} expansion (gha_context_required)\n"
        "  - Workflow has services: block (services_required)\n"
        "  - Workflow has container: directive (custom_container)\n"
        "  - sudo denied for system install (sudo_denied)\n"
        "  - All install methods failed for a tool (tool_unavailable)\n\n"
        f"Budget: {MAX_SETUP_ITERATIONS} tool calls, {MAX_SETUP_TOKENS} tokens combined."
    )


def _tool_result_message(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
    """Anthropic tool_result shape — copied from coder_subagent (bug #5).

    Keeps role=user with content=[{type: tool_result, ...}]. Anthropic
    rejects role=tool and rejects raw dicts in content.
    """
    body = json.dumps(result.to_tool_message_content())
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": body,
            }
        ],
    }


def _tool_error_message(tool_use_id: str, error: str) -> dict[str, Any]:
    body = json.dumps({"ok": False, "error": error})
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": body,
                "is_error": True,
            }
        ],
    }


def _to_anthropic_tool_schemas(tools: list[tuple[ToolSchema, Any]]) -> list[dict[str, Any]]:
    """Pack our ToolSchema list into the dict form Anthropic's tools= param wants."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.input_schema,
        }
        for s, _ in tools
    ]


async def run_sre_setup_subagent(
    ctx: SREToolContext,
    *,
    gaps: list[str],
    det_spec_summary: dict[str, Any],
    observed_failing_commands: list[str],
    llm_call: Any,
    max_iterations: int = MAX_SETUP_ITERATIONS,
    max_tokens: int = MAX_SETUP_TOKENS,
) -> SREResult:
    """Run the Sonnet-driven SRE setup loop until terminal status or budget hit.

    Args:
      ctx: prepared SREToolContext (container_id, workspace_path, exec_in_sandbox).
      gaps: first-tokens of failing commands not yet available in the sandbox.
      det_spec_summary: snapshot of what env_detector + deterministic provisioner
        already installed (so the agent doesn't re-install).
      observed_failing_commands: full failing-command strings (for the prompt).
      llm_call: provider callable. Production: build_sonnet_coder_callable(...).
      max_iterations / max_tokens: budget caps.

    Returns:
      SREResult with final_status one of READY / PARTIAL / BLOCKED.
    """
    handlers = {schema.name: handler for schema, handler in SRE_SETUP_TOOLS}
    anthropic_tools = _to_anthropic_tool_schemas(SRE_SETUP_TOOLS)
    tools_with_schema = "\n".join(f"  - {schema.name}" for schema, _ in SRE_SETUP_TOOLS)
    log.info(
        "v3.sre_setup.start",
        gaps=gaps,
        max_iter=max_iterations,
        max_tok=max_tokens,
        tools_available=tools_with_schema.replace("\n", ""),
    )

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _build_seed_prompt(
                workspace_path=ctx.workspace_path,
                container_id=ctx.container_id,
                gaps=gaps,
                det_spec_summary=det_spec_summary,
                observed_failing_commands=observed_failing_commands,
            ),
        }
    ]

    iterations = 0
    tokens = 0
    strikes = 0
    terminal_result: ToolResult | None = None

    for turn in range(max_iterations):
        iterations = turn + 1

        # Provider call with strike counting.
        try:
            response = (
                await llm_call(messages, tools=anthropic_tools)
                if _accepts_tools_kwarg(llm_call)
                else await llm_call(messages)
            )
        except Exception as exc:
            if _is_provider_error(exc):
                strikes += 1
                log.warning(
                    "v3.sre_setup.provider_strike",
                    turn=turn,
                    strike=strikes,
                    error=str(exc)[:200],
                )
                if strikes >= PROVIDER_STRIKES_LIMIT:
                    log.error(
                        "v3.sre_setup.provider_strikes_exhausted",
                        strikes=strikes,
                    )
                    return SREResult(
                        final_status="PARTIAL",
                        gaps_remaining=gaps,
                        setup_log=ctx.install_log,
                        iterations_used=iterations,
                        tokens_used=tokens,
                        provider_strikes=strikes,
                        fallback_used=True,
                        notes=(
                            f"agentic_unavailable_used_deterministic_only: "
                            f"{strikes} consecutive provider failures"
                        ),
                    )
                # Insert a synthetic "provider error, please retry" turn so
                # the next iteration tries fresh.
                continue
            # Non-provider exception — real bug, propagate.
            log.exception("v3.sre_setup.unexpected_error", error=str(exc))
            raise

        tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
        if tokens >= max_tokens:
            log.warning("v3.sre_setup.token_budget_exhausted", tokens=tokens)
            return SREResult(
                final_status="PARTIAL",
                gaps_remaining=gaps,
                setup_log=ctx.install_log,
                iterations_used=iterations,
                tokens_used=tokens,
                provider_strikes=strikes,
                notes="loop_exhausted: token budget hit",
            )

        # Echo assistant turn into history (bare-minimum content).
        assistant_blocks: list[dict[str, Any]] = []
        if response.text:
            assistant_blocks.append({"type": "text", "text": response.text})
        for use in response.tool_uses:
            assistant_blocks.append(
                {
                    "type": "tool_use",
                    "id": use.id,
                    "name": use.name,
                    "input": use.input,
                }
            )
        if assistant_blocks:
            messages.append({"role": "assistant", "content": assistant_blocks})

        # No tool calls — the agent gave up without reporting. Treat as PARTIAL.
        if not response.tool_uses:
            log.info(
                "v3.sre_setup.end_turn_no_tools",
                turn=turn,
                text_preview=(response.text or "")[:200],
            )
            return SREResult(
                final_status="PARTIAL",
                gaps_remaining=gaps,
                setup_log=ctx.install_log,
                iterations_used=iterations,
                tokens_used=tokens,
                provider_strikes=strikes,
                notes=f"agent_ended_without_terminal_call: {(response.text or '')[:200]}",
            )

        # Dispatch tool calls. Multiple tool_uses in one turn = parallel.
        for use in response.tool_uses:
            handler = handlers.get(use.name)
            if handler is None:
                messages.append(
                    _tool_error_message(
                        use.id,
                        (f"unknown_tool: {use.name}. Available: {sorted(handlers.keys())}"),
                    )
                )
                continue

            try:
                result = await asyncio.wait_for(
                    handler(ctx, use.input or {}),
                    timeout=_TOOL_DISPATCH_TIMEOUT_S,
                )
            except TimeoutError:
                result = ToolResult(
                    ok=False,
                    error=(
                        f"tool_timeout: {use.name} exceeded {_TOOL_DISPATCH_TIMEOUT_S}s wall-clock"
                    ),
                )

            messages.append(_tool_result_message(use.id, result))

            if is_terminal_result(result):
                terminal_result = result
                break

        if terminal_result is not None:
            break

    if terminal_result is None:
        log.warning(
            "v3.sre_setup.iter_budget_exhausted",
            iterations=iterations,
            tokens=tokens,
        )
        return SREResult(
            final_status="PARTIAL",
            gaps_remaining=gaps,
            setup_log=ctx.install_log,
            iterations_used=iterations,
            tokens_used=tokens,
            provider_strikes=strikes,
            notes=f"loop_exhausted: {iterations} iterations without terminal call",
        )

    # Terminal result — unpack into SREResult.
    data = terminal_result.data
    final_status = data.get("final_status", "PARTIAL")
    log.info(
        "v3.sre_setup.terminal",
        final_status=final_status,
        iterations=iterations,
        tokens=tokens,
        strikes=strikes,
    )
    return SREResult(
        final_status=final_status,
        capabilities=data.get("capabilities", []),
        gaps_remaining=data.get("gaps_remaining", []),
        blocked_reason=data.get("blocked_reason"),
        blocked_evidence=data.get("evidence"),
        observed_token_status=data.get("observed_token_status", []),
        setup_log=ctx.install_log,
        iterations_used=iterations,
        tokens_used=tokens,
        provider_strikes=strikes,
        notes=data.get("reason", ""),
    )


def _accepts_tools_kwarg(callable_: Any) -> bool:
    """Best-effort check: does the LLM callable accept tools= kwarg?

    The Sonnet provider built via build_sonnet_coder_callable closes over
    its own tools (so callers don't pass them). Tests inject a fake that
    may or may not accept them. We try with-tools first; if TypeError on
    unexpected kwarg, the wrapper retries without.

    For Phase 1 we keep it simple: inspect the signature.
    """
    import inspect

    try:
        sig = inspect.signature(callable_)
    except (TypeError, ValueError):
        return False
    return "tools" in sig.parameters
