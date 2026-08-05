"""
Run-probe router — POST /v1/run_probe.

Exposes the decoupled on-the-fly provisioner (ci_fixer_v3/provisioner.py) as a
clean "run this repo with this probe" API. Added for FetchSandbox, which calls
it to prove a fix against the user's REAL app: buggy code = reproduce, fixed
code = verify.

Additive only — this file + one include_router() line in api/main.py. Touches
no existing Phalanx logic.

Security: this clones/unpacks code and runs an arbitrary command, so it is a
remote-code-execution surface. It is gated by a shared secret (X-Probe-Token,
from env PHALANX_PROBE_TOKEN) so only a trusted caller may invoke it, and the
command runs inside the provisioner's throwaway, resource-capped Docker
container — never on the host.

Docker-socket boundary: the phalanx-api container has NO Docker socket — by
Phalanx's audited design the socket is scoped ONLY to the socket-having workers
(phalanx-ci-fixer-worker, phalanx-sre-worker). So this endpoint does NOT
provision inline. It enqueues phalanx.ci_fixer_v3.run_probe_task.run_probe_task
onto the `cifix_sre` queue (consumed by phalanx-ci-fixer-worker, which has the
socket), then awaits the result and maps it onto RunProbeResponse.
"""
from __future__ import annotations

import asyncio
import os

import structlog
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from phalanx.ci_fixer_v3.run_probe_task import run_probe_task

log = structlog.get_logger(__name__)
router = APIRouter(tags=["run_probe"])

# Extra wall-clock budget beyond the caller's probe timeout to cover
# materialize + provision (docker run, apt/pip installs) on the worker,
# plus broker/queue latency, before we give up waiting on the result.
_RESULT_WAIT_MARGIN_S = 300


class RunProbeRequest(BaseModel):
    workspace_tar_b64: str | None = Field(
        None,
        description="base64 of a .tar.gz of the repo — works for local / "
        "uncommitted code (the FetchSandbox demo case).",
    )
    git_url: str | None = Field(None, description="Public git URL to clone instead.")
    git_ref: str | None = Field(None, description="Optional ref/branch for git_url.")
    probe_cmd: str = Field(
        ..., description="Shell command whose EXIT CODE is the probe result "
        "(0 = fix holds / no failure; non-zero = failure reproduced)."
    )
    setup_cmds: list[str] | None = Field(
        None, description="Override install commands; else detect_env() infers them."
    )
    timeout_s: int = Field(120, ge=1, le=600)


class RunProbeResponse(BaseModel):
    available: bool
    exit_code: int
    stdout: str | None = None
    stderr: str | None = None
    setup_log: list[dict] = Field(default_factory=list)
    # The probe's captured request/response evidence (FS_PROOF_JSON), if it
    # emitted any. One entry per call the probe fired at the real handler.
    proof_events: list[dict] | None = None
    error: str | None = None


def _auth(token: str | None) -> None:
    expected = os.environ.get("PHALANX_PROBE_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing probe token")


@router.post("/run_probe", response_model=RunProbeResponse)
async def run_probe(
    req: RunProbeRequest, x_probe_token: str | None = Header(default=None)
) -> RunProbeResponse:
    """Unpack/clone a repo, boot a throwaway sandbox, run ONE probe, return its
    real exit code. The caller runs this twice — buggy code (reproduce) then
    fixed code (verify) — and compares exit codes.

    The API container has no Docker socket, so the actual provisioning is
    dispatched to the socket-having phalanx-ci-fixer-worker via the
    `cifix_sre` queue. We enqueue the task, await its result synchronously
    (off the event loop), and map the returned dict onto RunProbeResponse.
    """
    _auth(x_probe_token)

    # Cheap up-front validation so obviously-bad requests fail fast with a
    # 400 instead of round-tripping through the worker.
    if not req.workspace_tar_b64 and not req.git_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "provide workspace_tar_b64 or git_url"
        )

    async_result = run_probe_task.apply_async(
        kwargs={
            "workspace_tar_b64": req.workspace_tar_b64,
            "git_url": req.git_url,
            "git_ref": req.git_ref,
            "probe_cmd": req.probe_cmd,
            "setup_cmds": req.setup_cmds,
            "timeout_s": req.timeout_s,
        },
        queue="cifix_sre",  # socket-having worker (phalanx-ci-fixer-worker)
    )

    wait_s = req.timeout_s + _RESULT_WAIT_MARGIN_S
    try:
        # AsyncResult.get() blocks; run it in a thread so we don't stall the
        # event loop while the worker provisions + runs the probe.
        result = await asyncio.to_thread(async_result.get, timeout=wait_s)
    except CeleryTimeoutError:
        log.warning("run_probe.result_timeout", timeout_s=wait_s, task_id=async_result.id)
        return RunProbeResponse(
            available=False,
            exit_code=-1,
            error=f"probe timed out after {wait_s}s waiting on worker",
        )
    except Exception as exc:  # noqa: BLE001 — worker crash / broker error
        log.warning("run_probe.dispatch_failed", error=str(exc), task_id=async_result.id)
        return RunProbeResponse(
            available=False,
            exit_code=-1,
            error=f"probe dispatch failed: {type(exc).__name__}: {exc}",
        )

    if not isinstance(result, dict):
        return RunProbeResponse(
            available=False, exit_code=-1, error="worker returned malformed result"
        )

    return RunProbeResponse(
        available=bool(result.get("available")),
        exit_code=int(result.get("exit_code", -1)),
        stdout=result.get("stdout"),
        stderr=result.get("stderr"),
        setup_log=result.get("setup_log") or [],
        proof_events=result.get("proof_events"),
        error=result.get("error"),
    )
