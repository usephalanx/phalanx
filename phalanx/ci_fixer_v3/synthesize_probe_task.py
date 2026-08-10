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


# DB / infra dependencies that mean the app is a SERVICE (needs a running DB or
# broker to boot) — the clean sandbox has none, so the probe must STUB them.
_DB_DEPS = (
    "pg", "pg-promise", "mysql", "mysql2", "mongoose", "mongodb", "prisma",
    "@prisma/client", "sequelize", "typeorm", "knex", "redis", "ioredis",
    "psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "sqlalchemy",
    "databases", "django", "aiomysql", "pymongo", "motor", "redis-py",
)


def _service_signals(ws: Path) -> tuple[bool, str]:
    """Does this app need external infra (DB/broker) to boot? Returns (service,
    db_hint). Read from manifests + a light content scan — a service app cannot
    run in the clean sandbox unless the probe stubs that infra."""
    blob = ""
    for mf in ("package.json", "requirements.txt", "pyproject.toml", "setup.py"):
        p = ws / mf
        if p.exists():
            try:
                blob += p.read_text().lower() + "\n"
            except Exception:  # noqa: BLE001
                pass
    hits = sorted({d for d in _DB_DEPS if d in blob})
    if hits:
        return True, ", ".join(hits[:4])
    # content fallback: a db.query / new Pool / create_engine reachable from a route
    try:
        for f in list(ws.glob("**/*.js"))[:40] + list(ws.glob("**/*.py"))[:40]:
            t = f.read_text(errors="ignore").lower()
            if "new pool(" in t or "createpool" in t or "create_engine" in t or "psycopg" in t:
                return True, "database"
    except Exception:  # noqa: BLE001
        pass
    return False, ""


def _detect_lang(ws: Path) -> dict:
    """(lang, probe_file, probe_cmd, setup_cmds, service, db_hint) from manifests."""
    service, db_hint = _service_signals(ws)
    if (ws / "package.json").exists():
        node_setup = ["npm install --no-audit --no-fund --loglevel=error"]
        if service:
            # supertest drives the in-process Express app; not an app dep, so the
            # clean container lacks it (same gap httpx filled for Python).
            node_setup.append("npm install --no-audit --no-fund --loglevel=error supertest")
        return {"lang": "JavaScript/Node", "probe_file": "_fs_probe.js",
                "probe_cmd": "node _fs_probe.js", "setup_cmds": node_setup,
                "service": service, "db_hint": db_hint}
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
                "probe_cmd": "python3 _fs_probe.py", "setup_cmds": setup,
                "service": service, "db_hint": db_hint}
    # default: Node
    return {"lang": "JavaScript/Node", "probe_file": "_fs_probe.js",
            "probe_cmd": "node _fs_probe.js",
            "setup_cmds": ["npm install --no-audit --no-fund --loglevel=error"],
            "service": service, "db_hint": db_hint}


def _synth_prompt(bug: str, env: dict, grounding: str = "") -> str:
    pf, pc, lang = env["probe_file"], env["probe_cmd"], env["lang"]
    # When the bug matches a curated FetchSandbox failure class, we hand the probe
    # author the KNOWN reproduction recipe. Reproducing a known class is a recipe,
    # not an open-ended search, so this hint makes the probe more reliable (fewer
    # harness/exit-2 flakes) — the opposite of grounding the bug-FINDER, which
    # tunnel-visions. It never changes the verdict; the gate still judges the flip.
    ground = (
        "\nKNOWN REPRODUCTION for this failure class (follow this recipe to author a "
        "reliable probe — it is how this class is reproduced in practice):\n"
        + grounding.strip() + "\n"
    ) if grounding.strip() else ""
    # Service apps (Express+pg, FastAPI+psycopg/sqlalchemy, …) need a running DB
    # to boot — the clean sandbox has none. The recipe: STUB the infra the bug
    # does not depend on, KEEP the real deciding I/O, drive the app in-process.
    svc = ""
    if env.get("service"):
        db = env.get("db_hint") or "a database"
        if "Node" in lang:
            svc = (
                f"\nSERVICE APP ({db}) — CRITICAL: this app needs infra to boot and the sandbox has "
                "NONE, so you MUST stub it or the probe will exit 2 (couldn't run the service):\n"
                "  * BEFORE requiring the app, replace the DB client with an in-memory fake that "
                "RECORDS queries and returns canned rows — e.g. monkeypatch `require('pg').Pool` "
                "(and .connect/.query), or override the app's own db module via require cache. The "
                "recorder RECORDS side effects; it never decides the verdict.\n"
                "  * Set EVERY env var the app reads at import (DATABASE_URL, API keys, secrets, "
                "*_PRICE_ID, APP_URL) to dummy values so it boots without throwing.\n"
                "  * Import the real Express `app` in-process and fire requests with `supertest` "
                "(or node's http against `app.listen(0)`). Do NOT spawn a separate server/process "
                "and do NOT expect a real DB/network.\n"
                "  * KEEP the real deciding I/O: if the bug depends on Stripe/Paddle/Svix signature "
                "verification, compute a VALID signature against the app's own secret.\n"
                "  * Judge the invariant from the RECORDER (how many times the side effect ran / "
                "with what args), not from the stub's return values.\n"
            )
        else:
            svc = (
                f"\nSERVICE APP ({db}) — CRITICAL: this app needs infra to boot and the sandbox has "
                "NONE, so you MUST stub it or the probe will exit 2:\n"
                "  * BEFORE importing the app, replace the DB layer with an in-memory recorder — "
                "monkeypatch the db module / psycopg connect / sqlalchemy engine (via sys.modules or "
                "unittest.mock) so queries are recorded and canned rows returned. It records, never "
                "decides.\n"
                "  * Set every env var the app reads at import to dummy values so it boots.\n"
                "  * Drive the real app in-process via `fastapi.testclient.TestClient` (or the app's "
                "framework equivalent). Do NOT expect a real DB/network.\n"
                "  * KEEP the real deciding I/O (signature/token verification — compute a valid one).\n"
                "  * Judge the invariant from the RECORDER, not the stub.\n"
            )
    return (
        "You are FetchSandbox's probe-synthesis engine. Write a SELF-CONTAINED "
        f"behavioral probe IN {lang} that PROVES the bug below on THIS repo's REAL code.\n\n"
        "BUG:\n" + bug.strip() + "\n" + ground + svc + "\n"
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
        "- SIDE-EFFECT EVIDENCE (CRITICAL for side-effect bugs): many bugs (double-charge, "
        "double-provision, duplicate send) leave the HTTP response IDENTICAL before and "
        "after the fix — the response is always 200 {\"received\":true}. The proof lives in "
        "the SIDE EFFECT you already read from the recorder to decide the verdict. So on "
        "EACH entry, ALSO record what the recorder observed AFTER that request, as:\n"
        '    \"side_effect\": {\"label\": \"<what you measured, e.g. \'seats granted '
        "(cumulative)' or 'charge count'>\", \"value\": <the number the invariant checks — "
        "e.g. total seats granted so far, or how many times the side effect ran>}\n"
        "  Use the SAME measurement that drives your exit code, so the receipt shows the "
        "real before/after (e.g. seats 5 then 10 on the buggy retry; 5 then 5 when fixed). "
        "The `value` MUST be a plain number. If the bug genuinely has no numeric side "
        "effect, omit the field. NEVER fabricate it — read it from the recorder.\n"
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
    grounding: str = "",
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

            account, summary = _run_claude(ws, _synth_prompt(bug, env, grounding), timeout_s)
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
