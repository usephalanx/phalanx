"""
Context Retriever — assembles ContextBundle before the repair agent runs.

No LLM calls. Pure data gathering:
  1. Read failing files from cloned workspace
  2. Query ci_failure_fingerprints for similar past successful fixes
  3. For L3: pull imported module files (import graph traversal)

ContextBundle is the typed contract passed from retriever → repair agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phalanx.ci_fixer.classifier import ClassificationResult
    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

_MAX_FILES = 4
_MAX_FILE_CHARS = 80_000   # total chars across all files before truncation
_MAX_SIMILAR_FIXES = 3


# ── Data types ─────────────────────────────────────────────────────────────────


@dataclass
class SimilarFix:
    """One entry from the ci_failure_fingerprints history lookup."""
    fingerprint_hash: str
    tool: str
    sample_errors: str
    last_good_patch_json: str | None   # raw JSON string from DB
    success_count: int
    similarity_score: float            # 0.0–1.0; -1.0 = exact hash match


@dataclass
class ContextBundle:
    """
    Everything assembled before the repair agent runs.
    Typed contract: retriever → repair agent.
    """

    parsed_log: ParsedLog
    classification: ClassificationResult
    workspace: Path

    # Files to repair (relative paths from repo root)
    failing_files: list[str] = field(default_factory=list)

    # Full text of each failing file, keyed by relative path
    file_contents: dict[str, str] = field(default_factory=dict)

    # Up to 3 similar past fixes from ci_failure_fingerprints
    similar_fixes: list[SimilarFix] = field(default_factory=list)

    # Cleaned log excerpt (first 1200 chars of clean_log output)
    log_excerpt: str = ""

    # L3 only: files imported by the failing files
    extended_context_files: dict[str, str] = field(default_factory=dict)

    def total_file_chars(self) -> int:
        return (
            sum(len(v) for v in self.file_contents.values())
            + sum(len(v) for v in self.extended_context_files.values())
        )

    def has_history(self) -> bool:
        """True if at least one similar fix has a stored patch."""
        return any(f.last_good_patch_json for f in self.similar_fixes)


# ── Retriever ──────────────────────────────────────────────────────────────────


class ContextRetriever:
    """
    Assembles a ContextBundle from ParsedLog + workspace + DB.
    All methods are async (DB queries use AsyncSession).
    """

    async def retrieve(
        self,
        parsed_log: ParsedLog,
        classification: ClassificationResult,
        workspace: Path,
        repo_full_name: str,
        fingerprint_hash: str,
        session: AsyncSession,
    ) -> ContextBundle:
        """
        Build a ContextBundle.

        Steps:
          1. Read failing files from disk
          2. Query similar past fixes from DB
          3. L3: read imported modules too
        """
        failing_files = parsed_log.all_files[:_MAX_FILES]
        file_contents = _read_files(workspace, failing_files)

        similar_fixes = await _query_similar_fixes(
            session, fingerprint_hash, repo_full_name, classification.tool
        )


        bundle = ContextBundle(
            parsed_log=parsed_log,
            classification=classification,
            workspace=workspace,
            failing_files=failing_files,
            file_contents=file_contents,
            similar_fixes=similar_fixes,
            log_excerpt="",  # caller fills this from raw log
        )

        # L3: extend with imported modules
        if classification.complexity_tier == "L3":
            extended = _read_imported_files(workspace, file_contents, already_read=set(failing_files))
            bundle.extended_context_files = extended

        # Trim if over limit
        _trim_to_limit(bundle)

        log.info(
            "context_retriever.bundle_ready",
            files=len(bundle.file_contents),
            similar_fixes=len(bundle.similar_fixes),
            total_chars=bundle.total_file_chars(),
            tier=classification.complexity_tier,
        )
        return bundle


# ── Helpers ────────────────────────────────────────────────────────────────────


def _read_files(workspace: Path, rel_paths: list[str]) -> dict[str, str]:
    """Read files from workspace. Silently skips missing/unreadable files."""
    contents: dict[str, str] = {}
    for rel in rel_paths:
        full = workspace / rel
        if not full.exists():
            # Try rglob fallback for monorepo path prefix issues
            matches = list(workspace.rglob(Path(rel).name))
            if matches:
                full = matches[0]
                rel = str(full.relative_to(workspace))
            else:
                log.debug("context_retriever.file_not_found", path=rel)
                continue
        try:
            contents[rel] = full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            log.warning("context_retriever.read_failed", path=rel, error=str(exc))
    return contents


_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)


def _read_imported_files(
    workspace: Path,
    file_contents: dict[str, str],
    already_read: set[str],
) -> dict[str, str]:
    """
    For L3 failures: read files imported by the failing files.
    One level deep only — prevents runaway graph traversal.
    Caps at 3 additional files.
    """
    candidates: list[str] = []
    for content in file_contents.values():
        for m in _IMPORT_RE.finditer(content):
            module = (m.group(1) or m.group(2) or "").strip()
            if not module or module.startswith(("os", "sys", "typing", "collections")):
                continue
            # Convert module path to file path (simple heuristic)
            rel = module.replace(".", "/") + ".py"
            if rel not in already_read:
                candidates.append(rel)

    return _read_files(workspace, candidates[:3])


async def _query_similar_fixes(
    session: AsyncSession,
    fingerprint_hash: str,
    repo_full_name: str,
    tool: str,
) -> list[SimilarFix]:
    """
    Look up similar past fixes from ci_failure_fingerprints.

    Priority:
      1. Exact fingerprint_hash match (score = -1.0, sentinel for "exact")
      2. Same tool + repo, sorted by success_count desc (up to 2 more)
    """
    from sqlalchemy import and_, select  # noqa: PLC0415

    from phalanx.ci_fixer.suppressor import should_use_history  # noqa: PLC0415
    from phalanx.db.models import CIFailureFingerprint  # noqa: PLC0415

    results: list[SimilarFix] = []

    try:
        # 1. Exact match
        r = await session.execute(
            select(CIFailureFingerprint).where(
                and_(
                    CIFailureFingerprint.fingerprint_hash == fingerprint_hash,
                    CIFailureFingerprint.success_count > 0,
                    CIFailureFingerprint.last_good_patch_json.isnot(None),
                )
            )
        )
        exact = r.scalar_one_or_none()
        if exact and should_use_history(exact):
            results.append(SimilarFix(
                fingerprint_hash=exact.fingerprint_hash,
                tool=exact.tool,
                sample_errors=exact.sample_errors or "",
                last_good_patch_json=exact.last_good_patch_json,
                success_count=exact.success_count,
                similarity_score=-1.0,
            ))

        # 2. Same tool + repo (up to 2 more)
        if len(results) < _MAX_SIMILAR_FIXES:
            r2 = await session.execute(
                select(CIFailureFingerprint).where(
                    and_(
                        CIFailureFingerprint.repo_full_name == repo_full_name,
                        CIFailureFingerprint.tool == tool,
                        CIFailureFingerprint.success_count > 0,
                        CIFailureFingerprint.last_good_patch_json.isnot(None),
                        CIFailureFingerprint.fingerprint_hash != fingerprint_hash,
                    )
                ).order_by(CIFailureFingerprint.success_count.desc()).limit(2)
            )
            for fp in r2.scalars().all():
                if should_use_history(fp):
                    results.append(SimilarFix(
                        fingerprint_hash=fp.fingerprint_hash,
                        tool=fp.tool,
                        sample_errors=fp.sample_errors or "",
                        last_good_patch_json=fp.last_good_patch_json,
                        success_count=fp.success_count,
                        similarity_score=0.5,
                    ))
    except Exception as exc:
        log.warning("context_retriever.db_query_failed", error=str(exc))

    return results[:_MAX_SIMILAR_FIXES]


def _trim_to_limit(bundle: ContextBundle) -> None:
    """Trim extended_context_files then file_contents if over char limit."""
    if bundle.total_file_chars() <= _MAX_FILE_CHARS:
        return
    # Drop extended context first
    bundle.extended_context_files.clear()
    if bundle.total_file_chars() <= _MAX_FILE_CHARS:
        return
    # Trim largest file_contents entries
    while bundle.file_contents and bundle.total_file_chars() > _MAX_FILE_CHARS:
        largest = max(bundle.file_contents, key=lambda k: len(bundle.file_contents[k]))
        del bundle.file_contents[largest]
