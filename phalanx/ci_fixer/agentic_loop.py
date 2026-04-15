"""
Agentic CI Fix Loop — tool-agnostic LLM-driven repair pipeline.

Replaces repair_agent.py FSM. Instead of a rigid state machine with
hardcoded ruff/mypy/pytest validators, the LLM is given 4 tools and
figures out on its own how to fix the CI failure:

    read_file     — read a file from the workspace
    write_file    — write (overwrite) a file in the workspace
    run_command   — execute a shell command (gated by ToolExecutor allowlist)
    finish        — declare success or give up

Design principles (SWE-agent / OpenHands pattern):
  - No hardcoded tool names, validators, or fix patterns
  - LLM generates the commands; ToolExecutor enforces safety
  - Max _MAX_TURNS turns before giving up
  - Path traversal rejected: all file paths must be under workspace
  - No files outside workspace written ever
  - All run_command calls go through ToolExecutor (hard-block + allowlist)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import structlog

from phalanx.ci_fixer.analyst import FilePatch, FixPlan
from phalanx.ci_fixer.repair_agent import RepairResult
from phalanx.ci_fixer.tool_executor import ToolExecutor, ToolResult

if TYPE_CHECKING:
    from phalanx.ci_fixer.context_retriever import ContextBundle

log = structlog.get_logger(__name__)

_MAX_TURNS = 8          # max LLM→tool→LLM cycles
_MAX_FILE_READ = 20_000  # max chars returned per read_file call
_MAX_FILE_WRITE = 80_000  # max bytes per write_file call


# ── Tool schemas (passed to Claude as tool_use definitions) ───────────────────


_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository workspace. "
            "Returns the file contents as a string. "
            "Path must be relative to the repository root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file from the repository root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write (overwrite) a file in the repository workspace. "
            "This is the only way to apply fixes. "
            "Path must be relative to the repository root. "
            "Content must be the full new file contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file from the repository root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full new contents of the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the repository root directory. "
            "Only commands in the integration's allowed_tools list are permitted. "
            "Use this to run linters, type checkers, or test runners to verify your fix."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run (e.g. 'ruff check src/', 'pytest tests/').",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Declare the fix complete (success=true) or give up (success=false). "
            "Call this when the CI failure is fixed and validation passes, "
            "or when you determine the failure cannot be fixed automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "True if the fix is complete and validated, False to give up.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Brief explanation. If success=false, explain why the fix failed. "
                        "If success=true, summarize what was fixed."
                    ),
                },
                "files_written": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative paths of files that were written/modified.",
                },
            },
            "required": ["success", "reason"],
        },
    },
]


# ── Tool dispatch ──────────────────────────────────────────────────────────────


def _safe_path(workspace: Path, rel_path: str) -> Path | None:
    """
    Resolve rel_path against workspace and verify no path traversal.
    Returns None if the path would escape workspace.
    """
    try:
        target = (workspace / rel_path).resolve()
        workspace_resolved = workspace.resolve()
        target.relative_to(workspace_resolved)  # raises ValueError if outside
        return target
    except (ValueError, OSError):
        return None


def _execute_tool(
    tool_name: str,
    tool_input: dict,
    workspace: Path,
    executor: ToolExecutor,
) -> str:
    """
    Dispatch a tool call from the LLM.
    Returns a string result to feed back to the LLM as tool_result content.
    """
    if tool_name == "read_file":
        rel_path = tool_input.get("path", "")
        target = _safe_path(workspace, rel_path)
        if target is None:
            return f"ERROR: path traversal rejected: {rel_path}"
        if not target.exists():
            return f"ERROR: file not found: {rel_path}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > _MAX_FILE_READ:
                content = content[:_MAX_FILE_READ] + f"\n... (truncated, {len(content)} total chars)"
            return content
        except OSError as exc:
            return f"ERROR: could not read {rel_path}: {exc}"

    elif tool_name == "write_file":
        rel_path = tool_input.get("path", "")
        content = tool_input.get("content", "")
        target = _safe_path(workspace, rel_path)
        if target is None:
            return f"ERROR: path traversal rejected: {rel_path}"
        if len(content.encode()) > _MAX_FILE_WRITE:
            return f"ERROR: content too large ({len(content.encode())} bytes, max {_MAX_FILE_WRITE})"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} chars to {rel_path}"
        except OSError as exc:
            return f"ERROR: could not write {rel_path}: {exc}"

    elif tool_name == "run_command":
        command = tool_input.get("command", "")
        result: ToolResult = executor.run(command)
        if result.blocked:
            return f"BLOCKED: {result.block_reason}"
        status = "PASSED" if result.passed else f"FAILED (exit {result.exit_code})"
        return f"{status}\n{result.output}"

    elif tool_name == "finish":
        # Handled by caller — should not reach here
        return "OK"

    else:
        return f"ERROR: unknown tool: {tool_name}"


# ── System prompt ──────────────────────────────────────────────────────────────


def _build_system_prompt(context: "ContextBundle") -> str:
    lines = [
        "You are an expert software engineer fixing a CI failure.",
        "",
        "Your job:",
        "1. Read the failing files",
        "2. Fix the specific errors reported in the CI log",
        "3. Validate your fix by running the appropriate tool",
        "4. Call finish(success=true) when the CI passes, or finish(success=false) if you cannot fix it",
        "",
        "Rules:",
        "- Only fix what is broken. Do not refactor, rename, or restructure.",
        "- Do not modify test files.",
        "- Call finish() as soon as validation passes — do not keep going.",
        "- If a run_command is BLOCKED, it means that tool is not permitted. Use a different approach.",
        "- If you cannot fix the failure after a few attempts, call finish(success=false).",
    ]

    lines += [
        "",
        "=== CI FAILURE CONTEXT ===",
        f"Tool: {context.classification.tool}",
        f"Failure type: {context.classification.failure_type}",
        f"Root cause hypothesis: {context.classification.root_cause_hypothesis}",
        "",
        "=== FAILING ERRORS ===",
        context.log_excerpt[:2000] if context.log_excerpt else "(no log excerpt)",
    ]

    if context.file_contents:
        lines += ["", "=== FILES (pre-loaded for you) ==="]
        for rel_path, content in context.file_contents.items():
            preview = content[:3000]
            suffix = f"\n... ({len(content)} chars total)" if len(content) > 3000 else ""
            lines += [f"--- {rel_path} ---", preview + suffix, ""]

    if context.similar_fixes:
        lines += ["", "=== SIMILAR PAST FIXES (from history) ==="]
        for fix in context.similar_fixes[:2]:
            if fix.last_good_patch_json:
                lines += [
                    f"Fix for: {fix.sample_errors[:200]}",
                    f"Patch: {fix.last_good_patch_json[:400]}",
                    "",
                ]

    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────────


def run_agentic_loop(
    context: "ContextBundle",
    call_claude_with_tools: Callable,
    workspace: Path,
    allowed_tools: list[str],
    max_turns: int = _MAX_TURNS,
) -> RepairResult:
    """
    Run the agentic CI fix loop.

    The LLM is given 4 tools (read_file, write_file, run_command, finish)
    and drives the repair autonomously. ToolExecutor enforces the allowlist.

    Args:
        context:               pre-assembled ContextBundle (classifier + files + history)
        call_claude_with_tools: bound BaseAgent._call_claude_with_tools
        workspace:             absolute path to cloned repo on disk
        allowed_tools:         per-integration tool allowlist (from ci_integrations.allowed_tools)
        max_turns:             max LLM→tool→LLM cycles before giving up

    Returns:
        RepairResult (same type as repair_agent.run_repair for drop-in replacement)
    """
    executor = ToolExecutor(workspace=workspace, allowed_tools=allowed_tools)
    system_prompt = _build_system_prompt(context)

    messages: list[dict] = []
    files_written: list[str] = []
    turn = 0

    log.info("agentic_loop.start", workspace=str(workspace), max_turns=max_turns)

    while turn < max_turns:
        turn += 1
        log.info("agentic_loop.turn", turn=turn, messages_len=len(messages))

        # Build the user message for turn 1, or continue with tool results
        if not messages:
            messages = [
                {
                    "role": "user",
                    "content": (
                        "Fix the CI failure described above. "
                        "Start by reading the failing files if you need more context, "
                        "then apply your fix and validate it."
                    ),
                }
            ]

        # Call Claude with tool use
        try:
            response = call_claude_with_tools(
                messages=messages,
                tools=_TOOL_SCHEMAS,
                system=system_prompt,
                tool_choice={"type": "auto"},
            )
        except Exception as exc:
            log.warning("agentic_loop.llm_error", error=str(exc), turn=turn)
            return RepairResult(
                success=False,
                reason="llm_error",
                iteration=turn,
                state_trace=[f"llm_error:{exc}"],
            )

        # response is a dict from _call_claude: {"content": [...], "stop_reason": ...}
        content_blocks = response.get("content", [])
        stop_reason = response.get("stop_reason", "")

        # Collect tool uses from this response
        tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
        text_blocks = [b for b in content_blocks if b.get("type") == "text"]

        if text_blocks:
            text = " ".join(b.get("text", "") for b in text_blocks)
            log.info("agentic_loop.llm_text", turn=turn, text=text[:200])

        # Append assistant message to conversation
        messages.append({"role": "assistant", "content": content_blocks})

        # Check for finish tool
        finish_call = next((t for t in tool_uses if t.get("name") == "finish"), None)
        if finish_call:
            inp = finish_call.get("input", {})
            success = bool(inp.get("success", False))
            reason = str(inp.get("reason", ""))
            finish_files = inp.get("files_written", files_written)
            log.info("agentic_loop.finish", success=success, reason=reason, turn=turn)

            # Build a minimal fix_plan for downstream compatibility
            if success and finish_files:
                patches = [
                    FilePatch(path=f, start_line=1, end_line=1, corrected_lines=[], reason=reason)
                    for f in finish_files
                ]
                fix_plan = FixPlan(
                    root_cause=reason,
                    confidence="high",
                    patches=patches,
                )
                fix_plan._l1_files = finish_files  # type: ignore[attr-defined]
            else:
                fix_plan = None

            return RepairResult(
                success=success,
                fix_plan=fix_plan,
                reason="" if success else reason,
                iteration=turn,
                state_trace=[f"turn:{t}" for t in range(1, turn + 1)],
                escalate=not success,
            )

        # If no tool_use blocks and stop_reason is end_turn, LLM is done without finish
        if not tool_uses:
            log.warning("agentic_loop.no_tool_use", stop_reason=stop_reason, turn=turn)
            return RepairResult(
                success=False,
                reason="llm_did_not_call_finish",
                iteration=turn,
                state_trace=["no_tool_use"],
                escalate=True,
            )

        # Execute all non-finish tool calls
        tool_results = []
        for tool_use in tool_uses:
            if tool_use.get("name") == "finish":
                continue
            tool_name = tool_use.get("name", "")
            tool_id = tool_use.get("id", "")
            tool_input = tool_use.get("input", {})

            # Track files written
            if tool_name == "write_file":
                rel_path = tool_input.get("path", "")
                if rel_path and rel_path not in files_written:
                    files_written.append(rel_path)

            result_content = _execute_tool(tool_name, tool_input, workspace, executor)
            log.info(
                "agentic_loop.tool_result",
                tool=tool_name,
                turn=turn,
                result_preview=result_content[:100],
            )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_content,
            })

        # Append tool results as user message
        messages.append({"role": "user", "content": tool_results})

    # Max turns exceeded
    log.warning("agentic_loop.max_turns", max_turns=max_turns)
    return RepairResult(
        success=False,
        reason="max_turns_exceeded",
        iteration=max_turns,
        state_trace=["max_turns_exceeded"],
        escalate=True,
    )
