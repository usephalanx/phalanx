"""CI Fixer v3 — Challenger agent (adversarial reviewer).

Sits between TL and engineer dispatch. Reads TL's emitted fix_spec,
executes TL's verify_command in a fresh sandbox via `dry_run_verify`,
and emits a structured ChallengerVerdict.

Architecture rationale (see docs/v17-architecture-gaps.md):
  - Different model family from TL (Sonnet 4.6 vs TL's GPT-5.4) →
    mitigates self-enhancement bias (Panickssery 2024).
  - Clean context — Challenger gets fix_spec + ci_log + repo workspace,
    NEVER TL's chain-of-thought or prior critique rounds (Cognition's
    Devin Review pattern).
  - Default ACCEPT — only block on enumerated, evidence-backed concerns
    from a static rubric (mitigates failure mode #6 false rejection +
    #9 specification gaming on "find an objection").
  - Single pass, hard cap 4 turns — iteration plateaus past 2 rounds
    on strong base models (Smit 2024).
  - Cost cap $5/task — gives the critic budget to dry-run + investigate.

Output: ChallengerVerdict (see _v17_types.py). Loaded by commander to
decide accept (dispatch downstream) / block (re-route to TL with the
objection) / warn (dispatch but log).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any

import structlog
from sqlalchemy import select

from phalanx.agents._v17_types import (
    CHALLENGER_MAX_COST_USD,
    CHALLENGER_MODEL_DEFAULT,
)
from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.settings import get_settings
from phalanx.db.models import Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

# Side-effect: register dry_run_verify tool with the v2 registry.
from phalanx.agents import _challenger_dryrun  # noqa: F401

log = structlog.get_logger(__name__)


_CHALLENGER_TOOLS: tuple[str, ...] = (
    "read_file",
    "grep",
    "dry_run_verify",
)

# Loop bounds — Challenger is a REVIEWER, not an explorer. 6 turns is
# enough to: run dry_run_verify, read 1-2 files for evidence, emit verdict.
# Tool-call cap of 10 prevents exploration spirals.
_MAX_TURNS = 6
_MAX_TOOL_CALLS = 10


_SYSTEM_PROMPT = """You are the Challenger — an adversarial reviewer of CI-fix plans.

A different agent (the Tech Lead, "TL") just emitted a fix_spec for a CI
failure. Your job: review TL's plan, run ONE dry-run of the verify_command
to ground-truth it, and emit a structured verdict.

You are NOT a planner. You do NOT propose alternative fixes. You do NOT
write code. You only review, cite evidence, and decide accept / block / warn.

DEFAULT TO ACCEPT. Block only when you can cite SPECIFIC EVIDENCE from
the fix_spec, ci_log, or a repo file that proves a concrete problem.
Vague concerns ("this might be wrong" / "consider X") downgrade to warn,
not block. A reject without quoted evidence is treated as sycophantic
boilerplate and disregarded.

What you have:
  - TL's complete fix_spec (root_cause, affected_files, verify_command,
    verify_success, task_plan, env_requirements, error_line_quote, ...)
  - The original ci_log_text
  - Read-only access to the repo workspace
  - The dry_run_verify tool

Your investigation budget: at most 6 turns, 10 tool calls. The expected
shape is:
  Turn 1: call dry_run_verify (mandatory). May also read ONE file.
  Turn 2: emit verdict UNLESS dry_run gave ambiguous signal.
  Turns 3-5: ONLY for verifying a specific replace step's `old` text
             via read_file or grep when the dry-run alone is insufficient.

Do NOT re-investigate the bug — you're auditing TL's plan, not solving
the bug. Do NOT keep reading files looking for objections. After
dry_run_verify, decide: is there ONE specific file you must read to
back an objection? If yes, read it. If no, emit verdict.

REQUIRED FIRST STEP: Call dry_run_verify ONCE with TL's verify_command
and verify_success.exit_codes[0] (typically 0 for fix verify, but TL is
running on the BROKEN state — so you expect a NON-zero exit when verify
checks the failing path). Use the failing_command's expected exit code,
NOT verify_command's expected exit, since dry-run is on broken state.

In practice: call dry_run_verify with `verify_command=<TL's verify_command>`
and `expected_exit=<the exit you'd see on the broken state>`. For most
failures that's 1 (the test fails). Compare actual vs expected.

The dry-run interpretation is your STRONGEST evidence. Use it.

Static rubric — check each item against TL's plan:

  R1. Does verify_command actually re-trigger the failing check?
      Signal: dry_run_verify result. If exit MATCHES the broken-state
      expectation AND output mentions the same error class, R1 passes.
      If exit is 4 (no tests collected), 127 (command not found), or 0
      (verify says everything is fine on broken state), R1 FAILS — TL
      picked the wrong verify_command.

  R2. Is verify_success specific enough to prevent false-pass?
      Failure case: exit_codes=[0,1,2,3,4,5] (too permissive).
      Failure case: stdout_contains is empty for a delete-test fix
      where exit 4 is acceptable (need explicit allow-list).

  R3. Does the fix address root cause vs just the symptom?
      Look at task_plan steps. If they touch test code rather than
      source for a "production code is broken" diagnosis → symptom only.

  R4. Are step preconditions plausible?
      If a `replace` step's `old` text is suspicious (looks like TL
      paraphrased rather than quoted), call read_file to verify it's
      actually present in the target file.

  R5. Does affected_files match the actual error location?
      If error_line_quote names file X but affected_files is [Y], that's
      a mismatch — investigate.

  R6. Does env_requirements list every package verify_command depends on?
      If verify_command starts with `pytest`, `python_packages` should
      include `pytest` (or it's covered via pyproject's deps).

  R7. Does the plan avoid touching CI infrastructure?
      Any step modifying `.github/workflows/`, `tox.ini`, `noxfile.py`,
      `pre-commit-config.yaml`, `Makefile` is a P0 unless the spec is
      explicitly an env-mismatch ESCALATE shape (review_decision="ESCALATE",
      affected_files=[]).

  R8. Is confidence calibrated?
      If TL says confidence ≥ 0.9 but the dry-run interpretation is
      "EXIT MISMATCH" or "STDOUT DIFFERS", confidence is over-estimated.

When you have enough evidence, end your turn with a single fenced
```json``` code block matching this EXACT schema:

```json
{
  "verdict": "accept" | "block" | "warn",
  "objections": [
    {
      "category": "verify_command_does_not_retrigger_failure" | "verify_success_too_loose"
                  | "fix_targets_symptom_not_root_cause" | "ungrounded_step"
                  | "stale_old_text" | "affected_files_mismatch"
                  | "missing_env_dependency" | "edits_ci_infrastructure"
                  | "misdiagnosis_test_pollution" | "misdiagnosis_env_drift"
                  | "low_confidence_high_stakes" | "other",
      "severity": "P0" | "P1" | null,
      "claim": "one-sentence assertion of what's wrong",
      "evidence": "verbatim quote from fix_spec / ci_log / file content",
      "suggestion": "one-sentence hint for TL's re-plan"
    }
  ],
  "dry_run_evidence": {
    "actual_exit": <int>,
    "expected_exit": <int>,
    "exit_matches": <bool>,
    "interpretation": "<the dry_run_verify tool's interpretation field>"
  },
  "notes": "one-line summary"
}
```

Hard rules (validator will reject otherwise):
  - verdict="block" REQUIRES at least 1 objection with severity="P0" and
    non-empty evidence quoted from a real artifact.
  - verdict="warn" REQUIRES at least 1 objection.
  - verdict="accept" SHOULD have empty objections.
  - Every objection's evidence MUST be a verbatim quote, not a paraphrase.
  - dry_run_evidence MUST be present (you must have called dry_run_verify).
"""


@celery_app.task(
    name="phalanx.agents.cifix_challenger.execute_task",
    bind=True,
    queue="cifix_challenger",
    max_retries=1,
    soft_time_limit=300,
    time_limit=420,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:  # pragma: no cover
    from phalanx.ci_fixer_v3.task_lifecycle import persist_task_completion  # noqa: PLC0415

    agent = CIFixChallengerAgent(run_id=run_id, agent_id="cifix_challenger", task_id=task_id)
    result = asyncio.run(agent.execute())
    asyncio.run(persist_task_completion(task_id, result))
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixChallengerAgent(BaseAgent):
    AGENT_ROLE = "cifix_challenger"

    async def execute(self) -> AgentResult:  # pragma: no cover
        """Production entry point — loads TL output + SRE setup's workspace
        from sibling tasks, runs review, persists verdict. Used by commander
        dispatch loop in shadow mode (verdict logged but does NOT gate
        downstream dispatch).

        For unit tests, prefer `run_challenger_against(tl_output, ...)`
        which doesn't touch the DB.
        """
        async with get_db() as session:
            tl_output = await self._load_tl_output(session)
            if tl_output is None:
                # Defensive: in shadow mode, missing TL output is logged
                # but doesn't block the run. Engineer's task is already
                # next in the DAG and will execute regardless.
                self._log.warning(
                    "cifix_challenger.no_tl_output",
                    run_id=self.run_id,
                    note="shadow_mode_skip",
                )
                return AgentResult(
                    success=True,
                    output={
                        "verdict": "warn",
                        "objections": [{
                            "category": "other",
                            "severity": "P1",
                            "claim": "challenger_skipped: no upstream TL output found",
                            "evidence": f"run_id={self.run_id}",
                            "suggestion": "investigate why TL did not complete",
                        }],
                        "notes": "shadow_mode_skip",
                    },
                )
            workspace_path = await self._load_workspace_path(session)
            ci_log_text = self._extract_ci_log_from_tl_output(tl_output)
        verdict = await run_challenger_against(
            tl_output=tl_output,
            workspace_path=workspace_path or "",
            ci_log_text=ci_log_text,
            run_id=self.run_id,
        )
        # In shadow mode: log verdict prominently but always succeed so
        # advance_run dispatches the engineer regardless.
        self._log.info(
            "cifix_challenger.verdict",
            run_id=self.run_id,
            verdict=verdict.get("verdict"),
            n_objections=len(verdict.get("objections") or []),
            shadow_mode=True,
        )
        return AgentResult(
            success=True,
            output=verdict,
            tokens_used=int((verdict.get("_meta") or {}).get("tokens_used", 0)),
        )

    async def _load_tl_output(self, session) -> dict | None:  # pragma: no cover
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "cifix_techlead",
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        row = result.one_or_none()
        return row[0] if row and row[0] else None

    async def _load_workspace_path(self, session) -> str | None:  # pragma: no cover
        """Inherit workspace from the SRE setup task's output (mirrors
        how the TL agent does it). Falls back to None if SRE setup
        hasn't completed (shouldn't happen in normal DAG order)."""
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role.in_(
                    ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                ),
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.asc())
        )
        for (output,) in result.all():
            if isinstance(output, dict) and output.get("mode") == "setup":
                return output.get("workspace_path")
        return None

    def _extract_ci_log_from_tl_output(self, tl_output: dict) -> str:  # pragma: no cover
        """TL doesn't store ci_log_text directly in its output — but the
        error_line_quote field gives us the most-relevant excerpt, and
        the failing_command + root_cause give surrounding context. For
        Challenger's c1/c7-equivalent reasoning, this is enough.

        If we later need full ci_log, commander could attach it to the
        Challenger task's description before dispatch.
        """
        parts: list[str] = []
        if quote := tl_output.get("error_line_quote"):
            parts.append(f"Error line (verbatim from CI log):\n{quote}")
        if rc := tl_output.get("root_cause"):
            parts.append(f"TL root_cause:\n{rc}")
        if fc := tl_output.get("failing_command"):
            parts.append(f"TL failing_command: {fc}")
        return "\n\n".join(parts)


# ─── Public entrypoint usable from tests ──────────────────────────────────────


async def run_challenger_against(
    *,
    tl_output: dict,
    workspace_path: str,
    ci_log_text: str,
    run_id: str = "challenger-test",
    cache_dir: str | None = None,
    force: bool = False,
) -> dict:
    """Run the Challenger LLM loop against a TL output and return the
    parsed ChallengerVerdict dict (with _meta block appended).

    If `cache_dir` is set, caches verdicts by hash of (tl_output +
    workspace_files + ci_log_text). Re-runs are free. Pass `force=True`
    to bust the cache.

    Decoupled from the DB / Celery so tier-1 tests can drive it directly
    against corpus fixtures.
    """
    if cache_dir and not force:
        cached = _load_cached_verdict(cache_dir, tl_output, workspace_path, ci_log_text, run_id)
        if cached is not None:
            cached.setdefault("_meta", {})["from_cache"] = True
            return cached

    log.info("v3.challenger.run.start", run_id=run_id, has_workspace=bool(workspace_path))

    # Build a minimal AgentContext — Challenger's tools (read_file, grep,
    # dry_run_verify) only need workspace_path + ci_log fields.
    ctx = _build_challenger_context(
        run_id=run_id,
        workspace_path=workspace_path,
        ci_log_text=ci_log_text,
    )
    # Seed first user message with TL's output + ci_log
    initial_message = _build_initial_message(tl_output, ci_log_text)
    ctx.messages.append({"role": "user", "content": initial_message})

    llm_call = _build_challenger_llm(_CHALLENGER_TOOLS)

    try:
        verdict, turns_used, tool_calls_used = await _run_review_loop(
            ctx=ctx,
            llm_call=llm_call,
            max_turns=_MAX_TURNS,
            max_tool_calls=_MAX_TOOL_CALLS,
        )
    except _ReviewError as exc:
        log.warning("v3.challenger.review_failed", kind=exc.kind, detail=exc.detail)
        return {
            "verdict": "warn",
            "objections": [
                {
                    "category": "other",
                    "severity": "P1",
                    "claim": f"challenger_internal: {exc.kind}",
                    "evidence": (exc.detail or "")[:300],
                    "suggestion": "review challenger logs",
                }
            ],
            "dry_run_evidence": None,
            "notes": "challenger errored; defaulting to warn",
            "_meta": {"error": exc.kind, "detail": exc.detail},
        }

    verdict["_meta"] = {
        "model": CHALLENGER_MODEL_DEFAULT,
        "turns_used": turns_used,
        "tool_calls_used": tool_calls_used,
        "tokens_used": _tokens_used_from_ctx(ctx),
    }
    if cache_dir:
        _write_cache_verdict(cache_dir, tl_output, workspace_path, ci_log_text, run_id, verdict)
    return verdict


def _verdict_cache_key(tl_output: dict, workspace_path: str, ci_log_text: str, run_id: str) -> str:
    """Stable hash of inputs — re-cache only when something material changes.

    Includes run_id to keep good-plan vs bad-plan-mutation runs distinct
    even when the rest matches.
    """
    import hashlib
    h = hashlib.sha256()
    h.update(run_id.encode())
    h.update(json.dumps(tl_output, sort_keys=True, default=str).encode())
    h.update(ci_log_text.encode())
    # Hash workspace contents (deterministic order)
    if workspace_path:
        from pathlib import Path
        ws = Path(workspace_path)
        if ws.is_dir():
            for path in sorted(ws.rglob("*")):
                if path.is_file() and ".git" not in path.parts:
                    try:
                        h.update(str(path.relative_to(ws)).encode())
                        h.update(path.read_bytes())
                    except (OSError, ValueError):
                        pass
    return h.hexdigest()[:16]


def _load_cached_verdict(
    cache_dir: str, tl_output: dict, workspace_path: str, ci_log_text: str, run_id: str
) -> dict | None:
    from pathlib import Path
    key = _verdict_cache_key(tl_output, workspace_path, ci_log_text, run_id)
    cache_path = Path(cache_dir) / f"{run_id}.{key}.json"
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text())
    except Exception:  # noqa: BLE001
        return None


def _write_cache_verdict(
    cache_dir: str, tl_output: dict, workspace_path: str,
    ci_log_text: str, run_id: str, verdict: dict
) -> None:
    from pathlib import Path
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    key = _verdict_cache_key(tl_output, workspace_path, ci_log_text, run_id)
    cache_path = Path(cache_dir) / f"{run_id}.{key}.json"
    cache_path.write_text(json.dumps(verdict, indent=2, default=str))


# ─── Loop ────────────────────────────────────────────────────────────────────


class _ReviewError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


async def _run_review_loop(
    ctx,
    llm_call,
    max_turns: int,
    max_tool_calls: int,
) -> tuple[dict, int, int]:
    from phalanx.ci_fixer_v2.tools import base as tools_base

    total_tool_calls = 0
    for turn in range(max_turns):
        log.info("v3.challenger.turn_start", turn=turn, messages=len(ctx.messages))
        response = await llm_call(ctx.messages)
        log.info(
            "v3.challenger.turn_response",
            turn=turn,
            stop_reason=response.stop_reason,
            tools=[u.name for u in response.tool_uses] if response.tool_uses else [],
        )

        from phalanx.ci_fixer_v2.agent import _assistant_message_content
        ctx.messages.append({"role": "assistant", "content": _assistant_message_content(response)})

        if response.stop_reason == "end_turn" and not response.tool_uses:
            verdict = _parse_verdict_from_text(response.text or "")
            if verdict is None:
                raise _ReviewError(
                    "no_verdict_emitted",
                    f"text_tail={(response.text or '')[:600]!r}",
                )
            return (verdict, turn + 1, total_tool_calls)

        for use in response.tool_uses or []:
            total_tool_calls += 1
            if total_tool_calls > max_tool_calls:
                raise _ReviewError("tool_call_cap", f"max={max_tool_calls}")
            if use.name not in _CHALLENGER_TOOLS:
                raise _ReviewError("forbidden_tool", use.name)
            if not tools_base.is_registered(use.name):
                raise _ReviewError("unregistered_tool", use.name)
            tool = tools_base.get(use.name)
            try:
                result = await tool.handler(ctx, use.input)
            except Exception as exc:  # noqa: BLE001
                result = tools_base.ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            ctx.messages.append(_tool_result_message(use.id, result))

    raise _ReviewError("turn_cap_reached", f"max={max_turns}")


def _tool_result_message(tool_use_id: str, result) -> dict:
    """Anthropic-shaped tool_result message.

    Unlike OpenAI which accepts an object in tool_result.content, Anthropic
    requires a string OR a list of content blocks. We JSON-stringify the
    structured ToolResult dict so the wire format is correct.
    """
    content_dict = result.to_tool_message_content()
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(content_dict, default=str),
            }
        ],
    }


# ─── Verdict parsing + validation ────────────────────────────────────────────


_VALID_VERDICTS = {"accept", "block", "warn"}
_VALID_CATEGORIES = {
    "verify_command_does_not_retrigger_failure",
    "verify_success_too_loose",
    "fix_targets_symptom_not_root_cause",
    "ungrounded_step",
    "stale_old_text",
    "affected_files_mismatch",
    "missing_env_dependency",
    "edits_ci_infrastructure",
    "misdiagnosis_test_pollution",
    "misdiagnosis_env_drift",
    "low_confidence_high_stakes",
    "other",
}
_VALID_SEVERITIES = {"P0", "P1", None}

_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_UNLABELED_FENCE_RE = re.compile(r"```\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_verdict_from_text(text: str) -> dict | None:
    """Extract the structured ChallengerVerdict JSON from the LLM's final
    turn. Mirrors TL parsing pattern but with a stricter shape gate.
    """
    if not text:
        return None
    candidates: list[dict] = []
    for match in _JSON_FENCE_RE.finditer(text):
        with contextlib.suppress(json.JSONDecodeError):
            candidates.append(json.loads(match.group(1)))
    for match in _UNLABELED_FENCE_RE.finditer(text):
        with contextlib.suppress(json.JSONDecodeError):
            candidates.append(json.loads(match.group(1)))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        with contextlib.suppress(json.JSONDecodeError):
            candidates.append(json.loads(stripped))

    for obj in reversed(candidates):
        if not isinstance(obj, dict):
            continue
        if obj.get("verdict") not in _VALID_VERDICTS:
            continue
        # Normalize objections — drop malformed entries; keep valid ones
        objs = obj.get("objections") or []
        if not isinstance(objs, list):
            objs = []
        cleaned: list[dict] = []
        for o in objs:
            if not isinstance(o, dict):
                continue
            if o.get("category") not in _VALID_CATEGORIES:
                continue
            if o.get("severity") not in _VALID_SEVERITIES:
                continue
            if not isinstance(o.get("claim"), str) or not o["claim"]:
                continue
            if not isinstance(o.get("evidence"), str) or not o["evidence"]:
                continue
            cleaned.append({
                "category": o["category"],
                "severity": o.get("severity"),
                "claim": o["claim"],
                "evidence": o["evidence"],
                "suggestion": o.get("suggestion") or "",
            })
        obj["objections"] = cleaned

        # Hard rule: block requires ≥1 P0
        if obj["verdict"] == "block":
            if not any(o.get("severity") == "P0" for o in cleaned):
                # Downgrade to warn — block without P0 evidence is sycophantic
                obj["verdict"] = "warn"
        # Hard rule: warn requires ≥1 objection
        if obj["verdict"] == "warn" and not cleaned:
            obj["verdict"] = "accept"
        return obj
    return None


# ─── Initial message + LLM build ─────────────────────────────────────────────


def _build_initial_message(tl_output: dict, ci_log_text: str) -> str:
    """Compact framing of TL's plan + ci_log for the Challenger."""
    # Strip TL's _meta if present (chain-of-thought leakage avoidance)
    tl_view = {k: v for k, v in tl_output.items() if not k.startswith("_")}
    tl_json = json.dumps(tl_view, indent=2)[:6000]
    log_tail = ci_log_text[-3000:] if ci_log_text else "(none provided)"
    return (
        "Review this Tech Lead fix_spec.\n\n"
        "=== TL fix_spec (verbatim JSON) ===\n"
        f"{tl_json}\n\n"
        "=== ci_log_text (tail, up to 3000 chars) ===\n"
        f"{log_tail}\n\n"
        "Start by calling dry_run_verify with TL's verify_command. "
        "Use expected_exit=1 unless you have specific reason to expect otherwise. "
        "Then emit your final ```json``` verdict block per the schema."
    )


def _build_challenger_context(run_id: str, workspace_path: str | None, ci_log_text: str):
    from phalanx.ci_fixer_v2.context import AgentContext
    return AgentContext(
        ci_fix_run_id=f"v3-{run_id}-challenger",
        repo_full_name="challenger-review",
        repo_workspace_path=workspace_path or "",
        original_failing_command="",
        pr_number=None,
        has_write_permission=False,
        ci_api_key=None,
        ci_provider="github_actions",
        author_head_branch=None,
        sandbox_container_id=None,
    )


def _build_challenger_llm(tool_names: tuple[str, ...]):
    """Bind a Sonnet 4.6 callable for the Challenger.

    Imports here are lazy so the module loads without heavy deps until
    actually invoked.
    """
    import phalanx.ci_fixer_v2.tools.diagnosis  # noqa: F401
    import phalanx.ci_fixer_v2.tools.reading  # noqa: F401
    from phalanx.ci_fixer_v2.providers.anthropic_sonnet import build_sonnet_coder_callable
    from phalanx.ci_fixer_v2.tools import base as tools_base

    schemas = [tools_base.get(name).schema for name in tool_names]
    settings = get_settings()
    # Reuse the Sonnet provider with our system prompt
    return build_sonnet_coder_callable(
        model=getattr(settings, "anthropic_model_default", CHALLENGER_MODEL_DEFAULT),
        api_key=getattr(settings, "anthropic_api_key", "") or "",
        system_prompt=_SYSTEM_PROMPT,
        tool_schemas=schemas,
    )


def _tokens_used_from_ctx(ctx) -> int:
    cost = getattr(ctx, "cost", None)
    if cost is None:
        return 0
    for attr in ("total_tokens", "input_tokens"):
        val = getattr(cost, attr, None)
        if isinstance(val, int):
            return val
    return 0


__all__ = [
    "CIFixChallengerAgent",
    "execute_task",
    "run_challenger_against",
    "CHALLENGER_MAX_COST_USD",
]
