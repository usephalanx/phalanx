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

# v1.7.2.8 — bounded-read efficiency rules. Large source files (e.g.
# inflect's __init__.py at ~2500 lines) used to be read whole; that
# burned half the TL tool-call budget on a single call. These thresholds
# force the LLM to use find_symbol + bounded reads instead.
_FULL_READ_LINE_LIMIT: int = 500
"""Files at or below this line count may be read fully without a range."""
_FULL_READ_OVERRIDE_LIMIT: int = 1000
"""With reason='need_full_file', files up to this line count may be read fully."""
_FULL_READ_HARD_CEILING: int = 2000
"""Even with reason='need_full_file', files above this line count are
NEVER fully readable. TL must use find_symbol + around_line."""
_AROUND_LINE_DEFAULT_CONTEXT: int = 40

READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description=(
        "Read a file from the repository workspace. THREE modes:\n"
        "  (a) line_start/line_end — inclusive range slice.\n"
        "  (b) around_line + context — read context lines on each side of a "
        "target line (default context=40). Use this after find_symbol.\n"
        "  (c) no range — full read; ALLOWED ONLY for files <= 500 lines, "
        "OR <= 1000 lines if reason='need_full_file' is set. NEVER allowed "
        "for files > 2000 lines.\n"
        "Paths are resolved relative to workspace root; traversal is rejected. "
        "Files > 256 KiB must always use a range or around_line."
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
            "around_line": {
                "type": "integer",
                "description": (
                    "1-indexed target line. Returns lines "
                    "[around_line - context, around_line + context]. "
                    "Mutually exclusive with line_start/line_end (around_line wins)."
                ),
                "minimum": 1,
            },
            "context": {
                "type": "integer",
                "description": (
                    "Lines of context on each side of around_line "
                    f"(default {_AROUND_LINE_DEFAULT_CONTEXT}, max 200)."
                ),
                "minimum": 1,
                "maximum": 200,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Set to 'need_full_file' to allow a full read of files "
                    "501-1000 lines. Required justification for the override."
                ),
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
    around_line = tool_input.get("around_line")
    context_lines = tool_input.get("context")
    reason = tool_input.get("reason")

    if line_start is not None and line_end is not None and line_end < line_start:
        return ToolResult(ok=False, error="line_end must be >= line_start")
    if around_line is not None and (
        not isinstance(around_line, int) or around_line < 1
    ):
        return ToolResult(ok=False, error="around_line must be a positive int")
    if context_lines is not None and (
        not isinstance(context_lines, int) or context_lines < 1 or context_lines > 200
    ):
        return ToolResult(ok=False, error="context must be int in [1, 200]")

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

    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return ToolResult(ok=False, error=f"read_failed: {exc}")

    total_lines = len(all_lines)
    size = resolved.stat().st_size

    # v1.7.2.8 — large-file guard. Around_line / line_start make this a
    # bounded read; otherwise enforce the size ceiling.
    is_bounded = around_line is not None or line_start is not None
    if not is_bounded:
        if size > _MAX_READ_BYTES:
            return ToolResult(
                ok=False,
                error=(
                    f"file_too_large: {size} bytes > {_MAX_READ_BYTES}. "
                    "Pass line_start/line_end or around_line to read a slice."
                ),
            )
        if total_lines > _FULL_READ_HARD_CEILING:
            return ToolResult(
                ok=False,
                error=(
                    f"full_read_blocked: {total_lines} lines > "
                    f"{_FULL_READ_HARD_CEILING} hard ceiling. Use find_symbol "
                    "to locate the relevant symbol, then call read_file with "
                    "around_line=<line>."
                ),
            )
        if total_lines > _FULL_READ_LINE_LIMIT:
            if reason != "need_full_file":
                return ToolResult(
                    ok=False,
                    error=(
                        f"full_read_blocked: {total_lines} lines > "
                        f"{_FULL_READ_LINE_LIMIT}. Use find_symbol + "
                        "around_line, OR pass reason='need_full_file' to "
                        f"override (allowed up to {_FULL_READ_OVERRIDE_LIMIT} "
                        "lines)."
                    ),
                )
            if total_lines > _FULL_READ_OVERRIDE_LIMIT:
                return ToolResult(
                    ok=False,
                    error=(
                        f"full_read_blocked: {total_lines} lines > "
                        f"{_FULL_READ_OVERRIDE_LIMIT} (override ceiling). Use "
                        "find_symbol + around_line."
                    ),
                )

    # Mode (b): around_line + context — wins over line_start/line_end.
    if around_line is not None:
        ctx_n = (
            context_lines if context_lines is not None else _AROUND_LINE_DEFAULT_CONTEXT
        )
        slice_start_1 = max(1, around_line - ctx_n)
        slice_end_1 = min(total_lines, around_line + ctx_n)
        start_idx = slice_start_1 - 1
        end_idx = slice_end_1
        selected = all_lines[start_idx:end_idx]
        effective_start = slice_start_1
        effective_end = slice_end_1
    elif line_start is not None:
        end = line_end if line_end is not None else total_lines
        start_idx = max(0, min(line_start - 1, total_lines))
        end_idx = max(start_idx, min(end, total_lines))
        selected = all_lines[start_idx:end_idx]
        effective_start = line_start
        effective_end = (
            line_end
            if line_end is not None
            else min(line_start - 1 + len(selected), total_lines)
        )
    else:
        selected = all_lines
        effective_start = 1
        effective_end = total_lines

    return ToolResult(
        ok=True,
        data={
            "content": "".join(selected),
            "line_count": total_lines,
            "line_start": effective_start,
            "line_end": effective_end,
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


# ─────────────────────────────────────────────────────────────────────────────
# find_symbol  (v1.7.2.8 — TL efficiency on large files)
# ─────────────────────────────────────────────────────────────────────────────

import ast as _ast  # noqa: E402

_FIND_SYMBOL_MAX_HITS: int = 20
_FIND_SYMBOL_MAX_FILE_BYTES: int = 1 * 1024 * 1024  # skip files > 1 MiB
_PY_DEF_RE = _re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_PY_CLASS_RE = _re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]")

FIND_SYMBOL_SCHEMA = ToolSchema(
    name="find_symbol",
    description=(
        "Locate the definition of a function or class by name. Use this "
        "BEFORE read_file when the file is large — it returns the line "
        "range so you can read just the relevant snippet via "
        "read_file(path, around_line=<line>). For Python files uses AST; "
        "for other languages uses a tight regex on `def NAME` / `class "
        "NAME`. Returns up to 20 matches as "
        "{file, line_start, line_end, kind, signature}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact symbol name (function or class).",
            },
            "file": {
                "type": "string",
                "description": (
                    "Optional file path relative to workspace root. "
                    "If omitted, searches all files under workspace."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["function", "class", "any"],
                "description": "Filter by symbol kind (default 'any').",
            },
        },
        "required": ["name"],
    },
)


def _ast_find_symbol(
    source: str, name: str, kind: str
) -> list[dict[str, Any]]:
    """Return matches via Python AST. Empty list on parse failure."""
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return []
    hits: list[dict[str, Any]] = []
    for node in _ast.walk(tree):
        node_kind: str | None = None
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            node_kind = "function"
        elif isinstance(node, _ast.ClassDef):
            node_kind = "class"
        else:
            continue
        if node.name != name:
            continue
        if kind != "any" and kind != node_kind:
            continue
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", line_start) or line_start
        # Build a signature: first line of source for funcs, "class NAME(...)" for classes
        sig = ""
        try:
            sig = _ast.get_source_segment(source, node) or ""
            sig = sig.splitlines()[0][:200] if sig else ""
        except Exception:  # noqa: BLE001
            sig = ""
        hits.append(
            {
                "line_start": line_start,
                "line_end": line_end,
                "kind": node_kind,
                "signature": sig,
            }
        )
    return hits


def _regex_find_symbol(
    source: str, name: str, kind: str
) -> list[dict[str, Any]]:
    """Fallback locator for non-Python files / parse failures."""
    hits: list[dict[str, Any]] = []
    lines = source.splitlines()
    for i, line in enumerate(lines, start=1):
        node_kind: str | None = None
        if kind in ("function", "any"):
            m = _PY_DEF_RE.match(line)
            if m and m.group(1) == name:
                node_kind = "function"
        if node_kind is None and kind in ("class", "any"):
            m = _PY_CLASS_RE.match(line)
            if m and m.group(1) == name:
                node_kind = "class"
        if node_kind is None:
            continue
        # Crude end-line: walk forward until a line at the same-or-lower
        # indentation that's non-blank and not a comment. Bounded scan.
        base_indent = len(line) - len(line.lstrip())
        end_line = i
        for j in range(i, min(len(lines), i + 500)):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if j > i - 1 and nxt_indent <= base_indent and j != i - 1:
                end_line = j
                break
            end_line = j + 1
        hits.append(
            {
                "line_start": i,
                "line_end": end_line,
                "kind": node_kind,
                "signature": line.strip()[:200],
            }
        )
    return hits


async def _handle_find_symbol(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    name = tool_input.get("name")
    if not name or not isinstance(name, str):
        return ToolResult(ok=False, error="name is required")

    kind = tool_input.get("kind") or "any"
    if kind not in ("function", "class", "any"):
        return ToolResult(ok=False, error="kind must be function|class|any")

    file_input = tool_input.get("file")

    workspace_root = Path(ctx.repo_workspace_path).resolve(strict=False)

    candidates: list[Path] = []
    if file_input:
        resolved = _resolve_in_workspace(ctx.repo_workspace_path, file_input)
        if resolved is None:
            return ToolResult(ok=False, error=f"path_outside_workspace: {file_input!r}")
        if not resolved.exists() or not resolved.is_file():
            return ToolResult(ok=False, error=f"file_not_found: {file_input!r}")
        candidates = [resolved]
    else:
        for fpath in _iter_candidate_files(workspace_root, _GREP_DEFAULT_EXCLUDES):
            try:
                if fpath.stat().st_size > _FIND_SYMBOL_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            # Quick filter — only scan source-like files.
            if fpath.suffix not in {
                ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
            }:
                continue
            candidates.append(fpath)

    matches: list[dict[str, Any]] = []
    truncated = False
    for fpath in candidates:
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        is_py = fpath.suffix in {".py", ".pyi"}
        hits = _ast_find_symbol(source, name, kind) if is_py else []
        if not hits:
            hits = _regex_find_symbol(source, name, kind)
        try:
            rel = str(fpath.relative_to(workspace_root))
        except ValueError:
            rel = str(fpath)
        for h in hits:
            matches.append({"file": rel, **h})
            if len(matches) >= _FIND_SYMBOL_MAX_HITS:
                truncated = True
                break
        if truncated:
            break

    return ToolResult(
        ok=True,
        data={
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
        },
    )


class _FindSymbolTool:
    schema = FIND_SYMBOL_SCHEMA
    handler = staticmethod(_handle_find_symbol)


_find_symbol_tool = _FindSymbolTool()
register(_find_symbol_tool)
