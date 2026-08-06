"""Synthesize-probe router — POST /v1/synthesize_probe.

FetchSandbox's generative-prove endpoint: given a repo (buggy) + a bug, run the
Max subprocess to AUTHOR a self-contained behavioral probe that reproduces the
bug on the real code, self-validated (must exit 1 on the buggy code) before it's
returned. Used when no curated scenario matches. Mirrors find_bugs / run_probe:
X-Probe-Token gate, enqueue to the ci-fixer-worker (which carries claude + Max
tokens). See phalanx.ci_fixer_v3.synthesize_probe_task.
"""
from __future__ import annotations

import asyncio
import os

import structlog
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from phalanx.ci_fixer_v3.synthesize_probe_task import synthesize_probe_task

log = structlog.get_logger(__name__)
router = APIRouter(tags=["synthesize_probe"])

_RESULT_WAIT_MARGIN_S = 240


def _auth(token: str | None) -> None:
    expected = os.environ.get("PHALANX_PROBE_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing probe token")


class SynthesizeProbeRequest(BaseModel):
    workspace_tar_b64: str | None = Field(None, description="base64 .tar.gz of the buggy repo.")
    git_url: str | None = Field(None, description="Public git URL to clone instead.")
    git_ref: str | None = Field(None, description="Optional ref/branch for git_url.")
    bug: str = Field("", description="The bug to author a probe for (file:line + description).")
    timeout_s: int = Field(600, ge=60, le=900)


class SynthesizeProbeResponse(BaseModel):
    available: bool
    probe_src: str | None = None
    probe_file: str | None = None
    probe_cmd: str | None = None
    setup_cmds: list[str] | None = None
    lang: str | None = None
    buggy_exit: int | None = None
    summary: str | None = None
    account: str | None = None
    error: str | None = None


@router.post("/synthesize_probe", response_model=SynthesizeProbeResponse)
async def synthesize_probe(
    req: SynthesizeProbeRequest, x_probe_token: str | None = Header(default=None)
) -> SynthesizeProbeResponse:
    """Author + self-validate a probe for `bug`. available=True only if it
    reproduced (exit 1) on the buggy code. Enqueued onto `cifix_sre`."""
    _auth(x_probe_token)
    if not req.workspace_tar_b64 and not req.git_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide workspace_tar_b64 or git_url")
    if not req.bug.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide bug")

    async_result = synthesize_probe_task.apply_async(
        kwargs={
            "workspace_tar_b64": req.workspace_tar_b64,
            "git_url": req.git_url,
            "git_ref": req.git_ref,
            "bug": req.bug,
            "timeout_s": req.timeout_s,
        },
        queue="cifix_sre",
    )
    wait_s = req.timeout_s + _RESULT_WAIT_MARGIN_S
    try:
        result = await asyncio.to_thread(async_result.get, timeout=wait_s)
    except CeleryTimeoutError:
        log.warning("synthesize_probe.result_timeout", timeout_s=wait_s, task_id=async_result.id)
        return SynthesizeProbeResponse(available=False, error=f"timed out after {wait_s}s")
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesize_probe.dispatch_failed", error=str(exc), task_id=async_result.id)
        return SynthesizeProbeResponse(available=False, error=f"dispatch failed: {type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return SynthesizeProbeResponse(available=False, error="worker returned malformed result")
    return SynthesizeProbeResponse(
        available=bool(result.get("available")),
        probe_src=result.get("probe_src"),
        probe_file=result.get("probe_file"),
        probe_cmd=result.get("probe_cmd"),
        setup_cmds=result.get("setup_cmds"),
        lang=result.get("lang"),
        buggy_exit=result.get("buggy_exit"),
        summary=result.get("summary"),
        account=result.get("account"),
        error=result.get("error"),
    )
