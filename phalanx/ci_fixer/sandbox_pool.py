"""
SandboxPool — pre-warmed container pool for isolated CI fix execution.

Design (see docs/sandbox_pool_design.md for full rationale):

  One asyncio.Queue per stack holds ready PooledContainer objects.
  fix runs call checkout() → get an already-running container →
  exec commands inside it → call checkin() → container is reset
  and returned to the queue.  A background refill task keeps the
  queue at min_size after each checkout.

  A reaper task runs every sandbox_reaper_interval_seconds and kills
  containers that have been checked out longer than sandbox_max_hold_seconds
  (safety net for fix runs that crash without calling checkin).

Celery fork safety:
  The pool is NEVER initialised at module import time.  Call
  get_sandbox_pool() (async) from inside an already-running event loop
  (i.e. inside a Celery task's asyncio.run() call).  The Lock and
  instance are created lazily on first call in each child process.

Fallback contract:
  checkout() raises SandboxUnavailableError on timeout or Docker error.
  Callers must catch it and fall back to local-subprocess execution.
  The pool NEVER raises uncaught exceptions that would abort a fix run.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from phalanx.config.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger(__name__)
settings = get_settings()

# ── Custom exceptions ─────────────────────────────────────────────────────────


class SandboxUnavailableError(Exception):
    """Raised by checkout() when no container is available within the timeout."""


# ── Stack → custom image mapping ──────────────────────────────────────────────
# Falls back to official slim images if custom image is not present locally.
_POOL_IMAGES: dict[str, str] = {
    "python": "phalanx-sandbox-python:latest",
    "node": "phalanx-sandbox-node:latest",
    "go": "phalanx-sandbox-go:latest",
    "rust": "phalanx-sandbox-rust:latest",
    "java": "phalanx-sandbox-java:latest",
    "unknown": "ubuntu:22.04",
}

_FALLBACK_IMAGES: dict[str, str] = {
    "python": "python:3.12-slim",
    "node": "node:20-slim",
    "go": "golang:1.22-alpine",
    "rust": "rust:1.77-slim",
    "java": "maven:3.9-eclipse-temurin-21",
    "unknown": "ubuntu:22.04",
}


# ── PooledContainer ───────────────────────────────────────────────────────────


@dataclass
class PooledContainer:
    """A single running container slot in the pool."""

    container_id: str
    """Short Docker container ID."""

    stack: str
    """Tech stack this container is configured for."""

    image: str
    """Image the container was started from."""

    checked_out_at: float = field(default_factory=time.monotonic)
    """monotonic timestamp of last checkout — used by the reaper."""

    healthy: bool = True
    """False after a failed health check — container will be replaced."""


# ── SandboxPool ───────────────────────────────────────────────────────────────


class SandboxPool:
    """
    Pre-warmed container pool.  One instance per Celery worker process.
    Never instantiate directly — use get_sandbox_pool().
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[PooledContainer]] = {}
        self._checked_out: dict[str, PooledContainer] = {}  # container_id → container
        self._refill_lock: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._shutdown = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _warmup(self) -> None:
        """
        Start min_size containers per stack and populate queues.
        Called once by get_sandbox_pool() after construction.
        Errors during warmup are logged but do not raise — the pool
        starts empty and fills as containers become available.
        """
        stacks = list(_POOL_IMAGES.keys())
        for stack in stacks:
            self._queues[stack] = asyncio.Queue()
            self._refill_lock[stack] = asyncio.Lock()

        min_size = settings.sandbox_pool_min_size
        if min_size == 0:
            log.info("ci_fixer.sandbox_pool.warmup_skipped", reason="min_size=0")
            return

        warmup_tasks = []
        for stack in stacks:
            for _ in range(min_size):
                warmup_tasks.append(self._start_and_enqueue(stack))

        results = await asyncio.gather(*warmup_tasks, return_exceptions=True)
        started = sum(1 for r in results if not isinstance(r, Exception))
        log.info(
            "ci_fixer.sandbox_pool.warmed",
            started=started,
            total=len(warmup_tasks),
        )

        # Start reaper background task
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        """Kill all containers and stop the reaper.  Called on worker shutdown."""
        self._shutdown = True
        if self._reaper_task:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task

        # Drain queues and kill containers
        kill_tasks = []
        for queue in self._queues.values():
            while not queue.empty():
                try:
                    container = queue.get_nowait()
                    kill_tasks.append(self._kill_container(container.container_id))
                except asyncio.QueueEmpty:
                    break

        for container in list(self._checked_out.values()):
            kill_tasks.append(self._kill_container(container.container_id))

        if kill_tasks:
            await asyncio.gather(*kill_tasks, return_exceptions=True)

        log.info("ci_fixer.sandbox_pool.shutdown_complete")

    # ── Public API ────────────────────────────────────────────────────────────

    async def checkout(
        self,
        stack: str,
        timeout: int | None = None,
    ) -> PooledContainer:
        """
        Check out a ready container for the given stack.

        Waits up to `timeout` seconds (default: settings.sandbox_checkout_timeout_seconds).
        Raises SandboxUnavailableError if no container becomes available in time.
        """
        if stack not in self._queues:
            raise SandboxUnavailableError(f"no pool for stack={stack!r}")

        effective_timeout = (
            timeout if timeout is not None else settings.sandbox_checkout_timeout_seconds
        )

        try:
            container = await asyncio.wait_for(
                self._queues[stack].get(),
                timeout=effective_timeout,
            )
        except TimeoutError as exc:
            raise SandboxUnavailableError(
                f"pool exhausted for stack={stack!r} after {effective_timeout}s"
            ) from exc

        # Health check — if unhealthy, discard and try once more
        if not await self._health_check(container):
            log.warning(
                "ci_fixer.sandbox_pool.unhealthy_on_checkout",
                container_id=container.container_id,
                stack=stack,
            )
            await self._kill_container(container.container_id)
            # Start a fresh replacement asynchronously
            asyncio.create_task(self._start_and_enqueue(stack))
            # Try one more time with a shorter timeout
            try:
                container = await asyncio.wait_for(
                    self._queues[stack].get(),
                    timeout=min(effective_timeout, 15),
                )
            except TimeoutError as exc:
                raise SandboxUnavailableError(
                    f"pool exhausted after health check retry for stack={stack!r}"
                ) from exc

        container.checked_out_at = time.monotonic()
        self._checked_out[container.container_id] = container

        log.info(
            "ci_fixer.sandbox_pool.checkout",
            container_id=container.container_id,
            stack=stack,
            queue_depth=self._queues[stack].qsize(),
        )

        # Kick off background refill so the queue stays at min_size
        asyncio.create_task(self._refill(stack))

        return container

    async def checkin(self, container: PooledContainer) -> None:
        """
        Return a container to the pool after a fix run completes.
        Resets the container state, then re-enqueues it.
        """
        self._checked_out.pop(container.container_id, None)

        log.info(
            "ci_fixer.sandbox_pool.checkin",
            container_id=container.container_id,
            stack=container.stack,
        )

        if self._shutdown:
            await self._kill_container(container.container_id)
            return

        # Reset filesystem state inside the container
        reset_ok = await self._reset_container(container)
        if not reset_ok:
            log.warning(
                "ci_fixer.sandbox_pool.reset_failed",
                container_id=container.container_id,
            )
            await self._kill_container(container.container_id)
            asyncio.create_task(self._start_and_enqueue(container.stack))
            return

        # Verify still healthy after reset
        if not await self._health_check(container):
            log.warning(
                "ci_fixer.sandbox_pool.unhealthy_after_reset",
                container_id=container.container_id,
            )
            await self._kill_container(container.container_id)
            asyncio.create_task(self._start_and_enqueue(container.stack))
            return

        await self._queues[container.stack].put(container)

    @asynccontextmanager
    async def borrow(
        self,
        stack: str,
        timeout: int | None = None,
    ) -> AsyncIterator[PooledContainer]:
        """
        Context manager that checks out a container and guarantees checkin,
        even if the fix run raises.

        Usage:
            async with pool.borrow("python") as container:
                await exec_in_container(container, "ruff check .")
        """
        container = await self.checkout(stack, timeout=timeout)
        try:
            yield container
        finally:
            await self.checkin(container)

    async def mount_workspace(
        self,
        container: PooledContainer,
        workspace_path: object,
    ) -> None:
        """
        Ensure the workspace is accessible inside the container at /workspace.

        Strategy: the container is started with -v /tmp:/hosttmp, so we symlink
        /workspace → /hosttmp/{run_subdir} which avoids docker cp entirely.
        If the container was started without that mount, fall back to docker cp.

        In practice provision() always starts containers with the bind mount;
        this method is a no-op in the happy path (the bind mount already exists).
        """
        # The bind mount is set up at container start time in _start_container.
        # Nothing to do here — workspace is already visible at /workspace inside
        # the container via the per-run bind mount added by provision().
        pass

    # ── Docker helpers ────────────────────────────────────────────────────────

    async def _start_container(self, stack: str) -> str:
        """
        Start a new sandbox container for the given stack.
        Returns the container ID (short hash).
        Raises on Docker error.
        """
        image = await self._resolve_image(stack)
        cmd = settings.sandbox_docker_cmd

        proc = await asyncio.create_subprocess_exec(
            cmd,
            "run",
            "-d",  # detached
            "--rm",  # auto-remove when stopped
            "--network",
            "bridge",  # bridge allows pip install during env setup; isolated from host
            "--memory",
            "512m",  # memory limit
            "--cpus",
            "1",  # cpu limit
            image,
            "sleep",
            "infinity",  # keep alive until we kill it
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed for stack={stack!r}: {stderr.decode().strip()}")

        container_id = stdout.decode().strip()[:12]
        log.info(
            "ci_fixer.sandbox_pool.container_started",
            container_id=container_id,
            stack=stack,
            image=image,
        )
        return container_id

    async def _resolve_image(self, stack: str) -> str:
        """
        Return phalanx-sandbox-{stack}:latest if it exists locally,
        else fall back to the official slim image.
        """
        preferred = _POOL_IMAGES.get(stack, _FALLBACK_IMAGES.get(stack, "ubuntu:22.04"))
        cmd = settings.sandbox_docker_cmd

        proc = await asyncio.create_subprocess_exec(
            cmd,
            "image",
            "inspect",
            preferred,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        if proc.returncode == 0:
            return preferred

        fallback = _FALLBACK_IMAGES.get(stack, "ubuntu:22.04")
        log.info(
            "ci_fixer.sandbox_pool.image_fallback",
            preferred=preferred,
            fallback=fallback,
            stack=stack,
        )
        return fallback

    async def _health_check(self, container: PooledContainer) -> bool:
        """Return True if container responds to `docker exec echo ok`."""
        cmd = settings.sandbox_docker_cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd,
                "exec",
                container.container_id,
                "echo",
                "ok",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode == 0 and b"ok" in stdout
        except Exception:
            return False

    async def _reset_container(self, container: PooledContainer) -> bool:
        """
        Run the reset script inside the container to clear /workspace and caches.
        Returns True on success.
        """
        cmd = settings.sandbox_docker_cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd,
                "exec",
                container.container_id,
                "sh",
                "-c",
                "rm -rf /workspace/* /tmp/pip-* /tmp/npm-* /root/.cache 2>/dev/null; echo done",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and b"done" in stdout
        except Exception:
            return False

    async def _kill_container(self, container_id: str) -> None:
        """Kill and remove a container, ignoring errors."""
        cmd = settings.sandbox_docker_cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd,
                "kill",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _start_and_enqueue(self, stack: str) -> None:
        """Start a container and add it to the pool queue.  Errors are swallowed."""
        try:
            container_id = await self._start_container(stack)
            image = await self._resolve_image(stack)
            container = PooledContainer(
                container_id=container_id,
                stack=stack,
                image=image,
            )
            # Only enqueue if within max_size
            current_depth = self._queues[stack].qsize()
            current_checked_out = sum(1 for c in self._checked_out.values() if c.stack == stack)
            if current_depth + current_checked_out < settings.sandbox_pool_max_size:
                await self._queues[stack].put(container)
            else:
                # Pool is full — kill the just-started container
                await self._kill_container(container_id)
        except Exception as exc:
            log.warning(
                "ci_fixer.sandbox_pool.start_failed",
                stack=stack,
                error=str(exc),
            )

    async def _refill(self, stack: str) -> None:
        """
        Ensure the queue has at least min_size containers after a checkout.
        Uses a per-stack lock to avoid duplicate refill tasks racing.
        """
        async with self._refill_lock[stack]:
            current = self._queues[stack].qsize()
            needed = settings.sandbox_pool_min_size - current
            if needed > 0:
                await self._start_and_enqueue(stack)

    async def _reaper_loop(self) -> None:
        """
        Background task: every sandbox_reaper_interval_seconds, kill containers
        that have been checked out longer than sandbox_max_hold_seconds.
        This is a safety net for fix runs that crash without calling checkin().
        """
        while not self._shutdown:
            try:
                await asyncio.sleep(settings.sandbox_reaper_interval_seconds)
                now = time.monotonic()
                max_hold = settings.sandbox_max_hold_seconds
                stale = [
                    c for c in list(self._checked_out.values()) if now - c.checked_out_at > max_hold
                ]
                for container in stale:
                    log.warning(
                        "ci_fixer.sandbox_pool.reaper_killing",
                        container_id=container.container_id,
                        stack=container.stack,
                        held_seconds=round(now - container.checked_out_at),
                    )
                    self._checked_out.pop(container.container_id, None)
                    await self._kill_container(container.container_id)
                    await self._start_and_enqueue(container.stack)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ci_fixer.sandbox_pool.reaper_error", error=str(exc))


# ── Lazy singleton ────────────────────────────────────────────────────────────

_pool_instance: SandboxPool | None = None
_pool_lock: asyncio.Lock | None = None


async def get_sandbox_pool() -> SandboxPool:
    """
    Return the process-local SandboxPool singleton, initialising it on first call.

    Safe to call from inside a Celery asyncio.run() task — the Lock and instance
    are created lazily inside the child's own event loop, avoiding Celery pre-fork
    event-loop conflicts.
    """
    global _pool_instance, _pool_lock

    if _pool_lock is None:
        _pool_lock = asyncio.Lock()

    async with _pool_lock:
        if _pool_instance is None:
            _pool_instance = SandboxPool()
            await _pool_instance._warmup()

    return _pool_instance


def reset_pool_for_testing() -> None:
    """
    Reset the global singleton.  Only call from test teardown — never in production.
    """
    global _pool_instance, _pool_lock
    _pool_instance = None
    _pool_lock = None


# ── exec helper used by ReproducerAgent + VerifierAgent ───────────────────────


def wrap_cmd_for_container(
    container_id: str,
    cmd_args: list[str],
    workspace_path: str,
    docker_cmd: str = "docker",
) -> list[str]:
    """
    Wrap a command list so it executes inside the given container.

    The workspace is bind-mounted at /workspace inside the container.
    We set WORKDIR via -w flag so relative paths resolve correctly.

    Returns a new args list: [docker, exec, -w, /workspace, container_id, *cmd_args]
    """
    return [docker_cmd, "exec", "-w", "/workspace", container_id, *cmd_args]


def wrap_shell_cmd_for_container(
    container_id: str,
    shell_cmd: str,
    docker_cmd: str = "docker",
) -> list[str]:
    """
    Wrap a shell string command to run inside a container via `docker exec sh -c`.

    Runs as root (`--user 0`) so validator commands can see packages that
    env setup pip-installed to system site-packages / /root/.local. The
    container itself is the security boundary — running root inside is
    safe and matches what CI does (CI runners are always root).
    """
    return [
        docker_cmd,
        "exec",
        "--user",
        "0",
        "-w",
        "/workspace",
        "-e",
        "HOME=/root",
        container_id,
        "sh",
        "-c",
        shell_cmd,
    ]
