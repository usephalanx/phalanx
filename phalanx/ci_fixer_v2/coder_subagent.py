"""Coder subagent — Sonnet-backed patch+verify loop invoked by the main
agent via the `delegate_to_coder` tool.

Scope (spec §5):
  - Input: a specific patch plan from the main agent (task_description,
    target_files, failing_command).
  - Tools: read_file, grep, apply_patch, run_in_sandbox ONLY.
  - Max 10 turns.
  - Output: verified unified diff + sandbox result + cost breakdown.

The subagent shares the main agent's AgentContext because:
  - `last_sandbox_verified` flipping is the signal the main agent needs to
    trust a commit; that flag lives on context.
  - Cost accounting lines up per-run.
The subagent cannot commit, comment, escalate, or reach GitHub — its tool
allow-list is enforced inside the loop below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import structlog

from phalanx.ci_fixer_v2 import tools as tools_module
from phalanx.ci_fixer_v2.agent import LLMResponse
from phalanx.ci_fixer_v2.config import MAX_SUBAGENT_TURNS
from phalanx.ci_fixer_v2.context import AgentContext

log = structlog.get_logger(__name__)


ALLOWED_CODER_TOOLS: frozenset[str] = frozenset(
    {"read_file", "grep", "apply_patch", "run_in_sandbox"}
)


# The subagent uses the same LLMResponse shape as the main agent but
# a Sonnet-specific call path. Real Sonnet wiring lands in Week 1.7;
# until then, tests inject a scripted fake here.
SonnetCallable = Callable[[list[dict[str, Any]]], Awaitable[LLMResponse]]


async def _call_sonnet_llm(_messages: list[dict[str, Any]]) -> LLMResponse:
    """Test seam for Sonnet 4.6 tool-use calls. Real wiring lands in
    Week 1.7 via phalanx.ci_fixer_v2.providers.sonnet.
    """
    raise NotImplementedError(
        "Sonnet LLM wiring lands in Week 1.7. Tests must patch "
        "`coder_subagent._call_sonnet_llm` with a scripted fake."
    )


@dataclass
class CoderResult:
    """Terminal outcome of one coder subagent invocation."""

    success: bool
    unified_diff: str = ""
    sandbox_exit_code: int = 0
    sandbox_stdout_tail: str = ""
    sandbox_stderr_tail: str = ""
    attempts_used: int = 0
    sonnet_input_tokens: int = 0
    sonnet_output_tokens: int = 0
    sonnet_thinking_tokens: int = 0
    notes: str = ""
    tool_invocations: list[dict[str, Any]] = field(default_factory=list)


def _tool_result_message(tool_use_id: str, result: Any) -> dict[str, Any]:
    # Anthropic requires tool_result.content to be a string or a list of
    # content blocks — a raw dict is rejected with 400 invalid_request_error.
    # We JSON-serialize the tool payload so the model sees a faithful
    # representation.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(result.to_tool_message_content()),
            }
        ],
    }


def _tool_error_message(tool_use_id: str, error: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": True,
                "content": json.dumps({"ok": False, "error": error}),
            }
        ],
    }


def _build_seed_prompt(
    task_description: str,
    target_files: list[str],
    diagnosis_summary: str,
    failing_command: str,
    max_attempts: int,
) -> str:
    return (
        "You are the Phalanx CI Fixer coder subagent. One job: apply a "
        "bounded patch, then run the ORIGINAL failing command in sandbox "
        "and see it pass.\n\n"
        f"TASK: {task_description}\n\n"
        f"TARGET FILES (do not touch anything else): {', '.join(target_files)}\n\n"
        f"DIAGNOSIS (from main agent): {diagnosis_summary}\n\n"
        f"FAILING COMMAND TO VERIFY AGAINST: {failing_command}\n\n"
        f"MAX ATTEMPTS: {max_attempts}\n\n"
        "Available tools: read_file, grep, apply_patch, run_in_sandbox.\n"
        "You cannot commit, comment, or reach GitHub — that's the main "
        "agent's job. Finish by running the failing command in sandbox; "
        "if it exits 0 you're done. If you can't make it pass in the "
        "turn budget, stop with a short explanation."
    )


async def run_coder_subagent(
    ctx: AgentContext,
    task_description: str,
    target_files: list[str],
    diagnosis_summary: str,
    failing_command: str,
    max_attempts: int = 3,
    max_turns: int = MAX_SUBAGENT_TURNS,
    llm_call: SonnetCallable | None = None,
) -> CoderResult:
    """Run the Sonnet coder loop until sandbox verification or turn cap.

    Args:
      ctx:                The main agent's AgentContext (shared).
      task_description:   Specific patch plan from the main agent.
      target_files:       Files the subagent may edit (apply_patch checks).
      diagnosis_summary:  One-paragraph diagnosis from main agent.
      failing_command:    Exact CI command that must pass in sandbox.
      max_attempts:       How many apply_patch attempts are expected
                          (structural limit = max_turns; max_attempts is
                          surfaced in the prompt and in CoderResult).
      max_turns:          Hard structural turn cap.
      llm_call:           Override for the Sonnet call (tests use this;
                          production leaves it None and the default seam
                          `_call_sonnet_llm` is used).
    """
    call = llm_call if llm_call is not None else _call_sonnet_llm

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _build_seed_prompt(
                task_description,
                target_files,
                diagnosis_summary,
                failing_command,
                max_attempts,
            ),
        }
    ]
    attempts_used = 0
    tokens_in = 0
    tokens_out = 0
    tokens_thinking = 0
    tool_trace: list[dict[str, Any]] = []
    last_sandbox_stdout = ""
    last_sandbox_stderr = ""
    last_sandbox_exit_code = 0
    last_notes = ""

    logger = log.bind(ci_fix_run_id=ctx.ci_fix_run_id, subagent="coder")

    for turn in range(max_turns):
        logger.info("v2.coder.turn_start", turn=turn, messages=len(messages))
        response = await call(messages)
        tokens_in += response.input_tokens
        tokens_out += response.output_tokens
        tokens_thinking += response.thinking_tokens
        logger.info(
            "v2.coder.turn_response",
            turn=turn,
            stop_reason=response.stop_reason,
            tool_uses=[t.name for t in response.tool_uses],
            text_preview=response.text[:120] if response.text else "",
        )

        if response.stop_reason == "end_turn" and not response.tool_uses:
            last_notes = response.text or last_notes
            logger.info("v2.coder.end_turn", turn=turn, notes=last_notes[:200])
            break

        # Echo the assistant turn into the messages (bare minimum content).
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
        messages.append({"role": "assistant", "content": assistant_blocks})

        for use in response.tool_uses:
            # Enforce the coder's scoped tool allow-list.
            if use.name not in ALLOWED_CODER_TOOLS:
                messages.append(
                    _tool_error_message(
                        use.id,
                        f"tool_not_available_to_coder_subagent: {use.name}. "
                        f"Allowed: {sorted(ALLOWED_CODER_TOOLS)}",
                    )
                )
                tool_trace.append(
                    {"turn": turn, "tool": use.name, "error": "out_of_scope"}
                )
                continue

            if not tools_module.base.is_registered(use.name):
                messages.append(
                    _tool_error_message(
                        use.id, f"tool_not_registered: {use.name}"
                    )
                )
                tool_trace.append(
                    {"turn": turn, "tool": use.name, "error": "not_registered"}
                )
                continue

            tool = tools_module.base.get(use.name)
            result = await tool.handler(ctx, use.input)

            if use.name == "apply_patch":
                attempts_used += 1
            if use.name == "run_in_sandbox":
                last_sandbox_exit_code = int(result.data.get("exit_code") or 0)
                # Keep only a tail for the CoderResult — LLM output is large.
                stdout_full = result.data.get("stdout") or ""
                stderr_full = result.data.get("stderr") or ""
                last_sandbox_stdout = stdout_full[-2000:]
                last_sandbox_stderr = stderr_full[-2000:]

            messages.append(_tool_result_message(use.id, result))
            tool_trace.append(
                {
                    "turn": turn,
                    "tool": use.name,
                    "ok": result.ok,
                    "error": None if result.ok else result.error,
                }
            )

            # Early-exit inside turn: as soon as sandbox verification
            # flips, we're done — no reason to burn more turns.
            if use.name == "run_in_sandbox" and ctx.last_sandbox_verified:
                logger.info(
                    "v2.coder.sandbox_verified",
                    turn=turn,
                    attempts_used=attempts_used,
                )
                # Cost into the shared record.
                ctx.cost.sonnet_coder_input_tokens += tokens_in
                ctx.cost.sonnet_coder_output_tokens += tokens_out
                ctx.cost.sonnet_coder_thinking_tokens += tokens_thinking
                return CoderResult(
                    success=True,
                    unified_diff="",  # filled in by delegate_to_coder after
                    sandbox_exit_code=last_sandbox_exit_code,
                    sandbox_stdout_tail=last_sandbox_stdout,
                    sandbox_stderr_tail=last_sandbox_stderr,
                    attempts_used=attempts_used,
                    sonnet_input_tokens=tokens_in,
                    sonnet_output_tokens=tokens_out,
                    sonnet_thinking_tokens=tokens_thinking,
                    notes=response.text or "sandbox verified",
                    tool_invocations=tool_trace,
                )

    # Turn cap or clean end_turn without verification.
    ctx.cost.sonnet_coder_input_tokens += tokens_in
    ctx.cost.sonnet_coder_output_tokens += tokens_out
    ctx.cost.sonnet_coder_thinking_tokens += tokens_thinking
    return CoderResult(
        success=False,
        unified_diff="",
        sandbox_exit_code=last_sandbox_exit_code,
        sandbox_stdout_tail=last_sandbox_stdout,
        sandbox_stderr_tail=last_sandbox_stderr,
        attempts_used=attempts_used,
        sonnet_input_tokens=tokens_in,
        sonnet_output_tokens=tokens_out,
        sonnet_thinking_tokens=tokens_thinking,
        notes=last_notes or "turn cap or end_turn without verified fix",
        tool_invocations=tool_trace,
    )
