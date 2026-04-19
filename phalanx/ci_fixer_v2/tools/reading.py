"""Reading tools — safe, workspace-scoped file-system inspection.

Currently implemented in Week 1.5:
  - read_file: bounded file read with path-traversal protection

Pending (Week 1.6):
  - grep, glob

Safety rules (enforced in every tool here):
  - The `path` argument is resolved relative to `AgentContext.repo_workspace_path`.
  - After resolution, the realpath must be a subpath of the workspace realpath;
    any `..` / symlink escape is rejected with `path_outside_workspace`.
  - File size is capped — huge files are refused rather than streamed into
    the LLM context. The agent should grep for a line range instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools.base import (
    ToolResult,
    ToolSchema,
    register,
)

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# read_file
# ─────────────────────────────────────────────────────────────────────────────

# Hard size cap on a single read (in bytes). 256 KiB is comfortably above
# the largest source files in typical repos; anything bigger should be
# read via grep/glob or a line-range slice rather than a full read.
_MAX_READ_BYTES: int = 256 * 1024

READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description=(
        "Read a file from the repository workspace, optionally limited to a "
        "line range. Paths are resolved relative to the workspace root; "
        "attempts to read files outside the workspace are rejected. Files "
        "larger than 256 KiB must be read with a line range."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the repository workspace root.",
            },
            "line_start": {
                "type": "integer",
                "description": "Optional 1-indexed inclusive start line.",
                "minimum": 1,
            },
            "line_end": {
                "type": "integer",
                "description": (
                    "Optional 1-indexed inclusive end line. Must be >= line_start."
                ),
                "minimum": 1,
            },
        },
        "required": ["path"],
    },
)


def _resolve_in_workspace(workspace: str, requested_path: str) -> Path | None:
    """Return an absolute Path only if `requested_path` resolves inside the
    workspace. Returns None for traversal attempts or non-existent workspace.
    """
    try:
        workspace_real = Path(workspace).resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None

    candidate = (workspace_real / requested_path).resolve(strict=False)
    # os.path.commonpath raises on different drives (Windows); we're on
    # posix but the try/except keeps this robust.
    try:
        common = Path(os.path.commonpath([str(workspace_real), str(candidate)]))
    except ValueError:
        return None

    if common != workspace_real:
        return None
    return candidate


async def _handle_read_file(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    path_input = tool_input.get("path")
    if not path_input or not isinstance(path_input, str):
        return ToolResult(ok=False, error="path is required")

    line_start = tool_input.get("line_start")
    line_end = tool_input.get("line_end")
    if line_start is not None and line_end is not None and line_end < line_start:
        return ToolResult(ok=False, error="line_end must be >= line_start")

    resolved = _resolve_in_workspace(ctx.repo_workspace_path, path_input)
    if resolved is None:
        return ToolResult(
            ok=False,
            error=f"path_outside_workspace: {path_input!r}",
        )
    if not resolved.exists():
        return ToolResult(ok=False, error=f"file_not_found: {path_input!r}")
    if not resolved.is_file():
        return ToolResult(ok=False, error=f"not_a_file: {path_input!r}")

    size = resolved.stat().st_size
    if size > _MAX_READ_BYTES and line_start is None:
        return ToolResult(
            ok=False,
            error=(
                f"file_too_large: {size} bytes > {_MAX_READ_BYTES}. "
                "Pass line_start/line_end to read a slice."
            ),
        )

    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return ToolResult(ok=False, error=f"read_failed: {exc}")

    total_lines = len(all_lines)
    if line_start is not None:
        end = line_end if line_end is not None else total_lines
        # Convert to 0-indexed slice, clamp to bounds.
        start_idx = max(0, min(line_start - 1, total_lines))
        end_idx = max(start_idx, min(end, total_lines))
        selected = all_lines[start_idx:end_idx]
    else:
        selected = all_lines

    content = "".join(selected)
    return ToolResult(
        ok=True,
        data={
            "content": content,
            "line_count": total_lines,
            "line_start": line_start if line_start is not None else 1,
            "line_end": (
                line_end
                if line_end is not None
                else total_lines
                if line_start is None
                else min(line_start - 1 + len(selected), total_lines)
            ),
        },
    )


class _ReadFileTool:
    schema = READ_FILE_SCHEMA
    handler = staticmethod(_handle_read_file)


_read_file_tool = _ReadFileTool()
register(_read_file_tool)


# ─────────────────────────────────────────────────────────────────────────────
# grep  (Week 1.6b)
# ─────────────────────────────────────────────────────────────────────────────

import re as _re  # noqa: E402 — placed with grep logic, not at top, for locality

_GREP_MAX_MATCHES_DEFAULT: int = 200
_GREP_MAX_FILE_BYTES: int = 2 * 1024 * 1024  # skip files > 2 MiB
_GREP_DEFAULT_EXCLUDES = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
    }
)

GREP_SCHEMA = ToolSchema(
    name="grep",
    description=(
        "Search for a regex pattern in files under the workspace. Returns "
        "up to max_matches hits as {file, line, text}. Skips common vendored "
        "and generated directories (.git, node_modules, __pycache__, etc.) "
        "and files larger than 2 MiB. Pattern is a Python regex; use "
        "anchors (^$) and escapes as needed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python regex pattern.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional subdirectory relative to workspace root "
                    "(defaults to workspace root)."
                ),
            },
            "max_matches": {
                "type": "integer",
                "description": "Hard cap on matches (default 200, max 1000).",
                "minimum": 1,
                "maximum": 1000,
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive match (default false).",
            },
        },
        "required": ["pattern"],
    },
)


def _iter_candidate_files(root: Path, excludes: frozenset[str]):
    """Yield every file path under root, skipping excluded dir names."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in place so os.walk prunes descent into excludes.
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for fname in filenames:
            yield Path(dirpath) / fname


async def _handle_grep(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    pattern = tool_input.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return ToolResult(ok=False, error="pattern is required")

    flags = _re.IGNORECASE if tool_input.get("case_insensitive") else 0
    try:
        regex = _re.compile(pattern, flags)
    except _re.error as exc:
        return ToolResult(ok=False, error=f"invalid_regex: {exc}")

    max_matches_raw = tool_input.get("max_matches")
    max_matches = (
        max(1, min(int(max_matches_raw), 1000))
        if isinstance(max_matches_raw, int)
        else _GREP_MAX_MATCHES_DEFAULT
    )

    subpath = tool_input.get("path") or "."
    resolved = _resolve_in_workspace(ctx.repo_workspace_path, subpath)
    if resolved is None:
        return ToolResult(ok=False, error=f"path_outside_workspace: {subpath!r}")
    if not resolved.exists():
        return ToolResult(ok=False, error=f"path_not_found: {subpath!r}")
    root = resolved if resolved.is_dir() else resolved.parent

    matches: list[dict[str, Any]] = []
    truncated = False
    files_scanned = 0
    workspace_root = Path(ctx.repo_workspace_path).resolve(strict=False)

    for fpath in _iter_candidate_files(root, _GREP_DEFAULT_EXCLUDES):
        if len(matches) >= max_matches:
            truncated = True
            break
        try:
            if fpath.stat().st_size > _GREP_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        try:
            with fpath.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if regex.search(line):
                        try:
                            rel = str(fpath.relative_to(workspace_root))
                        except ValueError:
                            rel = str(fpath)
                        matches.append(
                            {
                                "file": rel,
                                "line": lineno,
                                "text": line.rstrip("\n"),
                            }
                        )
                        if len(matches) >= max_matches:
                            truncated = True
                            break
        except OSError:
            continue
        files_scanned += 1

    return ToolResult(
        ok=True,
        data={
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
            "files_scanned": files_scanned,
        },
    )


class _GrepTool:
    schema = GREP_SCHEMA
    handler = staticmethod(_handle_grep)


_grep_tool = _GrepTool()
register(_grep_tool)


# ─────────────────────────────────────────────────────────────────────────────
# glob  (Week 1.6b)
# ─────────────────────────────────────────────────────────────────────────────

_GLOB_MAX_FILES_DEFAULT: int = 500

GLOB_SCHEMA = ToolSchema(
    name="glob",
    description=(
        "Find files matching a shell-style glob under the workspace. "
        "Supports `*`, `?`, `**/` recursion. Returns up to max_files paths "
        "relative to the workspace root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '**/*.py', 'src/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "Optional subdirectory relative to workspace root.",
            },
            "max_files": {
                "type": "integer",
                "description": "Hard cap on result size (default 500, max 5000).",
                "minimum": 1,
                "maximum": 5000,
            },
        },
        "required": ["pattern"],
    },
)


async def _handle_glob(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    pattern = tool_input.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return ToolResult(ok=False, error="pattern is required")

    max_files_raw = tool_input.get("max_files")
    max_files = (
        max(1, min(int(max_files_raw), 5000))
        if isinstance(max_files_raw, int)
        else _GLOB_MAX_FILES_DEFAULT
    )

    subpath = tool_input.get("path") or "."
    resolved = _resolve_in_workspace(ctx.repo_workspace_path, subpath)
    if resolved is None:
        return ToolResult(ok=False, error=f"path_outside_workspace: {subpath!r}")
    if not resolved.exists() or not resolved.is_dir():
        return ToolResult(ok=False, error=f"path_not_a_directory: {subpath!r}")

    workspace_root = Path(ctx.repo_workspace_path).resolve(strict=False)
    matches: list[str] = []
    truncated = False

    # pathlib.Path.rglob handles '**/...' patterns; for a plain glob it
    # behaves like glob. Either way this single call covers the spec.
    try:
        iterator = resolved.rglob(pattern) if pattern.startswith("**/") or "**/" in pattern else resolved.glob(pattern)
        for p in iterator:
            if not p.is_file():
                continue
            # Skip excluded directories in the path.
            if any(part in _GREP_DEFAULT_EXCLUDES for part in p.parts):
                continue
            try:
                rel = str(p.relative_to(workspace_root))
            except ValueError:
                rel = str(p)
            matches.append(rel)
            if len(matches) >= max_files:
                truncated = True
                break
    except ValueError as exc:
        # rglob/glob raise ValueError for absolute or otherwise bad patterns.
        return ToolResult(ok=False, error=f"invalid_pattern: {exc}")

    return ToolResult(
        ok=True,
        data={
            "files": matches,
            "file_count": len(matches),
            "truncated": truncated,
        },
    )


class _GlobTool:
    schema = GLOB_SCHEMA
    handler = staticmethod(_handle_glob)


_glob_tool = _GlobTool()
register(_glob_tool)
