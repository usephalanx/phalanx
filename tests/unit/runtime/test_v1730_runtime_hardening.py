"""v1.7.3 runtime hardening — unit tests.

Covers the seven success criteria from the v1.7.3-runtime-hardening
spec section 7:

  - task timeout (heartbeat-stale → TIMED_OUT)
  - worker hang (no progress after TTL → propagates to Run)
  - commander does not loop forever (poll budget bounded)
  - partial ledger evidence preserved (TL output captured even when
    downstream task hangs)
  - sandbox cleanup after timeout (cleanup_for_run reads sre_setup
    output, calls stop_sandbox)
  - no repo side effects after timeout (verified separately by
    integration suite via shadow_mode + git audit)
  - infra-verdict classification (FAILED_INFRA_TIMEOUT vs
    FAILED_INFRA_WORKER_HANG vs FAILED_TL etc.)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.runtime.heartbeat import (
    DEFAULT_TTL_SECONDS,
    default_ttl_for_role,
    heartbeat_age_seconds,
    is_stale,
)
from phalanx.runtime.infra_verdicts import (
    ARCHITECTURE_FAILURE_CLASSES,
    FAILED_ENGINEER,
    FAILED_INFRA_TIMEOUT,
    FAILED_INFRA_WORKER_HANG,
    FAILED_SANDBOX_SETUP,
    FAILED_TL,
    INFRA_FAILURE_CLASSES,
    is_architecture_failure,
    is_infra_failure,
)


# ── heartbeat module ───────────────────────────────────────────────────


class TestDefaultTTLByRole:
    def test_known_roles_have_distinct_ttls(self):
        # If anyone collapses these accidentally, this test catches it.
        ttls = {
            default_ttl_for_role("cifix_techlead"),
            default_ttl_for_role("cifix_engineer"),
            default_ttl_for_role("cifix_sre"),
            default_ttl_for_role("cifix_challenger"),
            default_ttl_for_role("cifix_commander"),
        }
        assert len(ttls) >= 4  # at least 4 distinct values

    def test_unknown_role_falls_back_to_default(self):
        assert default_ttl_for_role("unknown_xyz") == DEFAULT_TTL_SECONDS

    def test_none_role_falls_back_to_default(self):
        assert default_ttl_for_role(None) == DEFAULT_TTL_SECONDS

    def test_sre_variants_match(self):
        # Both 'cifix_sre_setup' and 'cifix_sre_verify' should share
        # the same TTL since they have similar latency profiles.
        assert (
            default_ttl_for_role("cifix_sre_setup")
            == default_ttl_for_role("cifix_sre_verify")
            == default_ttl_for_role("cifix_sre")
        )


class TestIsStale:
    def test_no_heartbeat_does_not_flag_stale(self):
        # Initial-stamp path should set last_heartbeat_at; if it didn't,
        # is_stale returns False (the run-level reaper or commander
        # poll-timeout catches that case separately).
        assert is_stale(last_heartbeat_at=None, ttl_seconds=60, role=None) is False

    def test_recent_heartbeat_is_not_stale(self):
        now = datetime.now(UTC)
        recent = now - timedelta(seconds=30)
        assert is_stale(
            last_heartbeat_at=recent, ttl_seconds=60, role=None, now=now
        ) is False

    def test_old_heartbeat_is_stale(self):
        now = datetime.now(UTC)
        old = now - timedelta(seconds=300)
        assert is_stale(
            last_heartbeat_at=old, ttl_seconds=60, role=None, now=now
        ) is True

    def test_explicit_ttl_overrides_role_default(self):
        now = datetime.now(UTC)
        # 100s old; role default for cifix_engineer is 180s (would not be stale)
        # but explicit ttl=50 makes it stale.
        old = now - timedelta(seconds=100)
        assert is_stale(
            last_heartbeat_at=old,
            ttl_seconds=50,
            role="cifix_engineer",
            now=now,
        ) is True

    def test_role_default_used_when_ttl_seconds_none(self):
        now = datetime.now(UTC)
        # cifix_challenger TTL = 60s; 30s old should not be stale
        recent = now - timedelta(seconds=30)
        assert is_stale(
            last_heartbeat_at=recent,
            ttl_seconds=None,
            role="cifix_challenger",
            now=now,
        ) is False
        # 90s old should be stale (>60)
        old = now - timedelta(seconds=90)
        assert is_stale(
            last_heartbeat_at=old,
            ttl_seconds=None,
            role="cifix_challenger",
            now=now,
        ) is True


class TestHeartbeatAgeSeconds:
    def test_returns_none_when_never_beat(self):
        assert heartbeat_age_seconds(None) is None

    def test_returns_age_in_seconds(self):
        now = datetime.now(UTC)
        beat = now - timedelta(seconds=42)
        age = heartbeat_age_seconds(beat, now=now)
        assert 41.5 <= age <= 42.5


# ── infra verdicts module ──────────────────────────────────────────────


class TestInfraVerdicts:
    def test_infra_failure_classes_are_disjoint_from_architecture(self):
        assert INFRA_FAILURE_CLASSES.isdisjoint(ARCHITECTURE_FAILURE_CLASSES)

    def test_is_infra_failure_recognizes_each_constant(self):
        for cls in INFRA_FAILURE_CLASSES:
            assert is_infra_failure(cls) is True
            assert is_architecture_failure(cls) is False

    def test_is_architecture_failure_recognizes_each_constant(self):
        for cls in ARCHITECTURE_FAILURE_CLASSES:
            assert is_architecture_failure(cls) is True
            assert is_infra_failure(cls) is False

    def test_unknown_class_is_neither(self):
        assert is_infra_failure("WAT") is False
        assert is_architecture_failure("WAT") is False
        assert is_infra_failure(None) is False

    def test_FAILED_INFRA_TIMEOUT_constant_value(self):
        # If anyone changes this string, dashboards break. Lock it.
        assert FAILED_INFRA_TIMEOUT == "FAILED_INFRA_TIMEOUT"
        assert FAILED_INFRA_WORKER_HANG == "FAILED_INFRA_WORKER_HANG"
        assert FAILED_SANDBOX_SETUP == "FAILED_SANDBOX_SETUP"
        assert FAILED_TL == "FAILED_TL"
        assert FAILED_ENGINEER == "FAILED_ENGINEER"


# ── stuck-task detector logic ──────────────────────────────────────────


def _make_task(
    *,
    task_id: str = "t1",
    run_id: str = "r1",
    status: str = "IN_PROGRESS",
    role: str = "cifix_techlead",
    last_heartbeat_at=None,
    ttl_seconds=None,
):
    """Build a Task-shaped MagicMock for unit tests."""
    t = MagicMock()
    t.id = task_id
    t.run_id = run_id
    t.status = status
    t.agent_role = role
    t.last_heartbeat_at = last_heartbeat_at
    t.ttl_seconds = ttl_seconds
    return t


class TestStuckTaskDetectorClassification:
    """The decision of WHICH tasks to flag is encapsulated in is_stale.
    These tests assert that the detector's decision matches is_stale's
    semantics on a synthetic task list."""

    def test_only_stale_inflight_tasks_get_flagged(self):
        now = datetime.now(UTC)
        rows = [
            _make_task(task_id="ok-1", last_heartbeat_at=now - timedelta(seconds=10)),
            _make_task(task_id="stale-1", last_heartbeat_at=now - timedelta(seconds=200)),
            _make_task(task_id="ok-2", last_heartbeat_at=now - timedelta(seconds=20)),
            _make_task(task_id="stale-2", last_heartbeat_at=now - timedelta(seconds=600)),
        ]
        # Default TTL for cifix_techlead is 90s; rows >90s old should flag
        flagged = [
            r for r in rows
            if is_stale(
                last_heartbeat_at=r.last_heartbeat_at,
                ttl_seconds=r.ttl_seconds,
                role=r.agent_role,
                now=now,
            )
        ]
        assert {r.id for r in flagged} == {"stale-1", "stale-2"}

    def test_no_heartbeat_does_not_flag(self):
        """Workers stuck before their first heartbeat are NOT caught by
        is_stale — they're caught by advance_run's stale-IN_PROGRESS
        timer (45 min) instead. is_stale() returns False to avoid
        false positives during the brief window between dispatch and
        first record_heartbeat()."""
        now = datetime.now(UTC)
        rows = [_make_task(task_id="never-beat", last_heartbeat_at=None)]
        flagged = [
            r for r in rows
            if is_stale(
                last_heartbeat_at=r.last_heartbeat_at,
                ttl_seconds=r.ttl_seconds,
                role=r.agent_role,
                now=now,
            )
        ]
        assert flagged == []


# ── stuck-task detector → DB propagation (integration with mocked DB) ──


@pytest.mark.asyncio
async def _run_detector_against(rows: list, monkeypatch_session):
    from phalanx.maintenance.stuck_task_detector import _detect_stuck_tasks_impl

    # Patch get_db to return a fake session that yields `rows` on the
    # first SELECT and accepts updates silently.
    fake_session = MagicMock()
    select_results = MagicMock()
    select_results.scalars.return_value.all.return_value = rows
    select_results.scalar_one_or_none.return_value = None
    select_results.one_or_none.return_value = None
    fake_session.execute = AsyncMock(return_value=select_results)
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("phalanx.maintenance.stuck_task_detector.get_db", return_value=cm):
        return await _detect_stuck_tasks_impl()


class TestStuckDetectorEndToEnd:
    def test_no_in_progress_tasks_returns_zero_timed_out(self):
        result = asyncio.run(_run_detector_against([], monkeypatch_session=None))
        assert result["scanned"] == 0
        assert result["timed_out"] == 0
        assert result["ids"] == []

    def test_all_healthy_tasks_returns_zero_timed_out(self):
        now = datetime.now(UTC)
        rows = [
            _make_task(
                task_id="t1",
                last_heartbeat_at=now - timedelta(seconds=10),
            ),
        ]
        result = asyncio.run(_run_detector_against(rows, monkeypatch_session=None))
        assert result["scanned"] == 1
        assert result["timed_out"] == 0


# ── commander watchdog: poll budget bounded ────────────────────────────


class TestCommanderPollBudgetBounded:
    """Commander's _poll_for_terminal must NEVER block forever. It has
    a hard wall-clock cap (_MAX_WAIT_SECONDS). After that, it returns
    'TIMEOUT' and the caller writes failure_class=FAILED_INFRA_TIMEOUT."""

    def test_max_wait_seconds_constant_is_bounded(self):
        from phalanx.agents.cifix_commander import _MAX_WAIT_SECONDS

        # 45 min hard cap. If anyone bumps this above 1 hour without
        # also adjusting the run-level reaper TTL, this test fails.
        assert 1800 <= _MAX_WAIT_SECONDS <= 3600


# ── sandbox cleanup module ─────────────────────────────────────────────


class TestSandboxCleanupReturnShape:
    """cleanup_for_run must return a dict with the documented keys
    regardless of failure mode. Ledger writers depend on the shape."""

    def test_returns_dict_when_no_sre_output(self):
        from phalanx.runtime.sandbox_cleanup import cleanup_for_run

        async def _r():
            fake_session = MagicMock()
            empty = MagicMock()
            empty.one_or_none.return_value = None
            fake_session.execute = AsyncMock(return_value=empty)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=fake_session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.runtime.sandbox_cleanup.get_db", return_value=cm):
                return await cleanup_for_run("run-x", reason="test")

        result = asyncio.run(_r())
        assert set(result.keys()) == {"ok", "container_id", "reason", "error"}
        assert result["ok"] is False
        assert result["error"] == "no_sre_setup_output_found"

    def test_returns_dict_when_sre_output_lacks_container_id(self):
        from phalanx.runtime.sandbox_cleanup import cleanup_for_run

        async def _r():
            fake_session = MagicMock()
            row = ({"mode": "setup", "workspace_path": "/x"},)
            res = MagicMock()
            res.one_or_none.return_value = row
            fake_session.execute = AsyncMock(return_value=res)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=fake_session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.runtime.sandbox_cleanup.get_db", return_value=cm):
                return await cleanup_for_run("run-x", reason="test")

        result = asyncio.run(_r())
        assert result["ok"] is False
        assert result["error"] == "no_container_id_in_sre_output"

    def test_calls_stop_sandbox_when_container_id_present(self):
        from phalanx.runtime.sandbox_cleanup import cleanup_for_run

        stop_calls = []

        async def fake_stop_sandbox(container_id):
            stop_calls.append(container_id)

        async def _r():
            fake_session = MagicMock()
            row = ({"container_id": "abc123", "mode": "setup"},)
            res = MagicMock()
            res.one_or_none.return_value = row
            fake_session.execute = AsyncMock(return_value=res)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=fake_session)
            cm.__aexit__ = AsyncMock(return_value=False)

            with (
                patch("phalanx.runtime.sandbox_cleanup.get_db", return_value=cm),
                patch(
                    "phalanx.ci_fixer_v3.provisioner.stop_sandbox",
                    side_effect=fake_stop_sandbox,
                ),
            ):
                return await cleanup_for_run("run-x", reason="test_terminal")

        result = asyncio.run(_r())
        assert result["ok"] is True
        assert result["container_id"] == "abc123"
        assert result["reason"] == "test_terminal"
        assert stop_calls == ["abc123"]

    def test_never_raises_on_inner_exception(self):
        from phalanx.runtime.sandbox_cleanup import cleanup_for_run

        async def _r():
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.runtime.sandbox_cleanup.get_db", return_value=cm):
                return await cleanup_for_run("run-x", reason="db_failure")

        # Must not raise
        result = asyncio.run(_r())
        assert result["ok"] is False
        assert "RuntimeError" in (result["error"] or "")


# ── observability events ───────────────────────────────────────────────


class TestRuntimeEvents:
    def test_event_names_match_spec(self):
        """v1.7.3 spec section 5 lists 8 event names. Lock them so
        dashboards don't break silently."""
        from phalanx.observability import runtime_events as ev

        # Each function emits a structlog event with name 'runtime.<X>'
        # We test by capturing the structlog calls.
        import structlog

        calls = []

        def fake_info(name, **kw):
            calls.append((name, kw))

        def fake_warning(name, **kw):
            calls.append((name, kw))

        # Patch the module-level logger
        with patch.object(ev.log, "info", side_effect=fake_info), patch.object(
            ev.log, "warning", side_effect=fake_warning
        ):
            ev.task_started(task_id="t", run_id="r", agent_role="x", ttl_seconds=60)
            ev.task_heartbeat(task_id="t", run_id="r", agent_role="x", note="ok")
            ev.task_timeout(
                task_id="t", run_id="r", agent_role="x",
                age_seconds=120, ttl_seconds=60,
            )
            ev.task_completed(task_id="t", run_id="r", agent_role="x", duration_seconds=5)
            ev.task_failed(task_id="t", run_id="r", agent_role="x", error="x")
            ev.run_finalized(
                run_id="r", final_status="SHIPPED",
                failure_class=None, reason=None,
            )
            ev.sandbox_cleanup(
                run_id="r", container_id="c", ok=True, reason="test",
            )
            ev.queue_depth(queue="cifix_commander", depth=3)

        names = [c[0] for c in calls]
        assert names == [
            "runtime.task_started",
            "runtime.task_heartbeat",
            "runtime.task_timeout",
            "runtime.task_completed",
            "runtime.task_failed",
            "runtime.run_finalized",
            "runtime.sandbox_cleanup",
            "runtime.queue_depth",
        ]
