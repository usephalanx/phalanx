"""Fix-bug router — POST /v1/fix_bug.

FetchSandbox's grounded-remediation endpoint. Sibling of find_bugs: given a repo
(base64 tar of local/private code, or a git URL) and a specific bug, materialize
it on the worker and run the Claude Max subprocess with Edit/Write to AUTHOR a
fix — grounded by the brain's fix_pattern — returning a `git diff`.

The diff is a PROPOSAL. It is NOT proven here: the caller runs the honest-green
gate (buggy fails -> patched passes, measured in Phalanx) to certify it, then
hands the reviewed diff to the user's agent to apply. Same X-Probe-Token gate and
enqueue-to-worker pattern as find_bugs / run_probe.
"""
from __future__ import annotations

import asyncio
import os

import structlog
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from phalanx.ci_fixer_v3.fix_bug_task import fix_bug_task

log = structlog.get_logger(__name__)
router = APIRouter(tags=["fix_bug"])

_RESULT_WAIT_MARGIN_S = 180


def _auth(token: str | None) -> None:
    expected = os.environ.get("PHALANX_PROBE_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing probe token")


class FixBugRequest(BaseModel):
    workspace_tar_b64: str | None = Field(
        None,
        description="base64 of a .tar.gz of the repo — local / uncommitted / "
        "private code (the FetchSandbox case).",
    )
    git_url: str | None = Field(None, description="Public git URL to clone instead.")
    git_ref: str | None = Field(None, description="Optional ref/branch for git_url.")
    bug: str = Field(
        "", description="The specific bug to fix — file:line + description. Required "
        "unless a full prompt override is given."
    )
    fix_pattern: str | None = Field(
        None, description="Brain grounding: the known remediation for this failure "
        "class. Improves fix quality; the gate still certifies."
    )
    prompt: str | None = Field(
        None, description="Optional full override for the fix prompt; else built "
        "from bug + fix_pattern."
    )
    timeout_s: int = Field(300, ge=30, le=600)
    spec: str | None = Field(
        None, description="Opaque FetchSandbox brain/spec id (e.g. 'paddle'). "
        "Echoed back for attribution and NEVER interpreted by Phalanx — the "
        "brain stays on the FetchSandbox side.",
    )


class FixBugResponse(BaseModel):
    available: bool
    diff: str | None = None       # unified git diff — the proposed fix
    summary: str | None = None    # subprocess's own note on what it changed
    account: str | None = None    # which Max account served it (max1/max2)
    error: str | None = None
    grounding: dict | None = Field(
        None,
        description="Runtime-observed attribution: whether the brain's "
        "fix_pattern (or a prompt override) actually reached the subprocess. "
        "Metadata only — never the prompt text.",
    )


@router.post("/fix_bug", response_model=FixBugResponse)
async def fix_bug(
    req: FixBugRequest, x_probe_token: str | None = Header(default=None)
) -> FixBugResponse:
    """Materialize a repo and run the grounded Max subprocess to author a fix.
    Enqueued onto `cifix_sre` (ci-fixer-worker); result awaited off the loop."""
    _auth(x_probe_token)

    if not req.workspace_tar_b64 and not req.git_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "provide workspace_tar_b64 or git_url"
        )
    if not req.bug.strip() and not req.prompt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide bug or prompt")

    async_result = fix_bug_task.apply_async(
        kwargs={
            "workspace_tar_b64": req.workspace_tar_b64,
            "git_url": req.git_url,
            "git_ref": req.git_ref,
            "bug": req.bug,
            "fix_pattern": req.fix_pattern,
            "prompt": req.prompt,
            "timeout_s": req.timeout_s,
            "spec": req.spec,
        },
        queue="cifix_sre",
    )

    wait_s = req.timeout_s + _RESULT_WAIT_MARGIN_S
    try:
        result = await asyncio.to_thread(async_result.get, timeout=wait_s)
    except CeleryTimeoutError:
        log.warning("fix_bug.result_timeout", timeout_s=wait_s, task_id=async_result.id)
        return FixBugResponse(available=False, error=f"timed out after {wait_s}s waiting on worker")
    except Exception as exc:  # noqa: BLE001
        log.warning("fix_bug.dispatch_failed", error=str(exc), task_id=async_result.id)
        return FixBugResponse(available=False, error=f"dispatch failed: {type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return FixBugResponse(available=False, error="worker returned malformed result")

    return FixBugResponse(
        available=bool(result.get("available")),
        diff=result.get("diff"),
        summary=result.get("summary"),
        account=result.get("account"),
        error=result.get("error"),
        grounding=result.get("grounding"),
    )
