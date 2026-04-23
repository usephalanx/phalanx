"""CI Fixer v3 — Tech Lead agent (investigator).

Phase 1 implementation. GPT-5.4 with read-only diagnosis tools.

Role:
  - Reads the parent Run's ci_context (failing command, job name, repo, PR).
  - Uses GPT-5.4 to diagnose root cause. Tools are read-only — it can:
      fetch_ci_log, get_pr_context, get_pr_diff, get_ci_history, git_blame,
      query_fingerprint, read_file, glob, grep.
  - Does NOT write code. Does NOT run sandbox. Does NOT commit.
  - Final turn emits a JSON fix_spec block; we parse it and write to
    Task.output so the Engineer (next task in the DAG) can read it.

Output shape (written to tasks.output):
  {
    "root_cause": str,              # one-sentence diagnosis
    "affected_files": [str],        # repo-relative paths to edit
    "fix_spec": str,                # natural-language change description
    "confidence": float,            # 0.0 .. 1.0
    "open_questions": [str],        # unknowns left for the Engineer
    "model": "gpt-5.4",
    "turns_used": int,
    "tool_calls_used": int,
  }

Invariants:
  - Single Celery task invocation; no internal retry loop beyond max_turns.
  - Reuses v2 tool implementations + providers. No copy-paste.
  - Zero changes to v2 or build flow.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import structlog
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.settings import get_settings
from phalanx.db.models import CIIntegration, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


# Tool subset visible to the Tech Lead. Strict subset of v2's MAIN_AGENT_TOOL_NAMES
# — read-only + diagnosis. Anything NOT listed here is unreachable via the LLM.
_TECHLEAD_TOOLS: tuple[str, ...] = (
    "fetch_ci_log",
    "get_pr_context",
    "get_pr_diff",
    "get_ci_history",
    "git_blame",
    "query_fingerprint",
    "read_file",
    "glob",
    "grep",
)

_MAX_TURNS = 8
_MAX_TOOL_CALLS = 15  # hard upper bound across all turns

_SYSTEM_PROMPT = """You are a Senior Tech Lead investigating a failing CI build.

You have READ-ONLY tools. You do NOT write code. You do NOT run sandbox commands.
You do NOT commit. Your only job is to produce a precise fix specification that
a different engineer will implement in the next step.

Workflow you MUST follow:
  1. Call `fetch_ci_log` first with the provided job_id to see the actual failure.
  2. Use `get_pr_diff` to see what this PR changed.
  3. Read the affected file(s) with `read_file` or confirm authorship with
     `git_blame`. Use `get_ci_history` / `query_fingerprint` only if you suspect
     a known recurring failure.
  4. Do NOT loop — each tool should only be called once unless new information
     requires a follow-up read.

When you have enough evidence, end your turn with a single markdown fenced
`json` code block containing EXACTLY this shape:

```json
{
  "root_cause": "one-sentence diagnosis of why CI failed",
  "affected_files": ["repo-relative/path.py"],
  "fix_spec": "natural-language description of the minimum edit required",
  "confidence": 0.0,
  "open_questions": ["any unknowns the engineer should be aware of"]
}
```

Confidence 0.0-1.0. Be honest — if the fix is unclear, confidence < 0.5 and
list open_questions. The engineer will escalate rather than guess.
"""


@celery_app.task(
    name="phalanx.agents.cifix_techlead.execute_task",
    bind=True,
    queue="cifix_techlead",
    max_retries=1,
    soft_time_limit=600,
    time_limit=720,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:  # pragma: no cover
    agent = CIFixTechLeadAgent(run_id=run_id, agent_id="cifix_techlead", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixTechLeadAgent(BaseAgent):
    AGENT_ROLE = "cifix_techlead"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_techlead.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False, output={}, error=f"Task {self.task_id} not found"
                )
            # Tech Lead reads ci_context from its own Task.description (seeded by
            # cifix_commander when persisting the DAG).
            ci_context = _parse_ci_context(task.description)
            integration = await self._load_integration(session, ci_context.get("repo"))

        # Missing must-have fields → fast fail
        missing = _missing_required(ci_context)
        if missing:
            err = f"ci_context missing required fields: {missing}"
            self._log.error("cifix_techlead.bad_context", missing=missing)
            return AgentResult(success=False, output={}, error=err)

        # Clone workspace (reuses v2 logic via _clone_workspace helper)
        try:
            workspace_path = await _clone_workspace(
                run_id=self.run_id,
                repo_full_name=ci_context["repo"],
                branch=ci_context["branch"],
                github_token=_resolve_github_token(integration),
            )
        except Exception as exc:
            self._log.exception("cifix_techlead.clone_failed", error=str(exc))
            return AgentResult(
                success=False, output={}, error=f"workspace clone failed: {exc}"
            )

        # Build an AgentContext reused from v2 — Tech Lead's tools don't need sandbox.
        ctx = _build_techlead_context(
            run_id=self.run_id,
            ci_context=ci_context,
            workspace_path=workspace_path,
            integration=integration,
        )

        # Build GPT-5.4 LLM callable with TL-only tool schemas.
        llm_call = _build_techlead_llm(tool_names=_TECHLEAD_TOOLS)

        # Seed the first user message with the normalized CI context.
        initial_message = _build_initial_message(ci_context)
        ctx.messages.append({"role": "user", "content": initial_message})

        # Run the investigation loop.
        try:
            fix_spec, turns_used, tool_calls_used = await _run_investigation_loop(
                ctx=ctx,
                llm_call=llm_call,
                max_turns=_MAX_TURNS,
                max_tool_calls=_MAX_TOOL_CALLS,
                logger=self._log,
            )
        except _InvestigationFailure as exc:
            return AgentResult(
                success=False,
                output={"error_class": exc.kind, "detail": exc.detail},
                error=f"{exc.kind}: {exc.detail}",
                tokens_used=ctx.cost.total_tokens if hasattr(ctx.cost, "total_tokens") else 0,
            )

        self._log.info(
            "cifix_techlead.done",
            confidence=fix_spec.get("confidence"),
            affected_files=fix_spec.get("affected_files"),
            turns=turns_used,
            tool_calls=tool_calls_used,
        )
        return AgentResult(
            success=True,
            output={
                **fix_spec,
                "model": "gpt-5.4",
                "turns_used": turns_used,
                "tool_calls_used": tool_calls_used,
            },
            tokens_used=_tokens_used_from_ctx(ctx),
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Investigation loop — self-contained, reuses v2 tool dispatch + providers.
# Kept as module-level functions (not methods) so unit tests can inject fakes.
# ─────────────────────────────────────────────────────────────────────────────


class _InvestigationFailure(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


async def _run_investigation_loop(
    ctx,
    llm_call,
    max_turns: int,
    max_tool_calls: int,
    logger,
) -> tuple[dict, int, int]:
    """Core loop: drive the LLM until it emits a fix_spec JSON block."""
    # Lazy imports so the module loads without heavy deps until actually invoked.
    from phalanx.ci_fixer_v2.tools import base as tools_base  # noqa: PLC0415

    total_tool_calls = 0
    for turn in range(max_turns):
        logger.info("cifix_techlead.turn_start", turn=turn, messages=len(ctx.messages))
        response = await llm_call(ctx.messages)
        logger.info(
            "cifix_techlead.turn_response",
            turn=turn,
            stop_reason=response.stop_reason,
            tools=[u.name for u in response.tool_uses] if response.tool_uses else [],
        )

        # Record the assistant's turn in the history for the next round.
        from phalanx.ci_fixer_v2.agent import _assistant_message_content  # noqa: PLC0415

        ctx.messages.append(
            {"role": "assistant", "content": _assistant_message_content(response)}
        )

        if response.stop_reason == "end_turn" and not response.tool_uses:
            # Model thinks it's done. Parse the text for a JSON fix_spec.
            fix_spec = _parse_fix_spec_from_text(response.text or "")
            if fix_spec is None:
                raise _InvestigationFailure(
                    "no_fix_spec_emitted",
                    "LLM stopped without a valid JSON fix_spec block",
                )
            return (fix_spec, turn + 1, total_tool_calls)

        # Dispatch each tool_use; append tool_result messages for the next turn.
        for use in response.tool_uses or []:
            total_tool_calls += 1
            if total_tool_calls > max_tool_calls:
                raise _InvestigationFailure(
                    "tool_call_cap",
                    f"Tech Lead exceeded {max_tool_calls} tool calls without a fix_spec",
                )
            if use.name not in _TECHLEAD_TOOLS:
                # Shouldn't happen — LLM only sees TL tools — but belt and braces.
                raise _InvestigationFailure(
                    "forbidden_tool", f"Tech Lead tried to call {use.name!r}"
                )
            if not tools_base.is_registered(use.name):
                raise _InvestigationFailure(
                    "unregistered_tool", f"Tool {use.name!r} not in registry"
                )
            tool = tools_base.get(use.name)
            try:
                result = await tool.handler(ctx, use.input)
            except Exception as exc:
                result = tools_base.ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "cifix_techlead.tool_error",
                    tool=use.name,
                    error=str(exc),
                )
            ctx.messages.append(_tool_result_message(use.id, result))

    raise _InvestigationFailure(
        "turn_cap_reached",
        f"Tech Lead exhausted {max_turns} turns without a fix_spec",
    )


def _tool_result_message(tool_use_id: str, result) -> dict:
    """Shape the v2 providers expect for tool_result slots."""
    return {
        "role": "tool",
        "tool_use_id": tool_use_id,
        "content": json.dumps(result.to_tool_message_content()),
    }


_FIX_SPEC_REQUIRED_KEYS = {
    "root_cause",
    "affected_files",
    "fix_spec",
    "confidence",
    "open_questions",
}

_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_fix_spec_from_text(text: str) -> dict | None:
    """Extract the first fenced ```json``` block and validate required keys.

    Returns the parsed dict on success, None on any failure — the caller
    converts None to an investigation failure.
    """
    if not text:
        return None
    match = _JSON_FENCE_RE.search(text)
    if match is None:
        # Fallback: maybe the model emitted bare JSON. Try a best-effort parse.
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return None
        else:
            return None
    else:
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    if not isinstance(obj, dict):
        return None
    if not _FIX_SPEC_REQUIRED_KEYS.issubset(obj.keys()):
        return None
    # Shallow type sanity
    if not isinstance(obj.get("affected_files"), list):
        return None
    if not isinstance(obj.get("open_questions"), list):
        return None
    try:
        obj["confidence"] = float(obj["confidence"])
    except (TypeError, ValueError):
        return None
    return obj


def _build_techlead_context(
    run_id: str,
    ci_context: dict,
    workspace_path: str,
    integration: CIIntegration | None,
):
    from phalanx.ci_fixer_v2.context import AgentContext  # noqa: PLC0415

    return AgentContext(
        ci_fix_run_id=f"v3-{run_id}",
        repo_full_name=ci_context["repo"],
        repo_workspace_path=workspace_path,
        original_failing_command=ci_context.get("failing_command", ""),
        pr_number=ci_context.get("pr_number"),
        has_write_permission=False,  # Tech Lead cannot write — enforced by tool scope
        ci_api_key=_resolve_github_token(integration),
        ci_provider=(integration.ci_provider if integration else "github_actions"),
        author_head_branch=ci_context.get("branch"),
        sandbox_container_id=None,  # Tech Lead has no sandbox — its tools don't need one
    )


def _build_techlead_llm(tool_names: tuple[str, ...]):
    from phalanx.ci_fixer_v2.providers import build_gpt_reasoning_callable  # noqa: PLC0415
    from phalanx.ci_fixer_v2.tools import base as tools_base  # noqa: PLC0415

    # Ensure v2 tools are imported so the registry is populated.
    import phalanx.ci_fixer_v2.tools.diagnosis  # noqa: F401, PLC0415
    import phalanx.ci_fixer_v2.tools.reading  # noqa: F401, PLC0415

    schemas = [tools_base.get(name).schema for name in tool_names]

    settings = get_settings()
    return build_gpt_reasoning_callable(
        model=settings.openai_model_reasoning_ci_fixer,  # "gpt-5.4" in prod
        api_key=settings.openai_api_key,
        system_prompt=_SYSTEM_PROMPT,
        tool_schemas=schemas,
        reasoning_effort="medium",
    )


async def _clone_workspace(
    run_id: str, repo_full_name: str, branch: str, github_token: str | None
) -> str:
    """Shallow clone at the PR head branch. Returns absolute workspace path."""
    if not github_token:
        raise RuntimeError("no github token available for clone")
    import git  # noqa: PLC0415

    base = Path(get_settings().git_workspace) / f"v3-{run_id}-techlead"
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


def _missing_required(ci_context: dict) -> list[str]:
    required = ("repo", "branch", "failing_command", "failing_job_id", "pr_number")
    return [k for k in required if not ci_context.get(k)]


def _build_initial_message(ci_context: dict) -> str:
    # Compact, structured — no markdown noise. GPT-5.4 reads this once.
    return (
        "CI failure to investigate:\n"
        f"- repo: {ci_context.get('repo')}\n"
        f"- pr: #{ci_context.get('pr_number')} on branch {ci_context.get('branch')!r}\n"
        f"- failing_job: {ci_context.get('failing_job_name')} "
        f"(job_id={ci_context.get('failing_job_id')})\n"
        f"- failing_command: {ci_context.get('failing_command')}\n"
        f"- head_sha: {ci_context.get('sha')}\n\n"
        "Start with fetch_ci_log to see the raw failure. "
        "End your turn with the JSON fix_spec block as described."
    )


def _tokens_used_from_ctx(ctx) -> int:
    """Best-effort token accounting so the framework's telemetry has a number."""
    cost = getattr(ctx, "cost", None)
    if cost is None:
        return 0
    for attr in ("total_tokens", "input_tokens", "output_tokens"):
        val = getattr(cost, attr, None)
        if isinstance(val, int):
            return val
    return 0
