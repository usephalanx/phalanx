"""Age-based sandbox reaper — the backstop `sandbox_cleanup` cannot be.

`sandbox_cleanup.cleanup_for_run` is per-run and RECORD-DRIVEN: it looks up the
run's SRE task, reads `container_id` out of its output JSONB, and stops that one
container. That covers every path where the run reaches a terminal state with
its task row intact.

It cannot cover the path that actually leaks: a worker dies, or the commander
crashes, BEFORE the container id is written. There is then no record pointing at
the container, so nothing will ever reap it. On 2026-08-10 that had accumulated
481 orphaned containers on prod — the oldest 113 days — leaving 160 MB of 3.8 GB
free. Memory starvation is exactly how FAILED_INFRA_WORKER_HANG manifests, which
was ~40% of the last shadow attempts, so the leak feeds the failure that causes
the leak.

This module closes the loop from the other side: sweep by AGE and NAME, with no
reference to any database record. If it is a `cifix-v3-*` container and it is
older than the max possible run lifetime, it is garbage by definition — the
per-task probe timeout is 900 s and the commander's wall-clock cap is 1800 s, so
nothing legitimate survives hours.

Safety properties:
  - Name-scoped: only containers matching the provisioner's `cifix-v3-<hex>`
    naming (provisioner._docker_run_detached). Never `phalanx-prod-*`, never a
    demo, never anything it did not create.
  - Age-gated: a generous default floor, far above any legal run lifetime.
  - Never raises. A reaper that can break the worker is worse than a leak.
  - Reports what it removed so the sweep is auditable, not silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta

import structlog

from phalanx.config.settings import get_settings
from phalanx.observability import runtime_events
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# Matches provisioner._docker_run_detached: f"cifix-v3-{uuid4().hex[:8]}".
_SANDBOX_NAME_RE = re.compile(r"^cifix-v3-[0-9a-f]{8}$")

# Longest a legitimate sandbox can live. Commander's wall-clock cap is 1800 s
# and run_probe's hard time limit is 960 s; 6 h is ~12x the worst legal case, so
# a container past it cannot belong to a live run under any scheduling delay.
_DEFAULT_MAX_AGE_HOURS = 6

# Bound the blast radius of a single sweep. If there are ever more than this
# many orphans, something is badly wrong and we want the next sweep to pick up
# the rest rather than spend unbounded time inside one beat tick.
_MAX_REAPED_PER_SWEEP = 200

_DOCKER_TIMEOUT_S = 30


async def _docker(*args: str, timeout_s: int = _DOCKER_TIMEOUT_S) -> tuple[int, str]:
    """Run a docker command. Returns (exit_code, stdout). Never raises."""
    docker_cmd = get_settings().sandbox_docker_cmd
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode if proc.returncode is not None else -1, stdout.decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 — a broken docker CLI must not break the beat
        log.warning("sandbox_reaper.docker_failed", args=args[:2], error=str(exc))
        return -1, ""


def _parse_created(value: str) -> datetime | None:
    """Parse docker's CreatedAt (`2026-08-10 03:40:30 +0000 UTC`). None if unparseable."""
    try:
        return datetime.strptime(value.strip()[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except Exception:  # noqa: BLE001
        return None


async def find_orphans(max_age_hours: int = _DEFAULT_MAX_AGE_HOURS) -> list[dict]:
    """Return sandbox containers older than `max_age_hours`, newest-first order
    not guaranteed. Read-only — this never removes anything."""
    code, out = await _docker(
        "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.CreatedAt}}"
    )
    if code != 0:
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    orphans: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, name, created_raw = parts[0].strip(), parts[1].strip(), parts[2]
        # Name gate FIRST — nothing outside our own naming is ever a candidate.
        if not _SANDBOX_NAME_RE.match(name):
            continue
        created = _parse_created(created_raw)
        if created is None:
            # Unparseable timestamp: refuse to guess. Leaving a container is
            # always safer than removing one we cannot date.
            log.warning("sandbox_reaper.unparseable_created", name=name, raw=created_raw)
            continue
        if created < cutoff:
            orphans.append({
                "id": cid,
                "name": name,
                "age_hours": round((datetime.now(UTC) - created).total_seconds() / 3600, 1),
            })
    return orphans


async def reap_orphans(
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
    *,
    dry_run: bool = False,
    limit: int = _MAX_REAPED_PER_SWEEP,
) -> dict:
    """Remove orphaned sandbox containers. Returns an audit summary. Never raises."""
    orphans = await find_orphans(max_age_hours)
    selected = orphans[:limit]
    reaped: list[str] = []
    failed: list[str] = []

    if not dry_run:
        for o in selected:
            code, _ = await _docker("rm", "-f", o["id"])
            (reaped if code == 0 else failed).append(o["name"])

    summary = {
        "found": len(orphans),
        "selected": len(selected),
        "reaped": len(reaped),
        "failed": len(failed),
        "truncated": len(orphans) > len(selected),
        "max_age_hours": max_age_hours,
        "dry_run": dry_run,
        "oldest_age_hours": max((o["age_hours"] for o in orphans), default=0),
        "names": [o["name"] for o in selected][:20],
    }
    if orphans:
        # Only speak up when there is something to say — a quiet sweep should
        # stay quiet, but a leak should be visible without grepping.
        log.info("sandbox_reaper.swept", **summary)
    return summary


@celery_app.task(
    name="phalanx.runtime.sandbox_reaper.reap_sandboxes",
    queue="cifix_sre",  # the socket-having worker; the API container has no Docker
    max_retries=0,
    soft_time_limit=240,
    time_limit=300,
)
def reap_sandboxes(max_age_hours: int = _DEFAULT_MAX_AGE_HOURS, dry_run: bool = False) -> dict:
    """Beat-scheduled sweep. Never raises — a failed sweep logs and returns."""
    try:
        summary = asyncio.run(reap_orphans(max_age_hours, dry_run=dry_run))
        if summary.get("reaped"):
            # Reuse the existing sandbox_cleanup event so reaped containers land
            # in the same log stream operators already watch. run_id is None by
            # construction — the whole point is that no record pointed here.
            # Telemetry must never fail the sweep.
            with contextlib.suppress(Exception):
                runtime_events.sandbox_cleanup(
                    run_id=None,
                    container_id=None,
                    ok=True,
                    reason=(
                        f"age_reaper: removed {summary['reaped']} orphan(s) older than "
                        f"{max_age_hours}h (oldest {summary['oldest_age_hours']}h)"
                    ),
                )
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("sandbox_reaper.failed", error=str(exc))
        return {"found": 0, "reaped": 0, "failed": 0, "error": f"{type(exc).__name__}: {exc}"}
