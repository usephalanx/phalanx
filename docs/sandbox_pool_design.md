# Sandbox Pool Design — Isolated Execution for CI Fixer

## Status: Approved for implementation (Phase 3)

## Problem

`SandboxProvisioner.provision()` is currently a no-op — it returns a descriptor but
never starts a container. The reproducer and verifier run commands as local
subprocesses on the FORGE host. This means:

- No env isolation: host ruff/mypy version may differ from the repo's pinned version
- No filesystem isolation: a broken fix can dirty the host workspace
- No resource limits: a hung test can block other fix runs
- `docker run` cold-start (image pull + container create) costs 5–30s per fix run
  if we naively start a container on demand

---

## Design: Pre-warmed Pool

### Core idea

Never cold-start a container during a fix run. Keep a small pool of ready containers
per stack, already running with tools pre-installed. A fix run checks one out, uses
it, and the pool refills asynchronously in the background.

```
┌────────────────────────────────────────────────────────────┐
│  SandboxPool (lazy singleton, init after Celery fork)      │
│                                                            │
│  python: [🟢 ready] [🟢 ready] [🟡 warming]               │
│  node:   [🟢 ready] [🟡 warming]                           │
│  go:     [🟢 ready]                                        │
│  rust:   [🟢 ready]                                        │
└────────────────────────────────────────────────────────────┘
        │ checkout(stack)                    ↑ checkin(container)
        ▼                                    │
┌────────────────────────────────────────────────────────────┐
│  Fix run (ReproducerAgent + VerifierAgent)                  │
│  1. pool.checkout("python") → PooledContainer              │
│  2. bind-mount workspace into container                    │
│  3. docker exec reproducer_cmd                             │
│  4. docker exec fix validator                              │
│  5. docker exec verifier (ruff/pytest/etc.)                │
│  6. pool.checkin(container) → reset + async refill         │
└────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. `PooledContainer` (dataclass)

```python
@dataclass
class PooledContainer:
    container_id: str      # Docker container ID (short hash)
    stack: str             # "python" | "node" | "go" | "rust"
    image: str             # e.g. "phalanx-sandbox-python:latest"
    checked_out_at: float  # monotonic time — for reaper timeout detection
    healthy: bool = True
```

### 2. `SandboxPool` (async singleton)

One `asyncio.Queue` per stack. Each queue holds ready `PooledContainer` objects.

Key methods:

| Method | What it does |
|--------|-------------|
| `_warmup()` | Called once after lazy init. Starts `min_size` containers per stack. |
| `checkout(stack, timeout)` | `asyncio.wait_for(queue.get(), timeout)`. Returns container or raises `SandboxUnavailableError`. |
| `checkin(container)` | Runs reset script inside container, puts it back in queue. Triggers async `_refill`. |
| `_start_container(stack)` | `docker run -d --rm --user phalanx --no-new-privileges -v /tmp:/hosttmp {image} sleep infinity` |
| `_health_check(container)` | `docker exec {id} echo ok`. Returns bool. |
| `_reset_container(container)` | `docker exec {id} /phalanx/reset.sh` — clears /workspace, /tmp, pip/npm cache. |
| `_reaper()` | Background task. Every 60s: kill containers held > `max_hold_seconds`. Replace them. |
| `shutdown()` | Kill all containers in all queues. Called on worker shutdown. |

### 3. `SandboxResult` (upgraded)

Two new fields added to existing dataclass:

```python
container_id: str = ""        # populated when pool checkout succeeds
mount_path: str = "/workspace" # path inside the container
```

`available=True` + `container_id != ""` → real Docker exec path  
`available=True` + `container_id == ""` → pool timeout, local subprocess fallback  
`available=False` → sandbox_enabled=False, local subprocess fallback

### 4. `SandboxProvisioner.provision()` (upgraded)

```python
async def provision(workspace_path, stack_hint=None) -> SandboxResult | None:
    if not settings.sandbox_enabled:
        return None
    stack = stack_hint or self.detect_stack(workspace_path)
    image = _STACK_IMAGES[stack]
    sandbox_id = f"phalanx-sandbox-{uuid.uuid4().hex[:8]}"

    pool = await get_sandbox_pool()          # lazy singleton, safe after fork
    try:
        container = await pool.checkout(stack, timeout=settings.sandbox_checkout_timeout_seconds)
        # bind-mount workspace into the container
        await pool.mount_workspace(container, workspace_path)
        return SandboxResult(
            sandbox_id=sandbox_id, stack=stack, image=image,
            workspace_path=str(workspace_path),
            container_id=container.container_id,
        )
    except SandboxUnavailableError:
        log.warning("ci_fixer.sandbox_pool_exhausted", stack=stack)
        return SandboxResult(
            sandbox_id=sandbox_id, stack=stack, image=image,
            workspace_path=str(workspace_path),
            available=False,             # → local subprocess fallback
        )
```

### 5. `ReproducerAgent._run_subprocess()` (upgraded)

When `sandbox_result` has a `container_id`, wrap the command:

```python
if sandbox_result and sandbox_result.container_id:
    cmd = f"docker exec {sandbox_result.container_id} sh -c {shlex.quote(cmd)}"
```

Otherwise falls through to current `asyncio.create_subprocess_shell` behavior.

### 6. `VerifierAgent._run_cmd()` (upgraded)

Same pattern — when `container_id` is set, prefix args with `["docker", "exec", container_id]`.

---

## Stack Images

Custom images with tools pre-installed at pinned versions. Stored in `docker/sandbox/`.

```
docker/sandbox/
  python/Dockerfile
  node/Dockerfile
  go/Dockerfile
  rust/Dockerfile
  reset.sh          # shared reset script copied into every image
```

### `reset.sh`

```bash
#!/bin/bash
# Clear workspace and caches between fix runs.
rm -rf /workspace/*
rm -rf /tmp/pip-* /tmp/npm-* /root/.cache 2>/dev/null || true
```

### Python image example

```dockerfile
FROM python:3.12-slim
RUN useradd -m -u 1000 phalanx
RUN pip install --no-cache-dir ruff==0.4.4 mypy==1.10.0 pytest==8.2.0
COPY reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh
WORKDIR /workspace
USER phalanx
```

---

## Settings (new keys)

```
SANDBOX_POOL_MIN_SIZE=1           # containers to pre-warm per stack at startup
SANDBOX_POOL_MAX_SIZE=2           # max simultaneous checked-out containers per stack
SANDBOX_CHECKOUT_TIMEOUT_SECONDS=30   # wait for pool slot before falling back
SANDBOX_MAX_HOLD_SECONDS=300      # reaper kills containers held longer than this
SANDBOX_REAPER_INTERVAL_SECONDS=60    # how often reaper runs
```

Setting `SANDBOX_POOL_MIN_SIZE=0` disables pre-warming — containers start cold on first use.
Setting `SANDBOX_ENABLED=false` disables the entire pool (existing behavior).

---

## Pool initialization and Celery fork safety

**Problem**: Celery pre-forks workers. If the pool is a module-level singleton
initialized before fork, child workers inherit a stale event loop reference → all
`await` calls inside the pool fail.

**Solution**: Lazy init behind an `asyncio.Lock`.

```python
_pool_instance: SandboxPool | None = None
_pool_lock: asyncio.Lock | None = None

async def get_sandbox_pool() -> SandboxPool:
    global _pool_instance, _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()       # created inside the child's event loop
    async with _pool_lock:
        if _pool_instance is None:
            _pool_instance = SandboxPool()
            await _pool_instance._warmup()
    return _pool_instance
```

First call to `provision()` in each Celery child worker triggers this.
Subsequent calls in the same worker reuse the warm pool.

---

## Fallback chain (no regressions possible)

```
sandbox_enabled=False
    → return None → reproducer/verifier: local subprocess (today's behavior)

sandbox_enabled=True, pool checkout times out (all slots busy)
    → SandboxResult(available=False) → local subprocess fallback

sandbox_enabled=True, Docker daemon not found
    → SandboxResult(available=False) → local subprocess fallback

sandbox_enabled=True, container health check fails
    → discard container, start fresh one, retry checkout once
    → if retry fails: SandboxResult(available=False) → local subprocess fallback

sandbox_enabled=True, container_id populated
    → docker exec {cmd} → real isolated execution
```

Every error path degrades to local subprocess. Fix runs never fail due to sandbox
infrastructure issues.

---

## What is NOT in scope (future)

- **Network isolation** (`--network none`) — useful but breaks `pip install` fallback
- **CPU/memory cgroups** (`--cpus`, `--memory`) — nice-to-have, not blocking
- **Real Docker socket forwarding** for nested Docker — not needed for lint/type/test tools
- **Multi-host pool** (pool across multiple FORGE workers) — Redis-backed queue,
  post-MVP when horizontal scaling is needed

---

## File map

| File | Change |
|------|--------|
| `phalanx/ci_fixer/sandbox_pool.py` | **NEW** — SandboxPool, PooledContainer, get_sandbox_pool |
| `phalanx/ci_fixer/sandbox.py` | **MODIFIED** — SandboxResult gets container_id/mount_path; provision() uses pool |
| `phalanx/ci_fixer/reproducer.py` | **MODIFIED** — _run_subprocess wraps with docker exec when container_id set |
| `phalanx/ci_fixer/verifier.py` | **MODIFIED** — _run_cmd wraps with docker exec when container_id set |
| `phalanx/config/settings.py` | **MODIFIED** — 5 new SANDBOX_POOL_* settings |
| `docker/sandbox/python/Dockerfile` | **NEW** |
| `docker/sandbox/node/Dockerfile` | **NEW** |
| `docker/sandbox/go/Dockerfile` | **NEW** |
| `docker/sandbox/rust/Dockerfile` | **NEW** |
| `docker/sandbox/reset.sh` | **NEW** |
| `tests/unit/test_sandbox_pool.py` | **NEW** — ≥80% coverage on sandbox_pool.py |
| `tests/unit/test_ci_fixer_sandbox.py` | **MODIFIED** — cover pool checkout path |
| `tests/unit/test_ci_fixer_reproducer.py` | **MODIFIED** — cover docker exec path |
| `tests/unit/test_ci_fixer_verifier.py` | **MODIFIED** — cover docker exec path |
