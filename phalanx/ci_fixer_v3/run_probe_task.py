"""CI Fixer v3 — run-probe Celery task.

The POST /v1/run_probe API endpoint (api/routes/run_probe.py) must NOT touch
Docker directly: by Phalanx's audited security design the Docker socket is
scoped ONLY to the socket-having workers (phalanx-ci-fixer-worker,
phalanx-sre-worker), never the API container. See the docker-compose comment
"Docker socket privilege is scoped to CI Fixer only, not every agent."

So the API enqueues THIS task, which runs on the socket-having
phalanx-ci-fixer-worker (it consumes the `cifix_sre` queue). This is where the
actual provisioning happens: materialize the workspace (untar base64 or git
clone) ON THE WORKER, detect_env → provision_on_the_fly → _exec_in_container →
stop_sandbox, and hand back a plain JSON-serializable dict.

The task NEVER raises — every error is captured into the returned dict's
`error` field so the API can map it cleanly onto RunProbeResponse.
"""
from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

import structlog

from phalanx.ci_fixer_v3.env_detector import detect_env
from phalanx.ci_fixer_v3.provisioner import (
    _exec_in_container,
    provision_on_the_fly,
    stop_sandbox,
)
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

_MAX_TAR_BYTES = 64 * 1024 * 1024  # 64 MB unpacked-source cap


def _extract_proof(stdout: str | None) -> list | None:
    """Pull the probe's `FS_PROOF_JSON=<json-array>` evidence line out of stdout.

    The synthesized probe records the real request/response of every call it
    fires and prints them as a single final line. We parse the LAST such line
    (a probe may print progress before the final block). Returns the list, or
    None if absent/malformed — the receipt then degrades to the exit-code
    summary (no crash, no fabrication)."""
    if not stdout:
        return None
    marker = "FS_PROOF_JSON="
    idx = stdout.rfind(marker)
    if idx == -1:
        return None
    line = stdout[idx + len(marker):].splitlines()[0].strip()
    try:
        import json as _json
        val = _json.loads(line)
        return val if isinstance(val, list) else None
    except Exception:  # noqa: BLE001 — evidence is best-effort, never blocks the verdict
        return None


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract with a path-traversal guard (no absolute paths / .. escapes)."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            raise ValueError(f"unsafe path in tar: {member.name}")
    tf.extractall(dest)


def _materialize(
    *,
    dest: Path,
    workspace_tar_b64: str | None,
    git_url: str | None,
    git_ref: str | None,
) -> None:
    """Materialize the caller's repo into `dest` ON THE WORKER.

    Either untar a base64 .tar.gz (local / uncommitted code — the
    FetchSandbox demo case) or shallow-clone a public git URL. Raises a
    plain exception on bad input; the task wrapper captures it into `error`.
    """
    if workspace_tar_b64:
        raw = base64.b64decode(workspace_tar_b64)
        if len(raw) > _MAX_TAR_BYTES:
            raise ValueError("tar too large")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            _safe_extract(tf, dest)
    elif git_url:
        args = ["git", "clone", "--depth", "1"]
        if git_ref:
            args += ["--branch", git_ref]
        subprocess.run([*args, git_url, str(dest)], check=True, timeout=120)
    else:
        raise ValueError("provide workspace_tar_b64 or git_url")


async def _run_probe_async(
    *,
    workspace_tar_b64: str | None,
    git_url: str | None,
    git_ref: str | None,
    probe_cmd: str,
    setup_cmds: list[str] | None,
    timeout_s: int,
) -> dict:
    """Full provisioning + probe, on the socket-having worker.

    Returns a plain dict: {available, exit_code, stdout, stderr, setup_log,
    error}. Never raises.
    """
    with tempfile.TemporaryDirectory(prefix="fs_probe_") as tmp:
        ws = Path(tmp)
        _materialize(
            dest=ws,
            workspace_tar_b64=workspace_tar_b64,
            git_url=git_url,
            git_ref=git_ref,
        )
        env_spec = detect_env(ws)
        if setup_cmds is not None:
            env_spec.install_commands = setup_cmds

        sb = await provision_on_the_fly(ws, env_spec)
        if not sb.available or not sb.container_id:
            log.warning("run_probe_task.provision_failed", error=sb.error)
            return {
                "available": False,
                "exit_code": -1,
                "stdout": None,
                "stderr": None,
                "setup_log": sb.setup_log,
                "error": sb.error,
            }
        try:
            r = await _exec_in_container(
                sb.container_id,
                probe_cmd,
                workdir="/workspace",
                timeout_s=timeout_s,
                stdout_cap=24000,  # wide enough for the FS_PROOF_JSON evidence line
            )
            return {
                "available": True,
                "exit_code": r.exit_code,
                "stdout": r.stdout_tail,
                "stderr": r.stderr_tail,
                "setup_log": sb.setup_log,
                "proof_events": _extract_proof(r.stdout_tail),
                "error": None,
            }
        finally:
            await stop_sandbox(sb.container_id)


@celery_app.task(
    name="phalanx.ci_fixer_v3.run_probe_task.run_probe_task",
    queue="cifix_sre",  # consumed by phalanx-ci-fixer-worker (has the Docker socket)
    max_retries=0,
    soft_time_limit=900,
    time_limit=960,
)
def run_probe_task(
    workspace_tar_b64: str | None = None,
    git_url: str | None = None,
    git_ref: str | None = None,
    probe_cmd: str = "",
    setup_cmds: list[str] | None = None,
    timeout_s: int = 120,
) -> dict:  # pragma: no cover — exercised on the worker
    """Materialize a repo, boot a throwaway sandbox, run ONE probe, return its
    real exit code as a plain dict. Runs on the socket-having worker so the
    API container never needs the Docker socket.

    Never raises — provisioning/exec errors are captured into the returned
    dict's `error` field.
    """
    try:
        return asyncio.run(
            _run_probe_async(
                workspace_tar_b64=workspace_tar_b64,
                git_url=git_url,
                git_ref=git_ref,
                probe_cmd=probe_cmd,
                setup_cmds=setup_cmds,
                timeout_s=timeout_s,
            )
        )
    except Exception as exc:  # noqa: BLE001 — never let the task raise
        log.exception("run_probe_task.failed", error=str(exc))
        return {
            "available": False,
            "exit_code": -1,
            "stdout": None,
            "stderr": None,
            "setup_log": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
