"""
Find-bugs router — POST /v1/find_bugs.

FetchSandbox's discovery endpoint: given a repo (base64 tar of local/private
code, or a git URL), materialize it on the worker and run the Claude Max
subprocess to DISCOVER bugs by reading the code. Returns the findings.

Additive only — this file + one include_router() line in api/main.py. Mirrors
run_probe.py: same X-Probe-Token gate, same enqueue-to-worker pattern (the API
container has no claude CLI / Max tokens — those live on the ci-fixer-worker).
Unlike run_probe it does NOT provision a Docker container; it only reads source
+ runs the subprocess. See phalanx.ci_fixer_v3.find_bugs_task.
"""
from __future__ import annotations

import asyncio
import os

import structlog
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from phalanx.ci_fixer_v3.find_bugs_task import find_bugs_task

log = structlog.get_logger(__name__)
router = APIRouter(tags=["find_bugs"])

# The subprocess (materialize + a full Max analysis pass) can take minutes;
# budget beyond the caller's timeout for queue latency before giving up.
_RESULT_WAIT_MARGIN_S = 180


def _auth(token: str | None) -> None:
    expected = os.environ.get("PHALANX_PROBE_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing probe token")


class FindBugsRequest(BaseModel):
    workspace_tar_b64: str | None = Field(
        None,
        description="base64 of a .tar.gz of the repo — local / uncommitted / "
        "private code (the FetchSandbox case).",
    )
    git_url: str | None = Field(None, description="Public git URL to clone instead.")
    git_ref: str | None = Field(None, description="Optional ref/branch for git_url.")
    prompt: str | None = Field(
        None, description="Optional override for the analysis prompt; else the "
        "default production-bug audit prompt is used."
    )
    timeout_s: int = Field(300, ge=30, le=600)


class FindBugsResponse(BaseModel):
    available: bool
    bugs: str | None = None
    account: str | None = None  # which Max account served it (max1/max2)
    error: str | None = None


@router.post("/find_bugs", response_model=FindBugsResponse)
async def find_bugs(
    req: FindBugsRequest, x_probe_token: str | None = Header(default=None)
) -> FindBugsResponse:
    """Materialize a repo and run the Max subprocess to discover bugs. Enqueued
    onto the `cifix_sre` queue (ci-fixer-worker, which carries claude + tokens);
    result awaited off the event loop and mapped onto FindBugsResponse."""
    _auth(x_probe_token)

    if not req.workspace_tar_b64 and not req.git_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "provide workspace_tar_b64 or git_url"
        )

    async_result = find_bugs_task.apply_async(
        kwargs={
            "workspace_tar_b64": req.workspace_tar_b64,
            "git_url": req.git_url,
            "git_ref": req.git_ref,
            "prompt": req.prompt,
            "timeout_s": req.timeout_s,
        },
        queue="cifix_sre",
    )

    wait_s = req.timeout_s + _RESULT_WAIT_MARGIN_S
    try:
        result = await asyncio.to_thread(async_result.get, timeout=wait_s)
    except CeleryTimeoutError:
        log.warning("find_bugs.result_timeout", timeout_s=wait_s, task_id=async_result.id)
        return FindBugsResponse(available=False, error=f"timed out after {wait_s}s waiting on worker")
    except Exception as exc:  # noqa: BLE001
        log.warning("find_bugs.dispatch_failed", error=str(exc), task_id=async_result.id)
        return FindBugsResponse(available=False, error=f"dispatch failed: {type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return FindBugsResponse(available=False, error="worker returned malformed result")

    return FindBugsResponse(
        available=bool(result.get("available")),
        bugs=result.get("bugs"),
        account=result.get("account"),
        error=result.get("error"),
    )
