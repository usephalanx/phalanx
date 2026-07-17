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
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import structlog
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from phalanx.ci_fixer_v3.env_detector import detect_env
from phalanx.ci_fixer_v3.provisioner import (
    _exec_in_container,
    provision_on_the_fly,
    stop_sandbox,
)

log = structlog.get_logger(__name__)
router = APIRouter(tags=["run_probe"])

_MAX_TAR_BYTES = 64 * 1024 * 1024  # 64 MB unpacked-source cap


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
    error: str | None = None


def _auth(token: str | None) -> None:
    expected = os.environ.get("PHALANX_PROBE_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing probe token")


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract with path-traversal guard (no absolute paths / .. escapes)."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsafe path in tar: {member.name}")
    tf.extractall(dest)


def _materialize(req: RunProbeRequest, dest: Path) -> None:
    if req.workspace_tar_b64:
        raw = base64.b64decode(req.workspace_tar_b64)
        if len(raw) > _MAX_TAR_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "tar too large")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            _safe_extract(tf, dest)
    elif req.git_url:
        args = ["git", "clone", "--depth", "1"]
        if req.git_ref:
            args += ["--branch", req.git_ref]
        subprocess.run([*args, req.git_url, str(dest)], check=True, timeout=120)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide workspace_tar_b64 or git_url")


@router.post("/run_probe", response_model=RunProbeResponse)
async def run_probe(
    req: RunProbeRequest, x_probe_token: str | None = Header(default=None)
) -> RunProbeResponse:
    """Unpack/clone a repo, boot a throwaway sandbox, run ONE probe, return its
    real exit code. The caller runs this twice — buggy code (reproduce) then
    fixed code (verify) — and compares exit codes."""
    _auth(x_probe_token)
    with tempfile.TemporaryDirectory(prefix="fs_probe_") as tmp:
        ws = Path(tmp)
        _materialize(req, ws)
        env_spec = detect_env(ws)
        if req.setup_cmds is not None:
            env_spec.install_commands = req.setup_cmds

        sb = await provision_on_the_fly(ws, env_spec)
        if not sb.available or not sb.container_id:
            log.warning("run_probe.provision_failed", error=sb.error)
            return RunProbeResponse(
                available=False, exit_code=-1, setup_log=sb.setup_log, error=sb.error
            )
        try:
            r = await _exec_in_container(
                sb.container_id, req.probe_cmd, workdir="/workspace", timeout_s=req.timeout_s
            )
            return RunProbeResponse(
                available=True,
                exit_code=r.exit_code,
                stdout=r.stdout_tail,
                stderr=r.stderr_tail,
                setup_log=sb.setup_log,
            )
        finally:
            await stop_sandbox(sb.container_id)
