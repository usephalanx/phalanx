"""SRE setup memoization — Phase 2.

Cache key = sha256 of (pyproject.toml + .github/workflows/*.yml +
.pre-commit-config.yaml + .tool-versions). Same setup files → same plan
→ skip the LLM loop and replay deterministically.

Cache table created by alembic migration 20260430_0001. 24h TTL enforced
at SELECT time (not in DDL) so we can re-tune without migration.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import insert, select, update

from phalanx.db.session import get_db

if TYPE_CHECKING:
    from collections.abc import Iterable

log = structlog.get_logger(__name__)


CACHE_TTL_HOURS: int = 24
"""How long a cached plan stays usable. 24h matches the design doc's
default; bump to 7d if observability shows setup files change rarely."""

# Files whose contents define the env. Keep this list narrow on purpose;
# adding a file to the key invalidates the cache for everyone, so each
# inclusion needs to actually move the install plan.
_CACHE_KEY_FILES: tuple[str, ...] = (
    "pyproject.toml",
    ".pre-commit-config.yaml",
    ".tool-versions",
)
_CACHE_KEY_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)


def compute_cache_key(workspace_path: str | Path) -> str:
    """Return sha256 hex of the concatenated setup-relevant files.

    Files that don't exist contribute an empty marker so adding a workflow
    later actually changes the key (rather than colliding with a workspace
    that just doesn't have that file).
    """
    workspace = Path(workspace_path).resolve()
    h = hashlib.sha256()

    parts: list[bytes] = []
    for relpath in _CACHE_KEY_FILES:
        p = workspace / relpath
        marker = f"=== {relpath} ===\n".encode()
        parts.append(marker)
        if p.is_file():
            try:
                parts.append(p.read_bytes())
            except OSError:
                parts.append(b"<read-error>")
        else:
            parts.append(b"<missing>")

    # Workflow YAMLs — sort for determinism.
    wf_dir = workspace / ".github" / "workflows"
    if wf_dir.is_dir():
        wf_files = sorted(
            p for p in wf_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
        )
        for p in wf_files:
            relpath = str(p.relative_to(workspace))
            parts.append(f"=== {relpath} ===\n".encode())
            try:
                parts.append(p.read_bytes())
            except OSError:
                parts.append(b"<read-error>")

    for chunk in parts:
        h.update(chunk)
        h.update(b"\n")
    return h.hexdigest()


async def cache_lookup(
    cache_key: str,
    *,
    repo_full_name: str,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> dict[str, Any] | None:
    """Return the cached install_plan dict if a recent READY entry exists.

    Returns None on miss, expired, or non-READY status. Caller treats None
    as "run the LLM loop". Increments hit_count on hit (best-effort —
    hit_count failure doesn't block returning the plan).
    """
    from phalanx.db.models import (  # noqa: PLC0415  (avoid circular at import time)
        SREsSetupCache,
    )

    async with get_db() as session:
        result = await session.execute(
            select(SREsSetupCache).where(SREsSetupCache.cache_key == cache_key)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None

        if row.repo_full_name != repo_full_name:
            # Cache key collision across repos? Astronomically unlikely
            # (sha256 of file contents), but if it happens the per-repo
            # safety check refuses the hit.
            log.warning(
                "v3.sre_setup.cache.repo_mismatch",
                cache_key=cache_key[:16],
                cached_repo=row.repo_full_name,
                requested_repo=repo_full_name,
            )
            return None

        if row.final_status != "READY":
            return None

        cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)
        if row.created_at < cutoff:
            return None

        # Bump hit_count best-effort.
        try:
            await session.execute(
                update(SREsSetupCache)
                .where(SREsSetupCache.cache_key == cache_key)
                .values(hit_count=SREsSetupCache.hit_count + 1)
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "v3.sre_setup.cache.hit_count_update_failed",
                cache_key=cache_key[:16],
                error=str(exc)[:120],
            )

        log.info(
            "v3.sre_setup.cache.hit",
            cache_key=cache_key[:16],
            repo=repo_full_name,
            age_hours=round((datetime.now(UTC) - row.created_at).total_seconds() / 3600, 1),
        )
        return dict(row.install_plan)


async def cache_write(
    cache_key: str,
    *,
    repo_full_name: str,
    install_plan: dict[str, Any],
    final_status: str,
) -> None:
    """Insert a new cache row. ON CONFLICT (cache_key) leaves existing rows
    alone — re-using cache_key with a different plan signals a bug we
    should investigate, not silently overwrite.

    We only bother to call this on READY; PARTIAL/BLOCKED don't memoize
    well (the LLM may make different choices next time).
    """
    if final_status != "READY":
        return

    from phalanx.db.models import SREsSetupCache  # noqa: PLC0415

    async with get_db() as session:
        # Check first to avoid noisy IntegrityError on collision.
        existing = await session.execute(
            select(SREsSetupCache).where(SREsSetupCache.cache_key == cache_key)
        )
        if existing.scalar_one_or_none() is not None:
            log.info(
                "v3.sre_setup.cache.skip_existing",
                cache_key=cache_key[:16],
                repo=repo_full_name,
            )
            return

        await session.execute(
            insert(SREsSetupCache).values(
                cache_key=cache_key,
                repo_full_name=repo_full_name,
                install_plan=install_plan,
                final_status=final_status,
            )
        )
        await session.commit()
        log.info(
            "v3.sre_setup.cache.write",
            cache_key=cache_key[:16],
            repo=repo_full_name,
        )


def replay_plan_to_install_steps(install_plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Walk a cached install_plan into the ordered install steps the
    deterministic provisioner can re-execute.

    Phase 3 wires this — the cached plan is read here and re-applied without
    invoking the LLM loop. install_plan format:
        {"capabilities": [{"tool", "version", "install_method", "evidence_ref"}, ...]}
    """
    for cap in install_plan.get("capabilities", []):
        method = cap.get("install_method")
        if method == "preinstalled":
            continue
        yield {
            "tool": cap.get("tool"),
            "method": method,
            "evidence_ref": cap.get("evidence_ref"),
        }
