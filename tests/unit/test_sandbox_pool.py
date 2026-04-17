"""
Tests for phalanx.ci_fixer.sandbox_pool — SandboxPool, PooledContainer,
get_sandbox_pool, wrap_cmd_for_container, wrap_shell_cmd_for_container.

Coverage targets:
  - SandboxPool._warmup(): min_size=0 (skip), min_size>0 (starts containers)
  - SandboxPool.checkout(): happy path, timeout, health check fail + retry
  - SandboxPool.checkin(): reset ok → re-enqueue; reset fail → replace; unhealthy after reset → replace
  - SandboxPool.borrow(): context manager guarantees checkin on raise
  - SandboxPool.shutdown(): drains queues, kills checked-out containers
  - SandboxPool._reaper_loop(): kills stale checked-out containers
  - SandboxPool._resolve_image(): preferred present → preferred; preferred absent → fallback
  - SandboxPool._start_and_enqueue(): pool full → kills extra container
  - SandboxUnavailableError raised when pool for unknown stack
  - get_sandbox_pool(): lazy singleton, returns same instance on repeat calls
  - reset_pool_for_testing(): clears singleton
  - wrap_cmd_for_container(): correct docker exec prefix
  - wrap_shell_cmd_for_container(): correct sh -c wrapping
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.sandbox_pool import (
    PooledContainer,
    SandboxPool,
    SandboxUnavailableError,
    get_sandbox_pool,
    reset_pool_for_testing,
    wrap_cmd_for_container,
    wrap_shell_cmd_for_container,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_container(
    container_id: str = "abc123",
    stack: str = "python",
    image: str = "phalanx-sandbox-python:latest",
    checked_out_seconds_ago: float = 0,
) -> PooledContainer:
    c = PooledContainer(container_id=container_id, stack=stack, image=image)
    c.checked_out_at = time.monotonic() - checked_out_seconds_ago
    return c


def _make_proc(returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _mock_settings(
    min_size: int = 1,
    max_size: int = 2,
    checkout_timeout: int = 5,
    max_hold: int = 300,
    reaper_interval: int = 60,
    docker_cmd: str = "docker",
):
    s = MagicMock()
    s.sandbox_pool_min_size = min_size
    s.sandbox_pool_max_size = max_size
    s.sandbox_checkout_timeout_seconds = checkout_timeout
    s.sandbox_max_hold_seconds = max_hold
    s.sandbox_reaper_interval_seconds = reaper_interval
    s.sandbox_docker_cmd = docker_cmd
    return s


# ── wrap helpers ──────────────────────────────────────────────────────────────


class TestWrapHelpers:
    def test_wrap_cmd_for_container(self):
        result = wrap_cmd_for_container("ctr123", ["ruff", "check", "."], "/workspace")
        assert result == ["docker", "exec", "-w", "/workspace", "ctr123", "ruff", "check", "."]

    def test_wrap_cmd_custom_docker_cmd(self):
        result = wrap_cmd_for_container("ctr123", ["go", "test", "./..."], "/ws", docker_cmd="podman")
        assert result[0] == "podman"
        assert "ctr123" in result

    def test_wrap_shell_cmd_for_container(self):
        result = wrap_shell_cmd_for_container("ctr123", "ruff check .")
        assert result == ["docker", "exec", "-w", "/workspace", "ctr123", "sh", "-c", "ruff check ."]

    def test_wrap_shell_cmd_custom_docker(self):
        result = wrap_shell_cmd_for_container("ctr456", "npm test", docker_cmd="podman")
        assert result[0] == "podman"
        assert "sh" in result
        assert "npm test" in result


# ── PooledContainer ───────────────────────────────────────────────────────────


class TestPooledContainer:
    def test_defaults(self):
        c = PooledContainer(container_id="abc", stack="python", image="img:latest")
        assert c.healthy is True
        assert c.container_id == "abc"
        assert isinstance(c.checked_out_at, float)

    def test_fields(self):
        c = _make_container(container_id="xyz", stack="go", image="golang:1.22-alpine")
        assert c.stack == "go"
        assert c.image == "golang:1.22-alpine"


# ── SandboxPool._warmup ───────────────────────────────────────────────────────


class TestSandboxPoolWarmup:
    @pytest.mark.asyncio
    async def test_warmup_min_size_zero_skips(self):
        """min_size=0 → no containers started, queues initialised empty."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        assert "python" in pool._queues
        assert pool._queues["python"].qsize() == 0
        assert pool._reaper_task is None

    @pytest.mark.asyncio
    async def test_warmup_starts_containers(self):
        """min_size=1 → _start_and_enqueue called for each stack."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=1, max_size=2)

        start_calls = []

        async def fake_start_and_enqueue(stack):
            container = _make_container(container_id=f"ctr-{stack}", stack=stack)
            await pool._queues[stack].put(container)
            start_calls.append(stack)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_start_and_enqueue", side_effect=fake_start_and_enqueue):
                with patch.object(pool, "_reaper_loop", new_callable=AsyncMock):
                    await pool._warmup()

        assert len(start_calls) >= 1
        # Reaper task should have been created
        assert pool._reaper_task is not None
        pool._reaper_task.cancel()

    @pytest.mark.asyncio
    async def test_warmup_errors_swallowed(self):
        """Errors during warmup don't raise — pool starts empty."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=1)

        async def failing_start(stack):
            raise RuntimeError("docker not found")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_start_and_enqueue", side_effect=failing_start):
                with patch.object(pool, "_reaper_loop", new_callable=AsyncMock):
                    await pool._warmup()  # should not raise

        # Queues exist but are empty
        assert pool._queues["python"].qsize() == 0


# ── SandboxPool.checkout ──────────────────────────────────────────────────────


class TestSandboxPoolCheckout:
    @pytest.mark.asyncio
    async def test_checkout_happy_path(self):
        """Container in queue → returned immediately, removed from queue."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, checkout_timeout=5)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container(container_id="ctr1", stack="python")
        await pool._queues["python"].put(container)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_health_check", return_value=True):
                with patch.object(pool, "_refill", new_callable=AsyncMock):
                    result = await pool.checkout("python", timeout=5)

        assert result.container_id == "ctr1"
        assert "ctr1" in pool._checked_out

    @pytest.mark.asyncio
    async def test_checkout_timeout_raises(self):
        """Empty queue + short timeout → SandboxUnavailableError."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with pytest.raises(SandboxUnavailableError):
                await pool.checkout("python", timeout=1)

    @pytest.mark.asyncio
    async def test_checkout_unknown_stack_raises(self):
        """Stack not in pool → SandboxUnavailableError immediately."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()
            with pytest.raises(SandboxUnavailableError, match="no pool"):
                await pool.checkout("cobol", timeout=1)

    @pytest.mark.asyncio
    async def test_checkout_unhealthy_container_triggers_retry(self):
        """Unhealthy container is killed, fresh one started, retry succeeds."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, checkout_timeout=5)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        bad = _make_container("bad-ctr", "python")
        good = _make_container("good-ctr", "python")
        await pool._queues["python"].put(bad)

        health_calls = []

        async def fake_health(c):
            health_calls.append(c.container_id)
            return c.container_id == "good-ctr"

        async def fake_kill(cid):
            pass

        async def fake_start_enqueue(stack):
            await pool._queues[stack].put(good)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_health_check", side_effect=fake_health):
                with patch.object(pool, "_kill_container", side_effect=fake_kill):
                    with patch.object(pool, "_start_and_enqueue", side_effect=fake_start_enqueue):
                        with patch.object(pool, "_refill", new_callable=AsyncMock):
                            result = await pool.checkout("python", timeout=5)

        assert result.container_id == "good-ctr"
        assert "bad-ctr" in health_calls

    @pytest.mark.asyncio
    async def test_checkout_refill_triggered(self):
        """checkout() triggers _refill as a background task that eventually runs."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("ctr1", "go")
        await pool._queues["go"].put(container)

        refill_calls = []
        refill_event = asyncio.Event()

        async def fake_refill(stack):
            refill_calls.append(stack)
            refill_event.set()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_health_check", return_value=True):
                with patch.object(pool, "_refill", side_effect=fake_refill):
                    await pool.checkout("go", timeout=5)
                    # Give the background task a chance to run
                    await asyncio.wait_for(refill_event.wait(), timeout=2)

        assert "go" in refill_calls


# ── SandboxPool.checkin ───────────────────────────────────────────────────────


class TestSandboxPoolCheckin:
    @pytest.mark.asyncio
    async def test_checkin_re_enqueues_after_reset(self):
        """Reset succeeds + health ok → container back in queue."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("ctr1", "python")
        pool._checked_out["ctr1"] = container

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_reset_container", return_value=True):
                with patch.object(pool, "_health_check", return_value=True):
                    await pool.checkin(container)

        assert pool._queues["python"].qsize() == 1
        assert "ctr1" not in pool._checked_out

    @pytest.mark.asyncio
    async def test_checkin_reset_fails_replaces_container(self):
        """Reset fails → container killed, new one started asynchronously."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("bad-ctr", "python")
        pool._checked_out["bad-ctr"] = container

        kill_calls = []
        start_calls = []
        start_event = asyncio.Event()

        async def fake_kill(cid):
            kill_calls.append(cid)

        async def fake_start(stack):
            start_calls.append(stack)
            start_event.set()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_reset_container", return_value=False):
                with patch.object(pool, "_kill_container", side_effect=fake_kill):
                    with patch.object(pool, "_start_and_enqueue", side_effect=fake_start):
                        await pool.checkin(container)
                        await asyncio.wait_for(start_event.wait(), timeout=2)

        assert "bad-ctr" in kill_calls
        assert "python" in start_calls
        assert pool._queues["python"].qsize() == 0  # no re-enqueue

    @pytest.mark.asyncio
    async def test_checkin_unhealthy_after_reset_replaces(self):
        """Reset ok but health check fails → kill and replace."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("sick-ctr", "go")
        pool._checked_out["sick-ctr"] = container

        kill_calls = []
        start_calls = []
        start_event = asyncio.Event()

        async def fake_kill(cid):
            kill_calls.append(cid)

        async def fake_start(stack):
            start_calls.append(stack)
            start_event.set()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_reset_container", return_value=True):
                with patch.object(pool, "_health_check", return_value=False):
                    with patch.object(pool, "_kill_container", side_effect=fake_kill):
                        with patch.object(pool, "_start_and_enqueue", side_effect=fake_start):
                            await pool.checkin(container)
                            await asyncio.wait_for(start_event.wait(), timeout=2)

        assert "sick-ctr" in kill_calls
        assert pool._queues["go"].qsize() == 0

    @pytest.mark.asyncio
    async def test_checkin_during_shutdown_kills_container(self):
        """When pool is shutting down, checked-in container is killed not re-queued."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        pool._shutdown = True
        container = _make_container("ctr1", "python")

        kill_calls = []

        async def fake_kill(cid):
            kill_calls.append(cid)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_kill_container", side_effect=fake_kill):
                await pool.checkin(container)

        assert "ctr1" in kill_calls
        assert pool._queues["python"].qsize() == 0


# ── SandboxPool.borrow ────────────────────────────────────────────────────────


class TestSandboxPoolBorrow:
    @pytest.mark.asyncio
    async def test_borrow_checks_in_on_success(self):
        """borrow() context manager checks container back in after normal exit."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("ctr1", "python")
        await pool._queues["python"].put(container)

        checkin_calls = []

        async def fake_checkin(c):
            checkin_calls.append(c.container_id)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_health_check", return_value=True):
                with patch.object(pool, "_refill", new_callable=AsyncMock):
                    with patch.object(pool, "checkin", side_effect=fake_checkin):
                        async with pool.borrow("python", timeout=5) as borrowed:
                            assert borrowed.container_id == "ctr1"

        assert "ctr1" in checkin_calls

    @pytest.mark.asyncio
    async def test_borrow_checks_in_on_exception(self):
        """borrow() guarantees checkin even when the body raises."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("ctr1", "python")
        await pool._queues["python"].put(container)

        checkin_calls = []

        async def fake_checkin(c):
            checkin_calls.append(c.container_id)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_health_check", return_value=True):
                with patch.object(pool, "_refill", new_callable=AsyncMock):
                    with patch.object(pool, "checkin", side_effect=fake_checkin):
                        with pytest.raises(ValueError):
                            async with pool.borrow("python", timeout=5):
                                raise ValueError("fix run crashed")

        assert "ctr1" in checkin_calls


# ── SandboxPool.shutdown ──────────────────────────────────────────────────────


class TestSandboxPoolShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_kills_queued_containers(self):
        """shutdown() kills all containers in queues."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        c1 = _make_container("ctr1", "python")
        c2 = _make_container("ctr2", "go")
        await pool._queues["python"].put(c1)
        await pool._queues["go"].put(c2)

        kill_calls = []

        async def fake_kill(cid):
            kill_calls.append(cid)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_kill_container", side_effect=fake_kill):
                await pool.shutdown()

        assert "ctr1" in kill_calls
        assert "ctr2" in kill_calls

    @pytest.mark.asyncio
    async def test_shutdown_kills_checked_out_containers(self):
        """shutdown() also kills containers currently checked out."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        container = _make_container("live-ctr", "rust")
        pool._checked_out["live-ctr"] = container

        kill_calls = []

        async def fake_kill(cid):
            kill_calls.append(cid)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_kill_container", side_effect=fake_kill):
                await pool.shutdown()

        assert "live-ctr" in kill_calls


# ── SandboxPool._reaper_loop ──────────────────────────────────────────────────


class TestSandboxPoolReaper:
    @pytest.mark.asyncio
    async def test_reaper_kills_stale_container(self):
        """Container checked out > max_hold_seconds → reaped and replaced."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, max_hold=10, reaper_interval=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        stale = _make_container("stale-ctr", "python", checked_out_seconds_ago=20)
        pool._checked_out["stale-ctr"] = stale

        kill_calls = []
        start_calls = []
        done_event = asyncio.Event()

        async def fake_sleep(secs):
            pass  # instant

        async def fake_kill(cid):
            kill_calls.append(cid)

        async def fake_start(stack):
            start_calls.append(stack)
            pool._shutdown = True  # stop loop after this iteration
            done_event.set()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_kill_container", side_effect=fake_kill):
                with patch.object(pool, "_start_and_enqueue", side_effect=fake_start):
                    with patch("asyncio.sleep", side_effect=fake_sleep):
                        task = asyncio.create_task(pool._reaper_loop())
                        await asyncio.wait_for(done_event.wait(), timeout=5)
                        await task

        assert "stale-ctr" in kill_calls
        assert "python" in start_calls

    @pytest.mark.asyncio
    async def test_reaper_leaves_fresh_container_alone(self):
        """Container checked out recently → not reaped."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, max_hold=300, reaper_interval=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        fresh = _make_container("fresh-ctr", "python", checked_out_seconds_ago=5)
        pool._checked_out["fresh-ctr"] = fresh

        kill_calls = []
        slept = asyncio.Event()

        async def fake_sleep(secs):
            slept.set()
            pool._shutdown = True  # stop after first iteration

        async def fake_kill(cid):
            kill_calls.append(cid)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_kill_container", side_effect=fake_kill):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    task = asyncio.create_task(pool._reaper_loop())
                    await asyncio.wait_for(slept.wait(), timeout=5)
                    await task

        assert "fresh-ctr" not in kill_calls

    @pytest.mark.asyncio
    async def test_reaper_stops_on_cancelled(self):
        """CancelledError exits the loop cleanly."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, reaper_interval=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        async def raise_cancel(secs):
            raise asyncio.CancelledError()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.sleep", side_effect=raise_cancel):
                await pool._reaper_loop()  # should return cleanly, not propagate


# ── SandboxPool._resolve_image ────────────────────────────────────────────────


class TestResolveImage:
    @pytest.mark.asyncio
    async def test_preferred_image_present(self):
        """docker image inspect returns 0 → preferred image used."""
        pool = SandboxPool()
        mock_settings = _mock_settings()
        proc = _make_proc(returncode=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._resolve_image("python")

        assert result == "phalanx-sandbox-python:latest"

    @pytest.mark.asyncio
    async def test_preferred_image_absent_uses_fallback(self):
        """docker image inspect returns non-zero → fallback image used."""
        pool = SandboxPool()
        mock_settings = _mock_settings()
        proc = _make_proc(returncode=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._resolve_image("python")

        assert result == "python:3.12-slim"

    @pytest.mark.asyncio
    async def test_unknown_stack_returns_ubuntu(self):
        """Unknown stack → ubuntu:22.04 fallback."""
        pool = SandboxPool()
        mock_settings = _mock_settings()
        proc = _make_proc(returncode=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._resolve_image("unknown")

        assert result == "ubuntu:22.04"


# ── SandboxPool._start_and_enqueue ───────────────────────────────────────────


class TestStartAndEnqueue:
    @pytest.mark.asyncio
    async def test_enqueues_when_pool_not_full(self):
        """Container started + pool has room → added to queue."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, max_size=2)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        async def fake_start(stack):
            return "new-ctr"

        async def fake_resolve(stack):
            return "phalanx-sandbox-python:latest"

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_start_container", side_effect=fake_start):
                with patch.object(pool, "_resolve_image", side_effect=fake_resolve):
                    await pool._start_and_enqueue("python")

        assert pool._queues["python"].qsize() == 1

    @pytest.mark.asyncio
    async def test_kills_extra_when_pool_full(self):
        """Container started but pool already at max_size → kill the extra."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0, max_size=1)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        # Pre-fill the queue to max_size
        existing = _make_container("existing", "python")
        await pool._queues["python"].put(existing)

        kill_calls = []

        async def fake_start(stack):
            return "overflow-ctr"

        async def fake_resolve(stack):
            return "img:latest"

        async def fake_kill(cid):
            kill_calls.append(cid)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_start_container", side_effect=fake_start):
                with patch.object(pool, "_resolve_image", side_effect=fake_resolve):
                    with patch.object(pool, "_kill_container", side_effect=fake_kill):
                        await pool._start_and_enqueue("python")

        assert "overflow-ctr" in kill_calls
        assert pool._queues["python"].qsize() == 1  # still just the existing one

    @pytest.mark.asyncio
    async def test_start_failure_is_swallowed(self):
        """_start_container raises → error logged, no exception propagated."""
        pool = SandboxPool()
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            await pool._warmup()

        async def fake_start(stack):
            raise RuntimeError("docker daemon not found")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch.object(pool, "_start_container", side_effect=fake_start):
                await pool._start_and_enqueue("python")  # must not raise

        assert pool._queues["python"].qsize() == 0


# ── get_sandbox_pool singleton ────────────────────────────────────────────────


class TestGetSandboxPool:
    def setup_method(self):
        reset_pool_for_testing()

    def teardown_method(self):
        reset_pool_for_testing()

    @pytest.mark.asyncio
    async def test_returns_pool_instance(self):
        """get_sandbox_pool() returns a SandboxPool."""
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch(
                "phalanx.ci_fixer.sandbox_pool.SandboxPool._warmup",
                new_callable=AsyncMock,
            ):
                pool = await get_sandbox_pool()

        assert isinstance(pool, SandboxPool)

    @pytest.mark.asyncio
    async def test_returns_same_instance_on_repeat_calls(self):
        """Second call returns the same singleton."""
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch(
                "phalanx.ci_fixer.sandbox_pool.SandboxPool._warmup",
                new_callable=AsyncMock,
            ):
                p1 = await get_sandbox_pool()
                p2 = await get_sandbox_pool()

        assert p1 is p2

    @pytest.mark.asyncio
    async def test_reset_allows_new_instance(self):
        """reset_pool_for_testing() clears singleton → next call creates fresh pool."""
        mock_settings = _mock_settings(min_size=0)

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch(
                "phalanx.ci_fixer.sandbox_pool.SandboxPool._warmup",
                new_callable=AsyncMock,
            ):
                p1 = await get_sandbox_pool()

        reset_pool_for_testing()

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch(
                "phalanx.ci_fixer.sandbox_pool.SandboxPool._warmup",
                new_callable=AsyncMock,
            ):
                p2 = await get_sandbox_pool()

        assert p1 is not p2


# ── SandboxPool._health_check ─────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_container(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")
        proc = _make_proc(returncode=0, stdout=b"ok")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._health_check(container)

        assert result is True

    @pytest.mark.asyncio
    async def test_unhealthy_container_nonzero_exit(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")
        proc = _make_proc(returncode=1, stdout=b"")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._health_check(container)

        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_exception_returns_false(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("docker")):
                result = await pool._health_check(container)

        assert result is False


# ── SandboxPool._reset_container ─────────────────────────────────────────────


class TestResetContainer:
    @pytest.mark.asyncio
    async def test_reset_success(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")
        proc = _make_proc(returncode=0, stdout=b"done")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._reset_container(container)

        assert result is True

    @pytest.mark.asyncio
    async def test_reset_failure_nonzero(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")
        proc = _make_proc(returncode=1, stdout=b"")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await pool._reset_container(container)

        assert result is False

    @pytest.mark.asyncio
    async def test_reset_exception_returns_false(self):
        pool = SandboxPool()
        mock_settings = _mock_settings()
        container = _make_container("ctr1")

        with patch("phalanx.ci_fixer.sandbox_pool.settings", mock_settings):
            with patch("asyncio.create_subprocess_exec", side_effect=Exception("timeout")):
                result = await pool._reset_container(container)

        assert result is False
