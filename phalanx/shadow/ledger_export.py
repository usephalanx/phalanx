"""P0-2 — durable append-only JSONL export of every shadow_ledger row change.

The DB is the source of truth. This module appends a line to ledger.jsonl
*after* the DB commit succeeds, so:

  - if the file write fails, the DB row is already durable (no data loss);
  - the JSONL is a strict subset of the DB on crash (never the other way);
  - the file can be replayed from the DB at any time (operator tool).

Durability semantics
--------------------
Each append is:
  1. Built as one UTF-8 byte string ending in '\n'.
  2. Opened with O_WRONLY | O_CREAT | O_APPEND on the target file.
  3. Locked with fcntl.LOCK_EX so concurrent writers serialize.
  4. Written with one os.write() syscall.
  5. fsync()'d before close.

The fsync guarantees the bytes are on stable storage when the call returns
(modulo disk firmware lies). The flock guarantees no byte-interleaving
between writers. The single os.write makes the append atomic up to the
kernel's per-write atomicity window — flock covers the rest.

Crash consistency
-----------------
On crash *during* a write, the file's last line may be truncated mid-JSON.
Readers MUST tolerate this — each JSONL line is independent, so a corrupt
tail does not poison earlier evidence. The verify tool (scripts/ledger_jsonl_verify.sh)
flags such lines and skips them.

Failure handling
----------------
This module **never raises** out of append_ledger_row_async. Any exception
is caught and logged via structlog. The dispatch's terminal verdict has
already been committed to the DB by the time we get here, so swallowing
the export failure cannot lose evidence — it can only delay surfacing it.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from phalanx.config.settings import get_settings

log = structlog.get_logger(__name__)

LEDGER_JSONL_SCHEMA_VERSION = 1

# Resolved at module load — repo root for relative ledger_jsonl_path.
_PHALANX_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path() -> Path:
    """Resolve the configured ledger_jsonl_path. Relative paths anchor to
    the repo root so the same setting works in the worker container
    (mounted at /app) and from a host CLI invocation."""
    raw = get_settings().ledger_jsonl_path
    p = Path(raw)
    return p if p.is_absolute() else (_PHALANX_ROOT / p)


@functools.lru_cache(maxsize=1)
def _build_sha() -> str:
    """Best-effort git SHA of the running code. Cached for the process
    lifetime. Returns 'unknown' if not in a git repo."""
    env = os.environ.get("PHALANX_BUILD_SHA")
    if env:
        return env[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_PHALANX_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _build_entry(row_dict: dict[str, Any], exported_by: str) -> dict[str, Any]:
    """Construct the wire-format dict for one append.

    Outer `_*` keys are export metadata; `row` is the unmodified DB-row dict
    so audit tooling can compare row == to_dict(db_row) byte-for-byte.
    """
    return {
        "_schema_version": LEDGER_JSONL_SCHEMA_VERSION,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "_exported_by": exported_by,
        "_pid": os.getpid(),
        "_phalanx_build_sha": _build_sha(),
        "row": row_dict,
    }


def _serialize(entry: dict[str, Any]) -> bytes:
    """Sort keys + compact separators + trailing newline. Deterministic
    across runs so audit diffs are clean."""
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def _sync_append(path: Path, payload: bytes) -> None:
    """Single-flock atomic append + fsync. Caller's thread context only —
    never invoked directly from the asyncio event loop."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # Exclusive lock — serializes appends across processes (host CLI
        # + worker container can race on the same physical file via the
        # /app volume mount; flock works across the docker boundary).
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # Single syscall: atomic up to PIPE_BUF; flock holds for larger.
            written = os.write(fd, payload)
            if written != len(payload):
                # Should not happen for our payload sizes, but defend.
                raise OSError(
                    f"short write: {written}/{len(payload)} bytes"
                )
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def append_ledger_row_async(
    row_dict: dict[str, Any],
    *,
    exported_by: str,
) -> bool:
    """Append one ledger-row state-change to the JSONL stream.

    Returns True on success, False on any failure (already logged).
    Never raises. Safe to await after a successful DB commit.

    The blocking syscalls (flock + write + fsync) run in a worker thread
    so the event loop stays responsive even during slow fsyncs. The
    underlying _sync_append is also safe to call directly from sync code
    if needed (e.g. tests, repair tools).
    """
    started = time.monotonic()
    path = _resolve_path()
    try:
        entry = _build_entry(row_dict, exported_by)
        payload = _serialize(entry)
        await asyncio.to_thread(_sync_append, path, payload)
    except Exception as e:  # noqa: BLE001
        log.error(
            "ledger_jsonl.export_failed",
            error=str(e),
            error_type=type(e).__name__,
            exported_by=exported_by,
            ledger_id=row_dict.get("id"),
            path=str(path),
        )
        return False
    log.debug(
        "ledger_jsonl.exported",
        ledger_id=row_dict.get("id"),
        exported_by=exported_by,
        bytes=len(payload),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return True


def append_ledger_row_sync(
    row_dict: dict[str, Any],
    *,
    exported_by: str,
) -> bool:
    """Synchronous variant — for tests and CLI replay tools. Same swallow
    semantics: returns False on failure, never raises."""
    try:
        entry = _build_entry(row_dict, exported_by)
        payload = _serialize(entry)
        _sync_append(_resolve_path(), payload)
        return True
    except Exception as e:  # noqa: BLE001
        log.error(
            "ledger_jsonl.export_failed",
            error=str(e),
            error_type=type(e).__name__,
            exported_by=exported_by,
            ledger_id=row_dict.get("id"),
        )
        return False
