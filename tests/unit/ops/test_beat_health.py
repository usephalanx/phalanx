"""beat_health.sh — verify celery-beat is actually dispatching tasks.

Catches the 2026-05-20 silent-failure mode: forge-beat boots, prints its
banner, then crashes inside the scheduler init. No tasks fire, reconciler
never runs, ledger rows pile up PENDING. The check that catches this
is observing a "Sending due task" line in the beat log.

Tests use the PHALANX_TEST_BEAT_LOG env stub to drive the classification
without needing a live docker stack.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "beat_health.sh"


def _run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, env=env,
    )


class TestScriptShape:
    def test_executable(self):
        assert SCRIPT.exists()
        assert SCRIPT.stat().st_mode & 0o111

    def test_bash_parses(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


class TestStubbedClassification:
    """PHALANX_TEST_BEAT_LOG lets us exercise the success/failure paths
    without a live docker daemon."""

    def test_healthy_log_with_due_task_passes(self):
        """The post-fix 2026-05-20 shape: beat dispatches reconcile-shadow-ledger."""
        log = (
            "celery beat v5.6.3 is starting.\n"
            "Scheduler: Sending due task reconcile-shadow-ledger "
            "(phalanx.maintenance.ledger_reconciler.reconcile_shadow_ledger)\n"
        )
        r = _run({"PHALANX_TEST_BEAT_LOG": log})
        assert r.returncode == 0, r.stderr or r.stdout
        assert "firing tasks" in r.stdout
        assert "reconcile-shadow-ledger" in r.stdout

    def test_silent_failure_log_returns_2(self):
        """The 2026-05-20 pre-fix shape: banner present, no "Sending due task"
        ever appears because beat crashed inside scheduler init."""
        log = (
            "celery beat v5.6.3 is starting.\n"
            "ModuleNotFoundError: No module named 'django_celery_beat'\n"
        )
        r = _run({"PHALANX_TEST_BEAT_LOG": log})
        assert r.returncode == 2, (
            f"silent failure must exit 2 (overridable), got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )

    def test_empty_log_returns_2(self):
        r = _run({"PHALANX_TEST_BEAT_LOG": ""})
        assert r.returncode == 2

    def test_picks_latest_dispatch(self):
        """When multiple dispatch lines exist, the success message names the most recent."""
        log = (
            "Scheduler: Sending due task check-blocked-runs (phalanx.maintenance.tasks.check_blocked_runs)\n"
            "Scheduler: Sending due task reconcile-shadow-ledger "
            "(phalanx.maintenance.ledger_reconciler.reconcile_shadow_ledger)\n"
        )
        r = _run({"PHALANX_TEST_BEAT_LOG": log})
        assert r.returncode == 0
        assert "reconcile-shadow-ledger" in r.stdout


class TestEmergencyBypass:
    def test_skip_flag_short_circuits_exit_0(self):
        r = _run({"PHALANX_SKIP_BEAT_HEALTH": "1"})
        assert r.returncode == 0
        assert "skipped" in r.stderr or "skipped" in r.stdout


class TestMakefileWiring:
    def test_make_up_invokes_beat_health(self):
        mk = (REPO_ROOT / "Makefile").read_text()
        # The `up:` target must invoke beat_health.sh after compose up -d
        # so the silent-failure mode can't slip past `make up`.
        up_block_start = mk.find("\nup: preflight")
        assert up_block_start >= 0
        up_block_end = mk.find("\n\n", up_block_start)
        up_block = mk[up_block_start:up_block_end]
        assert "scripts/beat_health.sh" in up_block, (
            "make up must run beat_health.sh after docker compose up -d"
        )

    def test_make_beat_health_target_exists(self):
        mk = (REPO_ROOT / "Makefile").read_text()
        assert "\nbeat-health:" in mk
        assert "scripts/beat_health.sh" in mk

    def test_skip_env_var_documented_in_help(self):
        mk = (REPO_ROOT / "Makefile").read_text()
        assert "PHALANX_SKIP_BEAT_HEALTH" in mk


class TestRegressionGuardOn2026_05_20:
    """The 2026-05-20 silent-failure must trigger exit 2, never exit 0.
    Locks in the contract: a beat that has booted but never fires is
    treated as broken, not healthy."""

    def test_banner_only_log_fails(self):
        # Exact shape of the pre-fix log: banner emitted, error printed,
        # never reaches the "Sending due task" lines.
        log = """
celery beat v5.6.3 (recovery) is starting.
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/celery/beat.py", line 670, in get_scheduler
    return symbol_by_name(self.scheduler_cls, aliases=aliases)(
ModuleNotFoundError: No module named 'django_celery_beat'
"""
        r = _run({"PHALANX_TEST_BEAT_LOG": log})
        assert r.returncode == 2, "the exact 2026-05-20 log shape must not pass"
        assert "no \"Sending due task\"" in (r.stderr + r.stdout) or "no \\\"Sending due task\\\"" in (r.stderr + r.stdout)
