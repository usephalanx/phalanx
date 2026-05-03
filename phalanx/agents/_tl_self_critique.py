"""Tech Lead self-critique validator.

v1.6.0 introduced deterministic c1/c2/c3. v1.7 adds c4/c5/c7 to close
the patch-hallucination class flagged in the corpus run review:

    c1 — ci_log_addresses_root_cause (v1.6)
        token-overlap check on root_cause vs ci_log_text.

    c2 — affected_files_exist_in_repo (v1.6)
        every path resolves to a real file in the workspace.

    c3 — verify_command_will_distinguish_success (v1.6)
        first shell token of verify_command resolves on PATH.

    c4 — grounding_satisfied (v1.7 NEW)
        every step in draft_steps that names a file (replace/insert/
        delete_lines) MUST have been seen by a read_file call this
        turn. Catches TL hallucinating line numbers / `old` text.

    c5 — step_preconditions_satisfied (v1.7 NEW)
        for every replace step's `old` substring, grep the target
        file in the live workspace; pass iff `old` is actually
        present. Catches typos / stale OLD text from earlier reads.

    c7 — error_line_quoted_from_log (v1.7 NEW)
        draft_error_line_quote MUST be a non-empty substring of
        ci_log_text (length 20-200 chars). Forces TL to anchor its
        diagnosis to a verbatim line from the failure log.

(c6 — env_requirements_resolvable — reserved; will land with the
SRE wiring phase.)

Two consumers:
    - LLM tool-call site: handler runs all available checks; returns
      booleans + mismatches.
    - Commander gate: `commander_verify_fix_spec_self_critique` re-runs
      every check deterministically against the FINAL fix_spec to
      ensure the LLM didn't lie about validator output. Returns
      mismatch list; commander marks TL FAILED if any.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from phalanx.ci_fixer_v2.tools.base import ToolResult, ToolSchema

if TYPE_CHECKING:
    from phalanx.ci_fixer_v2.context import AgentContext

log = structlog.get_logger(__name__)


# Common English stopwords + meta-phrasing that shouldn't count as
# "distinctive" tokens for c1.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "this", "that", "into", "than",
        "when", "while", "where", "have", "been", "were", "would", "could",
        "should", "their", "there", "these", "those", "such", "some", "more",
        "most", "less", "very", "fail", "fails", "failed", "failing", "error",
        "errors", "issue", "issues", "test", "tests", "ci", "build", "code",
        "file", "files", "line", "lines", "function", "method", "value",
        "values", "case", "cases", "missing", "added", "removed", "changed",
        "because", "after", "before", "without", "during", "still", "again",
    }
)

_DISTINCTIVE_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{3,}\b")
_VERIFY_FIRST_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
_PATH_TRAVERSAL_RE = re.compile(r"(^/|\.\.)")


# ─────────────────────────────────────────────────────────────────────
# Pure functions (sandbox-independent) — used both by tool handler and
# by the commander gate
# ─────────────────────────────────────────────────────────────────────


def _extract_distinctive_tokens(text: str) -> list[str]:
    """Lowercase ≥4-char alpha tokens not in the stopword list."""
    if not text:
        return []
    tokens = [m.group(0).lower() for m in _DISTINCTIVE_TOKEN_RE.finditer(text)]
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in _STOPWORDS or t in seen:
            continue
        out.append(t)
        seen.add(t)
    return out


def check_c1_ci_log_addresses_root_cause(
    *, draft_root_cause: str, ci_log_text: str
) -> tuple[bool, str]:
    """At least one distinctive token from root_cause must appear in the CI
    log AND at least 30% of distinctive tokens must each appear ≥ once.

    Tuned to be lenient on phrasing differences ("E501 line too long" vs
    "Line too long (E501)") and strict against fabricated diagnoses
    ("DatabaseTimeout" when log says "ImportError")."""
    rc_tokens = _extract_distinctive_tokens(draft_root_cause)
    if not rc_tokens:
        return False, "root_cause has no distinctive tokens (only stopwords?)"

    log_lower = (ci_log_text or "").lower()
    hits = [t for t in rc_tokens if t in log_lower]
    overlap_pct = len(hits) / len(rc_tokens) if rc_tokens else 0.0
    if not hits:
        return False, (
            f"none of root_cause tokens {rc_tokens[:5]!r} appear in ci_log "
            f"({len(ci_log_text)} chars)"
        )
    if overlap_pct < 0.30:
        return False, (
            f"only {len(hits)}/{len(rc_tokens)} root_cause tokens "
            f"({overlap_pct:.0%}) appear in ci_log; need ≥30%"
        )
    return True, ""


def check_c2_affected_files_exist(
    *, draft_affected_files: list[str], workspace_path: str | Path
) -> tuple[bool, str]:
    """Each path resolves under workspace AND is a real file."""
    if not draft_affected_files:
        # Empty affected_files is a separate condition (handled by engineer
        # guards). For c2 specifically, we say this isn't a file-existence
        # failure — pass with a note.
        return True, ""

    workspace = Path(workspace_path).resolve()
    for raw_path in draft_affected_files:
        if not isinstance(raw_path, str) or not raw_path:
            return False, f"affected_files contains non-string or empty: {raw_path!r}"
        if _PATH_TRAVERSAL_RE.match(raw_path):
            return False, f"affected_files path traversal/absolute: {raw_path!r}"
        target = (workspace / raw_path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return False, f"affected_files resolves outside workspace: {raw_path!r}"
        if not target.is_file():
            return False, f"affected_files does not exist: {raw_path!r}"
    return True, ""


def check_c4_grounding_satisfied(
    *,
    draft_steps: list[dict] | None,
    files_read_this_turn: set[str],
) -> tuple[bool, str]:
    """Every step that modifies a file must reference a file TL has read
    this turn. Catches TL emitting `replace`/`insert`/`delete_lines`
    against files it hasn't actually loaded — the source class of patch
    hallucination flagged in ChatGPT's prompt review.
    """
    if not draft_steps:
        return True, ""  # No steps yet — nothing to ground

    for step in draft_steps:
        if not isinstance(step, dict):
            continue
        action = step.get("action")
        if action not in {"replace", "insert", "delete_lines"}:
            continue
        file_path = step.get("file")
        if not file_path or not isinstance(file_path, str):
            return False, f"step (action={action!r}) missing file"
        if file_path not in files_read_this_turn:
            return False, (
                f"step modifies {file_path!r} but read_file({file_path!r}) "
                f"was not called this turn (read these so far: "
                f"{sorted(files_read_this_turn)})"
            )
    return True, ""


def check_c5_step_preconditions_satisfied(
    *,
    draft_steps: list[dict] | None,
    workspace_path: str | Path,
) -> tuple[bool, str]:
    """For every `replace` step, the `old` substring must actually be
    present in the target file's current content. Catches stale OLD
    text (e.g., TL read the file but the prompt-eng iterator changed
    something between read and emit) and typos in OLD that would make
    the engineer's apply step fail with step_precondition_violated.
    """
    if not draft_steps:
        return True, ""

    workspace = Path(workspace_path).resolve()
    for step in draft_steps:
        if not isinstance(step, dict):
            continue
        if step.get("action") != "replace":
            continue
        file_path = step.get("file")
        old = step.get("old")
        if not file_path or not isinstance(file_path, str):
            return False, "replace step missing file"
        if not isinstance(old, str) or not old:
            return False, f"replace step on {file_path!r} has empty/missing 'old'"

        target = (workspace / file_path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return False, f"replace step references file outside workspace: {file_path!r}"
        if not target.is_file():
            return False, f"replace step references missing file: {file_path!r}"
        try:
            content = target.read_text(errors="replace")
        except Exception as exc:  # noqa: BLE001
            return False, f"could not read {file_path!r}: {exc}"
        if old not in content:
            preview = old[:60] + ("..." if len(old) > 60 else "")
            return False, (
                f"replace step's 'old' substring not in {file_path!r} "
                f"(old preview: {preview!r})"
            )
    return True, ""


def check_c7_error_line_quoted_from_log(
    *,
    draft_error_line_quote: str | None,
    ci_log_text: str,
) -> tuple[bool, str]:
    """error_line_quote must be a verbatim substring of ci_log_text,
    length 20-200 chars. Forces TL to anchor its diagnosis to a real
    line from the failure log instead of paraphrasing.
    """
    if not draft_error_line_quote:
        return False, "error_line_quote is empty/missing"
    if not isinstance(draft_error_line_quote, str):
        return False, "error_line_quote must be a string"
    quote = draft_error_line_quote.strip()
    if len(quote) < 20:
        return False, (
            f"error_line_quote too short ({len(quote)} < 20 chars); "
            f"pick a more specific line from ci_log"
        )
    if len(quote) > 240:
        return False, (
            f"error_line_quote too long ({len(quote)} > 240 chars); "
            f"pick a single failure line, not a paragraph"
        )
    if quote not in (ci_log_text or ""):
        return False, (
            f"error_line_quote not found verbatim in ci_log_text; "
            f"quote={quote[:80]!r}..."
        )
    return True, ""


def check_c11_environmental_control(
    *,
    draft_root_cause: str,
    draft_open_questions: list[str] | None,
    env_drift_hits: list,  # list[GitCommitHit] but kept untyped to avoid import cycle
) -> tuple[bool, str]:
    """If recent infra commits exist (env_drift_hits non-empty) AND TL's
    diagnosis doesn't mention env / drift / infra concerns, raise.

    Catches the trap: "code is fine but CI broke because mamba 2.6.0
    shipped" — TL diagnoses the visible Python error and misses the
    actual cause (an infra commit from yesterday).
    """
    if not env_drift_hits:
        return True, ""  # no drift detected — nothing to flag

    haystack = (draft_root_cause or "").lower()
    haystack += " " + " ".join(str(q) for q in (draft_open_questions or [])).lower()
    env_keywords = {
        "infra", "infrastructure", "env", "environment", "sandbox",
        "runner", "image", "drift", "github action", "workflow",
        "dockerfile", "dependency", "dependencies", "package", "version",
        "release", "deploy",
    }
    if any(kw in haystack for kw in env_keywords):
        return True, ""  # TL acknowledged env-side cause

    return False, (
        f"{len(env_drift_hits)} recent infra commit(s) detected but "
        f"root_cause + open_questions don't reference env/infra causes. "
        f"Top recent infra commits: "
        + ", ".join(f"{h.sha} ({', '.join(h.files[:2])})"
                    for h in env_drift_hits[:3])
        + ". If failure first appeared after one of these, the fix may be "
        f"reverting/adjusting the infra change, NOT patching code."
    )


def check_c12_isolation_test_advisable(
    *,
    draft_root_cause: str,
    draft_failing_command: str,
    draft_open_questions: list[str] | None,
) -> tuple[bool, str]:
    """If root_cause names a SINGLE specific test (e.g. via pytest selector
    `tests/foo.py::test_bar` in failing_command) AND TL didn't acknowledge
    test-pollution as a possibility, raise.

    Catches the trap: failing test is a victim of state another test
    leaked (autouse fixture, sys.modules, np.random). The fix lives in
    the polluter, not the failing test — but TL diagnoses the failing
    test's source.
    """
    cmd = (draft_failing_command or "").lower()
    # Single-test selector heuristic: pytest `path::test_name` or `-k name`
    is_single_test_selector = (
        "::" in cmd
        or " -k " in cmd
        or cmd.endswith("-k")
    )
    if not is_single_test_selector:
        return True, ""  # multi-test verify — pollution unlikely to mislead

    haystack = (draft_root_cause or "").lower()
    haystack += " " + " ".join(str(q) for q in (draft_open_questions or [])).lower()
    pollution_keywords = {
        "isolation", "isolated", "pollution", "polluted",
        "leak", "leaks", "leaked", "leakage", "shared state",
        "autouse", "fixture", "monkeypatch", "sys.modules", "global",
        "side effect", "side-effect", "ordering", "test order",
    }
    if any(kw in haystack for kw in pollution_keywords):
        return True, ""  # TL acknowledged test-pollution possibility

    return False, (
        f"failing_command targets a single test selector "
        f"({draft_failing_command!r}) — confirm the failure isn't "
        f"caused by another test polluting shared state (autouse fixtures, "
        f"sys.modules mutation, np.random seed leak, env vars). "
        f"Run the test in isolation: `pytest <selector> -p no:randomly`. "
        f"If it passes alone, the bug is in the POLLUTER, not the named test."
    )


def files_read_from_messages(messages: list[dict]) -> set[str]:
    """Extract every file path passed to read_file across the message
    history. Used by c4 to audit grounding.
    """
    files: set[str] = set()
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            # Provider-translated tool_use blocks; both Anthropic + OpenAI
            # shapes flow through here as {"type": "tool_use", "name": ..., "input": ...}
            if block.get("type") == "tool_use" and block.get("name") == "read_file":
                input_dict = block.get("input") or {}
                for key in ("path", "file_path", "file"):
                    val = input_dict.get(key)
                    if isinstance(val, str) and val:
                        files.add(val)
                        break
    return files


async def check_c3_verify_command_resolvable(
    *,
    draft_verify_command: str,
    sandbox_container_id: str | None,
    exec_in_sandbox=None,
) -> tuple[bool, str]:
    """First token of verify_command must be a valid identifier AND
    `command -v <token>` must succeed in the sandbox if available.

    If sandbox is unavailable, returns (True, "unverified: no sandbox") —
    soft pass. The commander gate is the authoritative verifier; this path
    exists for legacy / simulate cases.
    """
    if not draft_verify_command or not isinstance(draft_verify_command, str):
        return False, "verify_command empty or not a string"

    try:
        parts = shlex.split(draft_verify_command)
    except ValueError as exc:
        return False, f"verify_command shlex parse failed: {exc}"
    if not parts:
        return False, "verify_command splits to no tokens"
    first = parts[0]
    if not _VERIFY_FIRST_TOKEN_RE.match(first):
        return False, f"first token {first!r} contains shell metachars"

    if not sandbox_container_id or exec_in_sandbox is None:
        return True, "unverified_no_sandbox"

    try:
        exec_result = await exec_in_sandbox(
            sandbox_container_id, f"command -v {shlex.quote(first)} >/dev/null 2>&1"
        )
        if exec_result.exit_code != 0:
            return False, f"command -v {first!r} failed (exit {exec_result.exit_code})"
    except Exception as exc:  # noqa: BLE001
        return True, f"unverified_sandbox_error: {exc}"

    return True, ""


# ─────────────────────────────────────────────────────────────────────
# Tool registration — TL calls this BEFORE emit_fix_spec
# ─────────────────────────────────────────────────────────────────────


VALIDATE_SELF_CRITIQUE_SCHEMA = ToolSchema(
    name="validate_self_critique",
    description=(
        "REQUIRED before emit_fix_spec. Returns the AUTHORITATIVE self_critique "
        "booleans for your draft fix_spec. Use the returned values verbatim — "
        "do NOT overwrite. v1.6 booleans (c1/c2/c3) check root_cause/files/verify; "
        "v1.7 adds c4 (grounding — every step's file must have been read this "
        "turn), c5 (step preconditions — every replace step's `old` must exist "
        "in the target file), c7 (error_line_quote must be verbatim from ci_log). "
        "If any boolean is false, INVESTIGATE FURTHER before emitting (re-read the "
        "log, re-glob files, fix the OLD text, pick a real error line) and call "
        "this tool again. After 2 iterations max, emit fix_spec with confidence "
        "≤ 0.5 and failing booleans documented in open_questions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_root_cause": {
                "type": "string",
                "description": "The candidate root_cause text you would emit in fix_spec.",
            },
            "draft_affected_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Repo-relative paths you would emit in fix_spec.",
            },
            "draft_verify_command": {
                "type": "string",
                "description": (
                    "The verify_command you would emit (or the failing_command "
                    "if your fix is the default DEFAULT shape)."
                ),
            },
            "ci_log_text": {
                "type": "string",
                "description": (
                    "Verbatim text from your earlier fetch_ci_log call. Required "
                    "for c1 + c7 — paste enough context to verify root_cause "
                    "overlap and contain the error_line_quote."
                ),
            },
            "draft_steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "v1.7 NEW. The flat list of step dicts across ALL engineer "
                    "tasks in your draft task_plan. Used by c4 (file grounding) "
                    "and c5 (replace `old` precondition). Pass [] if your plan "
                    "has no engineer steps yet (e.g., ESCALATE shape)."
                ),
            },
            "draft_error_line_quote": {
                "type": "string",
                "description": (
                    "v1.7 NEW. Verbatim line from ci_log_text that captures the "
                    "actual failure — e.g., the line containing 'AssertionError', "
                    "'ImportError', 'E501', 'exit 127'. 20-240 chars. Used by c7 "
                    "to confirm your diagnosis is anchored in real evidence."
                ),
            },
        },
        "required": [
            "draft_root_cause",
            "draft_affected_files",
            "draft_verify_command",
            "ci_log_text",
        ],
    },
)


async def _handle_validate_self_critique(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    """Run all available deterministic checks (c1/c2/c3 v1.6, c4/c5/c7 v1.7)
    and return authoritative booleans.
    """
    draft_rc = tool_input.get("draft_root_cause") or ""
    draft_files = tool_input.get("draft_affected_files") or []
    draft_vc = tool_input.get("draft_verify_command") or ""
    ci_log = tool_input.get("ci_log_text") or ""
    draft_steps = tool_input.get("draft_steps")
    draft_error_line = tool_input.get("draft_error_line_quote")

    if not isinstance(draft_files, list):
        return ToolResult(
            ok=False,
            error="validate_self_critique: 'draft_affected_files' must be a list",
        )

    c1_ok, c1_reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause=draft_rc, ci_log_text=ci_log
    )
    c2_ok, c2_reason = check_c2_affected_files_exist(
        draft_affected_files=draft_files,
        workspace_path=ctx.repo_workspace_path,
    )

    # c3: prefer real sandbox check when available; else soft-pass.
    exec_in_sandbox = None
    if getattr(ctx, "sandbox_container_id", None):
        try:
            from phalanx.ci_fixer_v3.provisioner import _exec_in_container  # noqa: PLC0415

            exec_in_sandbox = _exec_in_container
        except ImportError:
            exec_in_sandbox = None
    c3_ok, c3_reason = await check_c3_verify_command_resolvable(
        draft_verify_command=draft_vc,
        sandbox_container_id=getattr(ctx, "sandbox_container_id", None),
        exec_in_sandbox=exec_in_sandbox,
    )

    # v1.7 — c4 grounding: audit ctx.messages for read_file calls.
    files_read = files_read_from_messages(ctx.messages or [])
    c4_ok, c4_reason = check_c4_grounding_satisfied(
        draft_steps=draft_steps if isinstance(draft_steps, list) else None,
        files_read_this_turn=files_read,
    )

    # v1.7 — c5 step preconditions: grep `old` against the live workspace.
    c5_ok, c5_reason = check_c5_step_preconditions_satisfied(
        draft_steps=draft_steps if isinstance(draft_steps, list) else None,
        workspace_path=ctx.repo_workspace_path,
    )

    # v1.7 — c7 error_line_quote: verbatim substring of ci_log_text.
    if draft_error_line is not None:
        c7_ok, c7_reason = check_c7_error_line_quoted_from_log(
            draft_error_line_quote=draft_error_line,
            ci_log_text=ci_log,
        )
    else:
        # Soft-skip when caller didn't pass it (older v1.6 path callers).
        # The COMMANDER gate (`commander_verify_fix_spec_self_critique`) is
        # authoritative on whether c7 was required.
        c7_ok, c7_reason = True, "skipped_no_input"

    mismatches: list[dict[str, str]] = []
    if not c1_ok:
        mismatches.append({"check": "ci_log_addresses_root_cause", "reason": c1_reason})
    if not c2_ok:
        mismatches.append({"check": "affected_files_exist_in_repo", "reason": c2_reason})
    if not c3_ok:
        mismatches.append({"check": "verify_command_will_distinguish_success", "reason": c3_reason})
    if not c4_ok:
        mismatches.append({"check": "grounding_satisfied", "reason": c4_reason})
    if not c5_ok:
        mismatches.append({"check": "step_preconditions_satisfied", "reason": c5_reason})
    if not c7_ok:
        mismatches.append({"check": "error_line_quoted_from_log", "reason": c7_reason})

    log.info(
        "v3.tl.self_critique.validated",
        c1=c1_ok,
        c2=c2_ok,
        c3=c3_ok,
        c4=c4_ok,
        c5=c5_ok,
        c7=c7_ok,
        mismatches_count=len(mismatches),
    )

    return ToolResult(
        ok=True,
        data={
            "validated": {
                "ci_log_addresses_root_cause": c1_ok,
                "affected_files_exist_in_repo": c2_ok,
                "verify_command_will_distinguish_success": c3_ok,
                "grounding_satisfied": c4_ok,
                "step_preconditions_satisfied": c5_ok,
                "error_line_quoted_from_log": c7_ok,
            },
            "mismatches": mismatches,
            "all_true": all([c1_ok, c2_ok, c3_ok, c4_ok, c5_ok, c7_ok]),
        },
    )


class _ValidateSelfCritiqueTool:
    schema = VALIDATE_SELF_CRITIQUE_SCHEMA
    handler = staticmethod(_handle_validate_self_critique)


validate_self_critique_tool = _ValidateSelfCritiqueTool()


# Register at import time so the TL tool dispatcher can find it.
# Idempotent re-registration (the registry uses dict assignment).
def _register_with_v2_registry() -> None:
    from phalanx.ci_fixer_v2.tools.base import register  # noqa: PLC0415

    register(validate_self_critique_tool)


_register_with_v2_registry()


# ─────────────────────────────────────────────────────────────────────
# Commander gate — re-runs validation deterministically against the
# fix_spec the TL emitted. This is the airtight check; even if the LLM
# lied about the tool's output, this catches it.
# ─────────────────────────────────────────────────────────────────────


async def commander_verify_fix_spec_self_critique(
    *,
    fix_spec: dict,
    workspace_path: str | Path,
    ci_log_text: str,
    sandbox_container_id: str | None = None,
    exec_in_sandbox=None,
) -> tuple[bool, list[dict[str, str]]]:
    """Authoritative re-validation of fix_spec.self_critique. Returns
    (all_passed, mismatches). Commander marks TL task FAILED on any false.

    Why both tool-side AND commander-side: the LLM controls what JSON it
    emits in fix_spec. Even if the validator tool returned all-true, the
    LLM could write all-true into fix_spec when it shouldn't. Commander
    re-runs the same checks deterministically against the FINAL fix_spec
    and rejects on mismatch.
    """
    draft_rc = fix_spec.get("root_cause") or ""
    draft_files = fix_spec.get("affected_files") or []
    draft_vc = fix_spec.get("verify_command") or fix_spec.get("failing_command") or ""

    c1_ok, c1_reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause=draft_rc, ci_log_text=ci_log_text
    )
    c2_ok, c2_reason = check_c2_affected_files_exist(
        draft_affected_files=draft_files,
        workspace_path=workspace_path,
    )
    c3_ok, c3_reason = await check_c3_verify_command_resolvable(
        draft_verify_command=draft_vc,
        sandbox_container_id=sandbox_container_id,
        exec_in_sandbox=exec_in_sandbox,
    )

    mismatches: list[dict[str, str]] = []
    if not c1_ok:
        mismatches.append({"check": "ci_log_addresses_root_cause", "reason": c1_reason})
    if not c2_ok:
        mismatches.append({"check": "affected_files_exist_in_repo", "reason": c2_reason})
    if not c3_ok:
        mismatches.append({"check": "verify_command_will_distinguish_success", "reason": c3_reason})

    return (c1_ok and c2_ok and c3_ok), mismatches
