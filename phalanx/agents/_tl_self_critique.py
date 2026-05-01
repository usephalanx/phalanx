"""Tech Lead self-critique validator (Phase 1, v1.6.0).

Replaces v1.5.0's prompt-driven self_critique with deterministic tool-side
validation. The LLM can no longer fake the booleans — they're computed
from real observable evidence (ci_log text overlap, file system state,
sandbox command resolution).

Three checks, one per boolean:

    c1 — ci_log_addresses_root_cause:
        extract distinctive ≥4-char tokens from draft_root_cause (drop
        common stopwords); count how many appear in supplied ci_log_text;
        pass iff ≥1 hit AND ≥30% of distinct tokens appear at least once.

    c2 — affected_files_exist_in_repo:
        for each path in draft_affected_files, resolve under
        ctx.repo_workspace_path; reject path traversal; require .is_file().
        Pass iff EVERY path exists.

    c3 — verify_command_will_distinguish_success:
        extract first shell token of draft_verify_command; require it
        matches `^[A-Za-z0-9._-]+$`. If sandbox available, run
        `command -v <token>` and pass iff exit 0. If sandbox unavailable,
        mark as "unverified" (treated as soft-pass for backwards compat
        on non-v3 paths).

Two consumers:
    - LLM tool-call site: handler runs all three; returns booleans + mismatches.
    - Commander gate: `verify_self_critique` re-runs all three deterministically
      against the FINAL fix_spec to ensure the LLM didn't lie about validator
      output. Returns mismatch list; commander marks TL FAILED if any.
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
        "do NOT overwrite. The booleans are computed deterministically from the "
        "CI log you fetched, the workspace file system, and the sandbox. If any "
        "boolean is false, INVESTIGATE FURTHER before emitting (re-read the log, "
        "re-glob files, etc.) and call this tool again. After 2 iterations max, "
        "if you still cannot achieve all-true, emit fix_spec with confidence ≤ 0.5 "
        "and the failing booleans documented in open_questions."
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
                    "for c1 — paste enough context to verify root_cause overlap."
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
    """Run the three deterministic checks and return authoritative booleans."""
    draft_rc = tool_input.get("draft_root_cause") or ""
    draft_files = tool_input.get("draft_affected_files") or []
    draft_vc = tool_input.get("draft_verify_command") or ""
    ci_log = tool_input.get("ci_log_text") or ""

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

    mismatches: list[dict[str, str]] = []
    if not c1_ok:
        mismatches.append({"check": "ci_log_addresses_root_cause", "reason": c1_reason})
    if not c2_ok:
        mismatches.append({"check": "affected_files_exist_in_repo", "reason": c2_reason})
    if not c3_ok:
        mismatches.append({"check": "verify_command_will_distinguish_success", "reason": c3_reason})

    log.info(
        "v3.tl.self_critique.validated",
        c1=c1_ok,
        c2=c2_ok,
        c3=c3_ok,
        mismatches_count=len(mismatches),
    )

    return ToolResult(
        ok=True,
        data={
            "validated": {
                "ci_log_addresses_root_cause": c1_ok,
                "affected_files_exist_in_repo": c2_ok,
                "verify_command_will_distinguish_success": c3_ok,
            },
            "mismatches": mismatches,
            "all_true": c1_ok and c2_ok and c3_ok,
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
