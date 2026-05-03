"""v1.7.1 setup recipe cache.

Per docs/v171-provisioning-tiers.md: target 90% cache hit rate on second
run of same repo. Without this, every run re-derives the recipe even
though deps haven't changed.

Backing store: per-repo JSONL files at
  {settings.git_workspace}/_v171_setup_cache/{repo_hash}.jsonl
Each line is a SetupRecipe entry. Lookup walks the file (newest-first)
and returns the first entry whose key matches.

JSONL was chosen over SQLite for v1.7.1 because:
  - One-file-per-repo isolates concurrency: writes for repo A don't
    contend with reads for repo B
  - Append-only writes are atomic on POSIX
  - Recovery is trivial: corrupt line → skip, never lose other entries
  - Visible to humans on the filesystem

If concurrent writes within a repo become an issue, swap to SQLite WAL —
the public API (`lookup`, `store`, `invalidate`) is store-agnostic.

Cache key: sha256 of (repo_full_name, workflow_path or '', concatenated
content of all known dep files in canonical order). When ANY dep file
content changes, the key changes and old entries become unreachable
(but stay on disk for debugging — not garbage-collected in v1.7.1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger(__name__)


# Files we hash into the cache key. Order matters — must be stable
# across runs. Missing files are treated as empty content.
_KEY_DEP_FILES = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pixi.toml",
    "pixi.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
)


@dataclass
class SetupRecipe:
    """A cached setup recipe. Maps directly to one JSONL line.

    `tier` records which tier produced it ("0"/"1"/"2"); useful for
    telemetry. `validated=True` means this recipe successfully
    provisioned a sandbox at least once — invalidated entries stay
    in the file with `validated=False` for debugging.

    `cache_key` is duplicated in each line so we can survive the
    "different keys in same file" edge case if hash inputs ever
    change shape.
    """

    cache_key: str
    tier: Literal["0", "1", "2"]
    commands: list[str]
    source: str                  # e.g., "workflow:.github/workflows/test.yml::test"
    produced_at: str             # ISO8601 UTC
    validated: bool = False
    validation_evidence: dict = field(default_factory=dict)

    def to_jsonl_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> SetupRecipe:
        return cls(
            cache_key=d["cache_key"],
            tier=d["tier"],
            commands=list(d.get("commands") or []),
            source=d.get("source") or "",
            produced_at=d.get("produced_at") or "",
            validated=bool(d.get("validated")),
            validation_evidence=dict(d.get("validation_evidence") or {}),
        )


# ─── Cache key construction ──────────────────────────────────────────────────


def _hash_file_content(workspace: Path, rel_path: str) -> bytes:
    """sha256 of file content; empty bytes if file missing/unreadable."""
    target = workspace / rel_path
    if not target.is_file():
        return b""
    try:
        return hashlib.sha256(target.read_bytes()).digest()
    except OSError:
        return b""


def compute_cache_key(
    *,
    repo_full_name: str,
    workflow_path: str | None,
    workspace_path: str | Path,
) -> str:
    """Stable hash of (repo, workflow_path, dep file contents).

    Stable means: same inputs → same key across runs / hosts. The
    workspace_path itself is NOT in the key — only the file CONTENTS.
    """
    workspace = Path(workspace_path)
    h = hashlib.sha256()
    h.update(repo_full_name.encode())
    h.update(b"\x1f")
    h.update((workflow_path or "").encode())
    for rel in _KEY_DEP_FILES:
        h.update(b"\x1f")
        h.update(rel.encode())
        h.update(b"\x1f")
        h.update(_hash_file_content(workspace, rel))
    return h.hexdigest()[:32]   # 128-bit, plenty for our scale


# ─── Per-repo file path ──────────────────────────────────────────────────────


def _repo_hash(repo_full_name: str) -> str:
    return hashlib.sha256(repo_full_name.encode()).hexdigest()[:16]


def _cache_file_for(cache_dir: str | Path, repo_full_name: str) -> Path:
    return Path(cache_dir) / f"{_repo_hash(repo_full_name)}.jsonl"


# ─── Public API ──────────────────────────────────────────────────────────────


def lookup(
    *,
    cache_dir: str | Path,
    repo_full_name: str,
    workflow_path: str | None,
    workspace_path: str | Path,
) -> SetupRecipe | None:
    """Return the most-recent VALIDATED recipe whose cache_key matches.

    Newest-first walk: we read the file in reverse and return the first
    valid match. Invalidated entries (validated=False) are skipped.
    """
    target_key = compute_cache_key(
        repo_full_name=repo_full_name,
        workflow_path=workflow_path,
        workspace_path=workspace_path,
    )
    cache_file = _cache_file_for(cache_dir, repo_full_name)
    if not cache_file.is_file():
        return None
    try:
        lines = cache_file.read_text(errors="replace").splitlines()
    except OSError as exc:
        log.warning("v171.cache.read_failed", error=str(exc))
        return None
    # Walk newest-first. The FIRST entry with a matching cache_key is
    # authoritative — if it's an invalidation marker, return None even
    # if older validated entries exist (they were superseded).
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("cache_key") != target_key:
            continue
        if not d.get("validated"):
            log.info(
                "v171.cache.miss_invalidated",
                repo=repo_full_name,
                source=d.get("source") or "",
            )
            return None
        recipe = SetupRecipe.from_dict(d)
        log.info(
            "v171.cache.hit",
            repo=repo_full_name,
            tier=recipe.tier,
            source=recipe.source,
        )
        return recipe
    return None


def store(
    *,
    cache_dir: str | Path,
    repo_full_name: str,
    workflow_path: str | None,
    workspace_path: str | Path,
    tier: Literal["0", "1", "2"],
    commands: list[str],
    source: str,
    validated: bool,
    validation_evidence: dict | None = None,
) -> SetupRecipe:
    """Append a recipe entry to the per-repo JSONL.

    Returns the persisted recipe (with computed cache_key + timestamp).
    Caller can hold this as the in-memory state for the rest of the run.
    """
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_file_for(cache_path, repo_full_name)

    cache_key = compute_cache_key(
        repo_full_name=repo_full_name,
        workflow_path=workflow_path,
        workspace_path=workspace_path,
    )
    recipe = SetupRecipe(
        cache_key=cache_key,
        tier=tier,
        commands=list(commands),
        source=source,
        produced_at=datetime.now(UTC).isoformat(),
        validated=validated,
        validation_evidence=dict(validation_evidence or {}),
    )
    # Append-only — POSIX guarantees atomicity for write() < PIPE_BUF
    # bytes; our lines are well under that for any sane recipe.
    with cache_file.open("a") as f:
        f.write(recipe.to_jsonl_line())
    log.info(
        "v171.cache.stored",
        repo=repo_full_name,
        tier=tier,
        source=source,
        validated=validated,
        cache_key=cache_key[:8],
    )
    return recipe


def invalidate(
    *,
    cache_dir: str | Path,
    repo_full_name: str,
    workflow_path: str | None,
    workspace_path: str | Path,
    reason: str,
) -> None:
    """Mark all entries with the current cache_key as invalidated.

    We don't delete entries — append a new record with `validated=False`
    that supersedes earlier validated copies on lookup (which only
    returns validated entries). Keeps history visible for debugging.
    """
    target_key = compute_cache_key(
        repo_full_name=repo_full_name,
        workflow_path=workflow_path,
        workspace_path=workspace_path,
    )
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_file_for(cache_path, repo_full_name)
    invalidation = SetupRecipe(
        cache_key=target_key,
        tier="0",
        commands=[],
        source=f"invalidated:{reason}",
        produced_at=datetime.now(UTC).isoformat(),
        validated=False,
    )
    with cache_file.open("a") as f:
        f.write(invalidation.to_jsonl_line())
    log.info(
        "v171.cache.invalidated",
        repo=repo_full_name,
        cache_key=target_key[:8],
        reason=reason,
    )


__all__ = [
    "SetupRecipe",
    "compute_cache_key",
    "lookup",
    "store",
    "invalidate",
]
