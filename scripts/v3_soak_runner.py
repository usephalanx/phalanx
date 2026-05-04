#!/usr/bin/env python3
"""v1.7.2.4 — minimal soak runner for Phalanx CI Fixer.

Loops the 4 testbed cells (lint → test_fail → flake → coverage), one every
~15 min, and appends one JSON-Lines row per run to soak-runs.jsonl with the
fields the soak plan declared as MUST-have:

    run_id, verdict, iterations, engineer_commit_sha, verified_commit_sha,
    GitHub check-gate decision, last failure fingerprint

This is intentionally minimal:
  - No dashboards
  - No new failure scenarios beyond the existing 4 cells
  - No Postgres-side writes; JSONL only
  - Stdlib only — no httpx, no async

Triggers the existing scripts/v3_python_regression.sh per cell. After the
script returns, queries prod Postgres + ci-fixer-worker logs over SSH to
fetch the additional metrics. Writes one record. Sleeps until the next
15-min boundary. Repeats until SIGTERM, --max-runs hit, or
/tmp/soak-stop sentinel touched.

Usage:

    # default — every 15 min, append to ./soak-runs.jsonl, run forever
    python3 scripts/v3_soak_runner.py

    # smoke-test: run 2 iterations only, 60s between them
    python3 scripts/v3_soak_runner.py --cadence-s 60 --max-runs 2

Env / CLI knobs:
    --cadence-s          seconds between starts (default 900 = 15 min)
    --out                JSONL output path (default ./soak-runs.jsonl)
    --max-runs           stop after N iterations (default: forever)
    --ssh-key            path to prod SSH key
    --ssh-host           prod ssh target (e.g. ubuntu@1.2.3.4)
    --pg-container       prod postgres container name
    --worker-container   prod ci-fixer-worker container name (for log greps)

Exit cleanly on:
    - SIGTERM / SIGINT (writes final state, exits after current iter)
    - /tmp/soak-stop file present (checked before each new iter)
    - --max-runs reached
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CELLS: tuple[str, ...] = ("lint", "test_fail", "flake", "coverage")

DEFAULT_CADENCE_S = 15 * 60
DEFAULT_OUT_PATH = Path("soak-runs.jsonl")
STATE_PATH = Path("soak-state.json")
STOP_SENTINEL = Path("/tmp/soak-stop")

REPO_ROOT = Path(__file__).resolve().parent.parent
TRIGGER_SCRIPT = REPO_ROOT / "scripts" / "v3_python_regression.sh"

DEFAULT_SSH_KEY = os.environ.get(
    "SSH_KEY", os.path.expanduser("~/work/aws/LightsailDefaultKey-us-west-2.pem")
)
DEFAULT_SSH_HOST = os.environ.get("SSH_HOST", "ubuntu@44.233.157.41")
DEFAULT_PG_CONTAINER = os.environ.get("PG_CONTAINER", "phalanx-prod-postgres-1")
DEFAULT_WORKER_CONTAINER = os.environ.get(
    "WORKER_CONTAINER", "phalanx-prod-phalanx-ci-fixer-worker-1"
)

# Per-cell trigger script timeout. v1.7.2.4 cells generally finish in
# 2-6 min; humanize-style runs can stretch with matrix CI gate poll.
# 25 min is a generous ceiling for testbed cells.
CELL_TIMEOUT_S = 25 * 60

# Strict UUID regex — any run_id we use to construct SQL must match this.
# Defense-in-depth alongside the parsed pipe-delimited input from the
# regression script.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_stop_flag = False


def _handle_signal(signum, _frame):
    global _stop_flag
    _stop_flag = True
    print(
        f"\n[soak] signal {signum} received — will exit after current iteration",
        file=sys.stderr,
        flush=True,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_result_line(line: str) -> dict | None:
    """The regression script's last stdout line is a `|`-delimited summary:

        cell|verdict|run_id|intro_sha|head_sha

    Returns the parsed dict or None if the line doesn't look like that."""
    if not line:
        return None
    parts = line.strip().split("|")
    if len(parts) < 3 or parts[0] not in CELLS:
        return None
    return {
        "cell": parts[0],
        "verdict": parts[1] if len(parts) > 1 else None,
        "run_id": parts[2] if len(parts) > 2 else None,
        "intro_sha": parts[3] if len(parts) > 3 else None,
        "head_sha": parts[4] if len(parts) > 4 else None,
    }


def _ssh_cmd(ssh_key: str, ssh_host: str, remote: str) -> list[str]:
    return [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        remote,
    ]


def _ssh(ssh_key: str, ssh_host: str, remote: str, timeout: int = 30) -> str:
    """Run `remote` over SSH and return stdout (str). Empty string on failure."""
    try:
        proc = subprocess.run(
            _ssh_cmd(ssh_key, ssh_host, remote),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[soak] ssh timeout: {remote[:80]}", file=sys.stderr)
        return ""
    except Exception as exc:  # noqa: BLE001
        print(f"[soak] ssh exception: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        print(
            f"[soak] ssh exit={proc.returncode} stderr={proc.stderr.strip()[-200:]!r}",
            file=sys.stderr,
        )
    return proc.stdout


def _query_run_metrics(
    run_id: str, ssh_key: str, ssh_host: str, pg_container: str
) -> dict:
    """One round-trip to prod psql. Returns the metric fields the soak
    plan declared. UUID-validated — `run_id` never gets concatenated raw."""
    if not _UUID_RE.match(run_id):
        return {}
    sql = (
        "SELECT "
        f"(SELECT COUNT(*) FROM tasks WHERE run_id='{run_id}' AND "
        "agent_role='cifix_sre_verify' AND status='COMPLETED') AS iterations, "
        f"(SELECT output->>'commit_sha' FROM tasks WHERE run_id='{run_id}' AND "
        "agent_role='cifix_engineer' AND status='COMPLETED' "
        "ORDER BY sequence_num DESC LIMIT 1) AS engineer_commit_sha, "
        f"(SELECT output->>'verified_commit_sha' FROM tasks WHERE run_id='{run_id}' AND "
        "agent_role='cifix_sre_verify' AND status='COMPLETED' "
        "ORDER BY sequence_num DESC LIMIT 1) AS verified_commit_sha, "
        f"(SELECT output->>'fingerprint' FROM tasks WHERE run_id='{run_id}' AND "
        "agent_role='cifix_sre_verify' AND status='COMPLETED' "
        "ORDER BY sequence_num DESC LIMIT 1) AS last_fingerprint, "
        f"(SELECT error_context->>'final_reason' FROM runs WHERE id='{run_id}') "
        "AS final_reason, "
        f"(SELECT status FROM runs WHERE id='{run_id}') AS run_status"
    )
    remote = (
        f"docker exec {pg_container} psql -tA -F'|' -U forge -d forge "
        f"-c \"{sql}\""
    )
    out = _ssh(ssh_key, ssh_host, remote, timeout=30).strip()
    if not out:
        return {}
    line = out.splitlines()[-1]
    parts = line.split("|")
    while len(parts) < 6:
        parts.append("")
    return {
        "iterations": int(parts[0]) if parts[0].isdigit() else 0,
        "engineer_commit_sha": parts[1] or None,
        "verified_commit_sha": parts[2] or None,
        "last_fingerprint": parts[3] or None,
        "final_reason": parts[4] or None,
        "run_status": parts[5] or None,
    }


def _query_recent_run_id_fallback(
    *,
    start_ts: datetime,
    cell: str,
    ssh_key: str,
    ssh_host: str,
    pg_container: str,
    repo: str = "usephalanx/phalanx-ci-fixer-testbed",
) -> str | None:
    """Fallback when stdout-line parsing fails.

    The regression script normally prints a `cell|verdict|run_id|...`
    summary on its last stdout line. Occasionally that line doesn't make
    it (script crash before final echo, runner regex mismatch, ANSI noise
    interleaved). When _parse_result_line returns None, this helper
    queries prod for the most recent run created after `start_ts` on the
    testbed for the given cell.

    Match strategy:
      - join runs ↔ work_orders to read the v3-rerun branch from
        work_orders.raw_command (JSON; the dispatch path stored
        ci_context there)
      - filter by `created_at > start_ts` AND raw_command's branch
        starts with `v3-rerun/<cell>-`
      - ORDER BY created_at DESC, take 1

    Returns the run UUID, or None if nothing found / SSH failed.
    """
    # Postgres timestamp format the prod psql can compare against
    ts_str = start_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    branch_prefix = f"v3-rerun/{cell}-"
    sql = (
        "SELECT r.id::text "
        "FROM runs r "
        "JOIN work_orders w ON w.id = r.work_order_id "
        f"WHERE r.created_at > '{ts_str}' "
        "AND w.work_order_type = 'ci_fix' "
        f"AND (w.raw_command::jsonb->>'repo') = '{repo}' "
        f"AND (w.raw_command::jsonb->>'branch') LIKE '{branch_prefix}%' "
        "ORDER BY r.created_at DESC LIMIT 1"
    )
    remote = (
        f"docker exec {pg_container} psql -tA -U forge -d forge "
        f"-c \"{sql}\""
    )
    out = _ssh(ssh_key, ssh_host, remote, timeout=30).strip()
    if not out:
        return None
    candidate = out.splitlines()[-1].strip()
    if _UUID_RE.match(candidate):
        return candidate
    return None


def _fetch_gate_decision(
    run_id: str, ssh_key: str, ssh_host: str, worker_container: str
) -> str | None:
    """Grep the ci-fixer-worker for `cifix_commander.check_gate_verdict`
    lines mentioning this run_id; return the decision from the LAST one
    (most recent iteration's verdict).

    Returns None if the gate didn't fire (e.g. legacy ship path with
    no integration token, or run failed before reaching the gate)."""
    if not _UUID_RE.match(run_id):
        return None
    remote = (
        f"sudo docker logs --since 60m {worker_container} 2>&1 | "
        f"grep '{run_id}' | grep 'check_gate_verdict' | tail -1"
    )
    out = _ssh(ssh_key, ssh_host, remote, timeout=30).strip()
    if not out:
        return None
    m = re.search(r"decision=(\w+)", out)
    return m.group(1) if m else None


def _run_cell(cell: str) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """Spawn the regression script for one cell. Returns (CompletedProcess,
    last_stdout_line). On exception/timeout returns (None, None)."""
    if not TRIGGER_SCRIPT.is_file():
        print(f"[soak] trigger script missing: {TRIGGER_SCRIPT}", file=sys.stderr)
        return None, None
    try:
        proc = subprocess.run(
            [str(TRIGGER_SCRIPT), cell],
            capture_output=True,
            text=True,
            timeout=CELL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        print(
            f"[soak] cell {cell!r} timed out after {CELL_TIMEOUT_S}s",
            file=sys.stderr,
        )
        # exc has .stdout and .stderr if any was buffered before the kill
        proc = None
        last_line = None
        return proc, last_line
    except Exception as exc:  # noqa: BLE001
        print(
            f"[soak] cell {cell!r} subprocess exception: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None, None

    last_line: str | None = None
    if proc.stdout:
        # Skip ANSI-coloring noise; the result line is plain text.
        for line in reversed(proc.stdout.splitlines()):
            if line and "|" in line and line.split("|", 1)[0] in CELLS:
                last_line = line.strip()
                break
    return proc, last_line


def _strip_ansi(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _interruptible_sleep(seconds: int) -> None:
    """Sleep but check stop_flag + STOP_SENTINEL every 5s so SIGTERM
    surfaces quickly."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _stop_flag or STOP_SENTINEL.exists():
            return
        time.sleep(min(5, max(0.1, end - time.monotonic())))


def _load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"[soak] state file corrupt ({exc}); starting from idx=0", file=sys.stderr)
    return {"next_idx": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Minimal soak runner for Phalanx CI Fixer v1.7.2.4."
    )
    ap.add_argument("--cadence-s", type=int, default=DEFAULT_CADENCE_S)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--max-runs", type=int, default=None)
    ap.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--pg-container", default=DEFAULT_PG_CONTAINER)
    ap.add_argument("--worker-container", default=DEFAULT_WORKER_CONTAINER)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = _load_state()
    print(
        f"[soak] start cadence={args.cadence_s}s out={args.out} "
        f"resume_idx={state['next_idx']} max_runs={args.max_runs}",
        file=sys.stderr,
        flush=True,
    )

    iter_count = 0
    while not _stop_flag:
        if STOP_SENTINEL.exists():
            print(f"[soak] {STOP_SENTINEL} present — exiting cleanly", file=sys.stderr)
            break
        if args.max_runs is not None and iter_count >= args.max_runs:
            print(f"[soak] reached --max-runs={args.max_runs}", file=sys.stderr)
            break

        cell = CELLS[state["next_idx"] % len(CELLS)]
        ts_start = datetime.now(timezone.utc)
        print(
            f"[soak] iter={iter_count} idx={state['next_idx']} cell={cell} "
            f"ts_start={ts_start.isoformat(timespec='seconds')}",
            file=sys.stderr,
            flush=True,
        )

        proc, last_line = _run_cell(cell)
        ts_end = datetime.now(timezone.utc)
        parsed = _parse_result_line(last_line) if last_line else None

        record = {
            "ts_start": ts_start.isoformat(timespec="seconds"),
            "ts_end": ts_end.isoformat(timespec="seconds"),
            "duration_s": int((ts_end - ts_start).total_seconds()),
            "cell": cell,
            "verdict": None,
            "run_id": None,
            "intro_sha": None,
            "head_sha": None,
            "iterations": None,
            "engineer_commit_sha": None,
            "verified_commit_sha": None,
            "sha_match": None,
            "last_fingerprint": None,
            "final_reason": None,
            "run_status": None,
            "gate_decision": None,
            "run_id_source": None,  # "stdout_line" | "db_fallback" | None
            "trigger_exit_code": getattr(proc, "returncode", None) if proc else None,
            "trigger_stderr_tail": _strip_ansi(
                (getattr(proc, "stderr", "") or "")[-500:]
            )
            if proc
            else None,
        }

        if parsed:
            record["verdict"] = parsed["verdict"]
            record["run_id"] = parsed["run_id"]
            record["intro_sha"] = parsed["intro_sha"]
            record["head_sha"] = parsed["head_sha"]
            if parsed["run_id"]:
                record["run_id_source"] = "stdout_line"
        else:
            # Fallback: parser couldn't extract a run_id from the script's
            # last line (script crash before final echo, ANSI noise,
            # regex drift). Look up the most recent run in the DB filtered
            # by cell branch + ts > ts_start. This restores observability
            # for a class of soak rows that would otherwise be `null`.
            if proc is not None:
                fallback_run_id = _query_recent_run_id_fallback(
                    start_ts=ts_start,
                    cell=cell,
                    ssh_key=args.ssh_key,
                    ssh_host=args.ssh_host,
                    pg_container=args.pg_container,
                )
                if fallback_run_id:
                    print(
                        f"[soak] iter={iter_count} stdout-parse failed; "
                        f"fallback found run_id={fallback_run_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                    record["run_id"] = fallback_run_id
                    record["run_id_source"] = "db_fallback"

        run_id = record["run_id"] or ""
        if run_id:
            metrics = _query_run_metrics(
                run_id, args.ssh_key, args.ssh_host, args.pg_container
            )
            record.update(
                {
                    k: v
                    for k, v in metrics.items()
                    if k
                    in {
                        "iterations",
                        "engineer_commit_sha",
                        "verified_commit_sha",
                        "last_fingerprint",
                        "final_reason",
                        "run_status",
                    }
                }
            )
            # If verdict is still None (fallback path didn't get one from
            # the stdout line), use run_status as the best available signal.
            if record["verdict"] is None and record["run_status"]:
                record["verdict"] = record["run_status"]
            record["sha_match"] = bool(
                record["engineer_commit_sha"]
                and record["engineer_commit_sha"] == record["verified_commit_sha"]
            )
            record["gate_decision"] = _fetch_gate_decision(
                run_id, args.ssh_key, args.ssh_host, args.worker_container
            )

        try:
            with args.out.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[soak] failed to write JSONL: {exc}", file=sys.stderr)

        # Compact stderr summary
        print(
            f"[soak] iter={iter_count} cell={cell} verdict={record['verdict']!s} "
            f"iters={record['iterations']!s} gate={record['gate_decision']!s} "
            f"sha_match={record['sha_match']!s} duration={record['duration_s']}s "
            f"run_id={record['run_id']}",
            file=sys.stderr,
            flush=True,
        )

        state["next_idx"] = state["next_idx"] + 1
        state["last_iter"] = iter_count
        state["last_run_id"] = record["run_id"]
        state["last_ts"] = ts_end.isoformat(timespec="seconds")
        _save_state(state)
        iter_count += 1

        if _stop_flag or (
            args.max_runs is not None and iter_count >= args.max_runs
        ):
            continue  # let the outer-loop check + exit cleanly

        # Sleep until next cadence boundary, accounting for variable cell duration.
        elapsed = int((datetime.now(timezone.utc) - ts_start).total_seconds())
        sleep_s = max(0, args.cadence_s - elapsed)
        if sleep_s > 0:
            print(f"[soak] sleeping {sleep_s}s until next iter", file=sys.stderr, flush=True)
            _interruptible_sleep(sleep_s)
        else:
            print(
                f"[soak] cell took {elapsed}s ≥ cadence {args.cadence_s}s; "
                "starting next iter immediately",
                file=sys.stderr,
                flush=True,
            )

    print(f"[soak] exited cleanly after {iter_count} runs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
