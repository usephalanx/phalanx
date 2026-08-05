"""CI Fixer — probe-synthesis task (FetchSandbox generative prove).

For a NOVEL bug with no curated scenario, FetchSandbox authors the probe itself:
materialize the buggy repo on the worker, install deps, then run the Claude Max
subprocess (Read/Grep/Glob/Write/Edit/Bash) to WRITE a self-contained behavioral
probe that reproduces the bug on the repo's REAL code — and to RUN it until it
exits 1 (bug reproduced) on this buggy code.

Language-aware: detects the repo's stack (node / python / …) and authors the
probe in THAT language, installing + running with that language's tooling, so it
works on FastAPI/Python apps as well as Node — not just JS.

The non-negotiable safety rule (the lesson from the false-green): a synthesized
probe is trusted ONLY if it reproduces on the buggy code. The subprocess
self-checks, and THEN this task independently re-runs the probe and returns
available=True only if it exits 1 here. Otherwise the probe is discarded.

Exit convention (matches proof_gate): 0=HELD(bug absent), 1=VIOLATED(reproduced),
2=INCONCLUSIVE(harness error). Reuses find_bugs_task helpers. Never raises.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import structlog

from phalanx.ci_fixer_v3.find_bugs_task import _EXHAUSTION, _accounts, _claude_bin
from phalanx.ci_fixer_v3.run_probe_task import _materialize
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

_TOOLS = "Read,Grep,Glob,Write,Edit,Bash"


def _detect_lang(ws: Path) -> dict:
    """(lang, probe_file, probe_cmd, setup_cmds) from the repo's manifests."""
    if (ws / "package.json").exists():
        return {"lang": "JavaScript/Node", "probe_file": "_fs_probe.js",
                "probe_cmd": "node _fs_probe.js",
                "setup_cmds": ["npm install --no-audit --no-fund --loglevel=error"]}
    is_py = (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists() \
        or (ws / "setup.py").exists() or any(ws.glob("*.py")) or any(ws.glob("**/*.py"))
    if is_py:
        setup = []
        if (ws / "requirements.txt").exists():
            setup = ["pip install --quiet --disable-pip-version-check -r requirements.txt"]
        elif (ws / "pyproject.toml").exists() or (ws / "setup.py").exists():
            setup = ["pip install --quiet --disable-pip-version-check -e . || "
                     "pip install --quiet --disable-pip-version-check ."]
        # The probe drives the app in-process. FastAPI/Starlette TestClient (the
        # dominant driver) needs httpx, and many probes reach for requests —
        # neither is usually a runtime dep of the app, so the CLEAN prove
        # container lacks them. Install them here so the probe imports there;
        # the worker's global env would otherwise mask the gap at self-validation
        # (self-validation and prove must run the SAME install set).
        setup.append("pip install --quiet --disable-pip-version-check httpx requests")
        return {"lang": "Python", "probe_file": "_fs_probe.py",
                "probe_cmd": "python3 _fs_probe.py", "setup_cmds": setup}
    # default: Node
    return {"lang": "JavaScript/Node", "probe_file": "_fs_probe.js",
            "probe_cmd": "node _fs_probe.js",
            "setup_cmds": ["npm install --no-audit --no-fund --loglevel=error"]}


def _synth_prompt(bug: str, env: dict) -> str:
    pf, pc, lang = env["probe_file"], env["probe_cmd"], env["lang"]
    return (
        "You are FetchSandbox's probe-synthesis engine. Write a SELF-CONTAINED "
        f"behavioral probe IN {lang} that PROVES the bug below on THIS repo's REAL code.\n\n"
        "BUG:\n" + bug.strip() + "\n\n"
        "Requirements:\n"
        f"- Write the probe to `{pf}` at the repo root; it runs as `{pc}`.\n"
        f"- It MUST exercise the repo's REAL code (import/require the actual handlers/"
        "modules in " + lang + "), never a reimplementation of them.\n"
        "- For external I/O the bug does NOT depend on (DB, network), you may inject a "
        "lightweight recorder/stub that RECORDS what the code does but does NOT decide "
        "the outcome — the verdict must come from the repo's real logic. For anything "
        "the bug DOES depend on, use the real thing.\n"
        "- If the code verifies a signature/token, compute a VALID one (set the secret "
        "in env yourself) so you reach the real logic instead of bouncing at the check.\n"
        "- Assert the correct-behavior invariant. Exit codes: 0 = invariant HELD (bug "
        "absent), 1 = invariant VIOLATED (bug reproduced), 2 = harness/setup error.\n"
        "- The exit-0 (HELD) path MUST be reachable and GRACEFUL. When the bug is absent "
        "(the code correctly rejects/clamps/handles the bad input), the probe must cleanly "
        "exit 0 — NOT throw or exit 2. A grant that is skipped, clamped to a safe value, "
        "rejected, or produces no record counts as HELD (0), not a harness error. Read the "
        "recorder defensively (missing/empty = the bad thing did NOT happen = HELD).\n"
        "- Dependencies are already installed.\n"
        "- PROOF EVIDENCE (required): the receipt shows engineers the exact traffic. For "
        "EVERY request you fire at the real handler, record the real request you sent and "
        "the real response you got back. Do NOT summarize or invent — capture the actual "
        "bytes. Accumulate a list of entries, each:\n"
        '    {\"label\": \"<short human label, e.g. \'retry #2\'>\", \"request\": '
        '{\"method\": \"POST\", \"path\": \"/webhook\", \"headers\": {..the headers you sent, '
        "including any signature..}, \"body\": <the request body you sent, parsed JSON or a "
        'string>}, \"response\": {\"status\": <int http status>, \"body\": <the response body '
        "you received, parsed JSON or a string>}}\n"
        "  At the very end, print this list as ONE final line, exactly:\n"
        "    FS_PROOF_JSON=<compact-single-line-json-array>\n"
        "  (use json.dumps(list, separators=(',',':')) — no newlines inside it). Keep bodies "
        "real but trim giant blobs to the fields that matter to the bug. This line is parsed "
        "by the pipeline; the exit code is still the verdict.\n"
        "- Dependencies are already installed.\n"
        f"- CRITICAL: run `{pc}` yourself. On THIS code (which HAS the bug) it MUST exit 1. "
        "Iterate until it does. NEVER ship a probe that exits 0 here — a probe that doesn't "
        "catch the bug is worthless. If you truly cannot reproduce it, say so explicitly.\n"
        "When done, state the final exit code you observed."
    )


def _setup(ws: Path, setup_cmds: list[str], timeout_s: int) -> bool:
    for cmd in setup_cmds:
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(ws),
                               capture_output=True, text=True, timeout=timeout_s)
            if p.returncode != 0:
                log.info("synthesize_probe.setup_nonzero", cmd=cmd, err=(p.stderr or "")[-200:])
                # non-fatal: many probes only need the stdlib; keep going
        except Exception:  # noqa: BLE001
            return False
    return True


def _run_probe_exit(ws: Path, probe_cmd: str, timeout_s: int) -> int:
    """Independently run the probe; return its exit code (2 on any harness error)."""
    try:
        p = subprocess.run(shlex.split(probe_cmd), cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout_s)
        return p.returncode
    except Exception:  # noqa: BLE001
        return 2


def _run_claude(ws: Path, prompt: str, timeout_s: int) -> tuple[str | None, str]:
    claude = _claude_bin()
    if not claude:
        return None, "claude CLI not found on worker"
    last = ""
    for label, token in _accounts():
        env = {**os.environ}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        try:
            p = subprocess.run(
                [claude, "-p", prompt, "--allowedTools", _TOOLS, "--output-format", "text"],
                cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout_s,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode == 0:
                return label, out[:2000]
            last = out[:400]
            if any(m in out.lower() for m in _EXHAUSTION):
                log.info("synthesize_probe.capacity_failover", account=label)
                continue
            break
        except subprocess.TimeoutExpired:
            last = "claude subprocess timed out"
            break
    return None, last or "claude failed"


@celery_app.task(
    name="phalanx.ci_fixer_v3.synthesize_probe_task.synthesize_probe_task",
    queue="cifix_sre",
    max_retries=0,
    soft_time_limit=900,
    time_limit=960,
)
def synthesize_probe_task(
    workspace_tar_b64: str | None = None,
    git_url: str | None = None,
    git_ref: str | None = None,
    bug: str = "",
    timeout_s: int = 600,
) -> dict:  # pragma: no cover — exercised on the worker
    """Author + self-validate a probe for `bug` on the (buggy) repo, in the repo's
    OWN language. Returns {available, probe_src, probe_file, probe_cmd, setup_cmds,
    lang, buggy_exit, summary, account, error}. available=True ONLY if the probe
    reproduces (exit 1) on the buggy code. Never raises."""
    try:
        if not bug.strip():
            return {"available": False, "error": "no bug provided"}
        with tempfile.TemporaryDirectory(prefix="fs_synth_") as tmp:
            ws = Path(tmp)
            _materialize(dest=ws, workspace_tar_b64=workspace_tar_b64,
                         git_url=git_url, git_ref=git_ref)
            env = _detect_lang(ws)
            if not _setup(ws, env["setup_cmds"], min(timeout_s, 300)):
                return {"available": False, "lang": env["lang"],
                        "error": "dependency install failed on worker"}

            account, summary = _run_claude(ws, _synth_prompt(bug, env), timeout_s)
            probe_path = ws / env["probe_file"]
            if not probe_path.exists():
                return {"available": False, "account": account, "lang": env["lang"],
                        "summary": summary, "error": "subprocess wrote no probe"}

            # INDEPENDENT self-validation: the probe MUST reproduce on buggy code.
            buggy_exit = _run_probe_exit(ws, env["probe_cmd"], min(timeout_s, 120))
            probe_src = probe_path.read_text()
            if buggy_exit != 1:
                return {"available": False, "account": account, "summary": summary,
                        "lang": env["lang"], "buggy_exit": buggy_exit, "probe_src": probe_src,
                        "error": f"probe did not reproduce on buggy code (exit {buggy_exit}) — discarded"}
            return {"available": True, "account": account, "summary": summary,
                    "lang": env["lang"], "buggy_exit": buggy_exit, "probe_src": probe_src,
                    "probe_file": env["probe_file"], "probe_cmd": env["probe_cmd"],
                    "setup_cmds": env["setup_cmds"], "error": None}
    except Exception as exc:  # noqa: BLE001
        log.exception("synthesize_probe_task.failed", error=str(exc))
        return {"available": False, "error": str(exc)}
