"""CI Fixer — fix-bug task (FetchSandbox grounded remediation).

Sibling of find_bugs_task. Where find_bugs DISCOVERS bugs read-only, this task
AUTHORS a fix: it materializes the caller's repo on the worker, runs the Claude
Max CLI subprocess with Edit/Write allowed — grounded by the brain's fix_pattern
for the matched failure class — and returns a `git diff` of the change. It NEVER
mutates the user's working tree; the diff is handed back for the user to apply.

Correctness is NOT claimed here — this only AUTHORS the patch. The honest-green
gate (buggy fails -> patched passes, measured in Phalanx) is what certifies it.
Subprocess proposes; the gate disposes.

Reuses run_probe_task._materialize (untar b64 / shallow clone) and the account /
capacity-failover helpers from find_bugs_task. Runs on the ci-fixer-worker, which
carries the `claude` CLI + Max tokens. The task NEVER raises.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

from phalanx.ci_fixer_v3.find_bugs_task import _EXHAUSTION, _accounts, _claude_bin
from phalanx.ci_fixer_v3.run_probe_task import _materialize
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

_FIX_TOOLS = "Read,Grep,Glob,Edit,Write"


def _fix_prompt(bug: str, fix_pattern: str | None) -> str:
    grounding = ""
    if fix_pattern and fix_pattern.strip():
        grounding = (
            "\n\nKNOWN REMEDIATION for this failure class (apply this pattern, "
            "adapted to the code):\n" + fix_pattern.strip()
        )
    return (
        "You are FetchSandbox's remediation engine. Apply the MINIMAL correct fix "
        "for the specific production bug below — and nothing else. Do not refactor "
        "unrelated code, do not reformat, do not touch other files unless the fix "
        "genuinely requires it.\n\nBUG:\n"
        + bug.strip()
        + grounding
        + "\n\nEdit the source in place to fix ONLY this bug. When done, state in "
        "one or two sentences what you changed and why."
    )


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    # `-c safe.directory=*`: the materialized tree carries the caller's original
    # uid (e.g. macOS 501) while the worker runs as root, which trips git 2.35+'s
    # "dubious ownership" guard and makes every git call fail silently (→ empty
    # diff → "no changes"). Safe here: throwaway dirs in an isolated worker.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=str(ws), capture_output=True, text=True,
    )


def _ensure_baseline(ws: Path) -> bool:
    """Make HEAD the pre-fix baseline so a later `git diff` is clean.

    Cloned repos already have a clean HEAD at the requested ref. Tar'd / private
    code has no .git — init one and commit the untouched tree. Returns False iff
    git is unavailable (then we can't produce a diff)."""
    if not shutil.which("git"):
        return False
    if (ws / ".git").exists():
        return True
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "engine@fetchsandbox.com")
    _git(ws, "config", "user.name", "FetchSandbox")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "fs-baseline", "--allow-empty")
    return True


def _reset_to_baseline(ws: Path) -> None:
    """Restore the workspace to HEAD so a failover attempt starts clean rather
    than stacking on the previous account's partial edits."""
    _git(ws, "reset", "-q", "--hard", "HEAD")
    _git(ws, "clean", "-fdq")


def _diff(ws: Path) -> str:
    """Unified diff of everything changed vs baseline HEAD, including new files."""
    _git(ws, "add", "-A")
    return _git(ws, "-c", "core.pager=cat", "diff", "--cached").stdout


def _run_fix(ws: Path, prompt: str, timeout_s: int) -> dict:
    claude = _claude_bin()
    if not claude:
        return {"available": False, "diff": None, "summary": None,
                "account": None, "error": "claude CLI not found on worker"}
    if not _ensure_baseline(ws):
        return {"available": False, "diff": None, "summary": None,
                "account": None, "error": "git not available on worker — cannot diff"}

    last = ""
    for label, token in _accounts():
        _reset_to_baseline(ws)  # clean slate per account (no stacked edits, no double-fix)
        env = {**os.environ}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        try:
            p = subprocess.run(
                [claude, "-p", prompt, "--allowedTools", _FIX_TOOLS,
                 "--output-format", "text"],
                cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout_s,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode == 0:
                diff = _diff(ws)
                if diff.strip():
                    return {"available": True, "diff": diff, "summary": out[:2000],
                            "account": label, "error": None}
                # Clean exit but no edits — the agent changed nothing. Deterministic:
                # a second account won't behave differently, so don't failover/double-bill.
                last = "subprocess produced no changes"
                break
            last = out[:400]
            if any(m in out.lower() for m in _EXHAUSTION):
                log.info("fix_bug.capacity_failover", account=label)
                continue  # capacity → try the next Max account
            break  # deterministic failure → don't retry on the other account
        except subprocess.TimeoutExpired:
            last = "claude subprocess timed out"
            break
    return {"available": False, "diff": None, "summary": None,
            "account": None, "error": last or "claude fix failed"}


@celery_app.task(
    name="phalanx.ci_fixer_v3.fix_bug_task.fix_bug_task",
    queue="cifix_sre",  # ci-fixer-worker (carries claude CLI + Max tokens)
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def fix_bug_task(
    workspace_tar_b64: str | None = None,
    git_url: str | None = None,
    git_ref: str | None = None,
    bug: str = "",
    fix_pattern: str | None = None,
    prompt: str | None = None,
    timeout_s: int = 300,
) -> dict:  # pragma: no cover — exercised on the worker
    """Materialize a repo and run the grounded Max subprocess to AUTHOR a fix for
    `bug`. Returns {available, diff, summary, account, error}. Never raises. The
    diff is a proposal — the gate certifies it, not this task."""
    try:
        if not bug.strip() and not prompt:
            return {"available": False, "diff": None, "summary": None,
                    "account": None, "error": "no bug or prompt provided"}
        with tempfile.TemporaryDirectory(prefix="fs_fix_") as tmp:
            ws = Path(tmp)
            _materialize(
                dest=ws,
                workspace_tar_b64=workspace_tar_b64,
                git_url=git_url,
                git_ref=git_ref,
            )
            return _run_fix(ws, prompt or _fix_prompt(bug, fix_pattern), timeout_s)
    except Exception as exc:  # noqa: BLE001 — never let the task raise
        log.exception("fix_bug_task.failed", error=str(exc))
        return {"available": False, "diff": None, "summary": None,
                "account": None, "error": str(exc)}
