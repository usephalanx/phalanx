"""CI Fixer — find-bugs task (FetchSandbox discovery).

Materialize the caller's repo ON THE WORKER, then run the Claude Max CLI
subprocess to DISCOVER bugs by reading the code, and return the findings.

Unlike run_probe_task this does NOT provision a Docker container — it only reads
source + runs the subprocess, so it doesn't need the Docker socket. It reuses
run_probe_task._materialize (untar base64 / shallow git clone) and runs on the
ci-fixer-worker, which carries the `claude` CLI + Max tokens.

Max-account failover (ember rule): try Max#1 (ambient CLAUDE_CODE_OAUTH_TOKEN),
and ONLY on a capacity/quota error retry on Max#2 (CLAUDE_CODE_OAUTH_TOKEN_FALLBACK).
A deterministic failure (bad prompt, CLI missing, empty output) is NOT retried on
the second account — a second subscription can't fix a bug and retrying double-bills.

The task NEVER raises — every error is captured into the returned dict's `error`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

from phalanx.ci_fixer_v3.grounding import describe as describe_grounding
from phalanx.ci_fixer_v3.run_probe_task import _materialize
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# Capacity/exhaustion markers → a DIFFERENT Max account could satisfy the call.
_EXHAUSTION = (
    "session limit", "rate limit", "usage limit", "weekly limit",
    "429", "overloaded", "5-hour", "five-hour", "quota", "capacity",
)

_DEFAULT_PROMPT = (
    "You are FetchSandbox's analysis engine auditing a repo for PRODUCTION bugs "
    "(auth, webhooks, payments, data handling, error handling, security, input "
    "validation). Read all source. List EVERY real bug: file:line, a one-line "
    "description, and how it manifests in production. Numbered list only."
)


def _claude_bin() -> str | None:
    return shutil.which("claude") or next(
        (
            p
            for p in (
                os.path.expanduser("~/.local/bin/claude"),
                os.path.expanduser("~/.claude/bin/claude"),
                "/usr/local/bin/claude",
            )
            if os.path.exists(p)
        ),
        None,
    )


def _accounts() -> list[tuple[str, str | None]]:
    """[(label, token_override_or_None)] — Max#1 (ambient), then Max#2 iff a
    fallback token is configured. No fallback → single attempt (strict no-op)."""
    accounts: list[tuple[str, str | None]] = [("max1", None)]
    fb = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_FALLBACK") or "").strip()
    if fb:
        accounts.append(("max2", fb))
    return accounts


def _run_claude(ws: Path, prompt: str, timeout_s: int) -> dict:
    claude = _claude_bin()
    if not claude:
        return {"available": False, "bugs": None, "account": None,
                "error": "claude CLI not found on worker"}
    last = ""
    for label, token in _accounts():
        env = {**os.environ}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        try:
            p = subprocess.run(
                [claude, "-p", prompt, "--allowedTools", "Read,Grep,Glob",
                 "--output-format", "text"],
                cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout_s,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode == 0 and out:
                return {"available": True, "bugs": out, "account": label, "error": None}
            last = out[:400]
            if any(m in out.lower() for m in _EXHAUSTION):
                log.info("find_bugs.capacity_failover", account=label)
                continue  # capacity → try the next Max account
            break  # deterministic failure → don't retry on the other account
        except subprocess.TimeoutExpired:
            last = "claude subprocess timed out"
            break
    return {"available": False, "bugs": None, "account": None, "error": last or "claude failed"}


@celery_app.task(
    name="phalanx.ci_fixer_v3.find_bugs_task.find_bugs_task",
    queue="cifix_sre",  # ci-fixer-worker (carries claude CLI + Max tokens)
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def find_bugs_task(
    workspace_tar_b64: str | None = None,
    git_url: str | None = None,
    git_ref: str | None = None,
    prompt: str | None = None,
    timeout_s: int = 300,
    spec: str | None = None,
) -> dict:  # pragma: no cover — exercised on the worker
    """Materialize a repo and run the Max subprocess to discover bugs.
    Returns {available, bugs, account, error, grounding}. Never raises.

    `spec` is an opaque label (FetchSandbox's brain id). Phalanx echoes it for
    attribution and never interprets it. `grounding` reports which grounding
    actually reached the subprocess — see phalanx.ci_fixer_v3.grounding.
    """
    prompt_used = prompt or _DEFAULT_PROMPT
    meta = describe_grounding(prompt_used, spec=spec, fields={"prompt": prompt})
    try:
        with tempfile.TemporaryDirectory(prefix="fs_find_") as tmp:
            ws = Path(tmp)
            _materialize(
                dest=ws,
                workspace_tar_b64=workspace_tar_b64,
                git_url=git_url,
                git_ref=git_ref,
            )
            return {**_run_claude(ws, prompt_used, timeout_s), "grounding": meta}
    except Exception as exc:  # noqa: BLE001 — never let the task raise
        log.exception("find_bugs_task.failed", error=str(exc))
        # Attribution rides the failure path too: an ungrounded run that errors
        # and a grounded run that errors are different data points.
        return {"available": False, "bugs": None, "account": None,
                "error": str(exc), "grounding": meta}
