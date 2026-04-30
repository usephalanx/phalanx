"""Agentic SRE setup tools — Phase 0.

Nine tools, each with strict input validation. Tool design principles:

  1. Validation at the tool layer, not the prompt layer. Hallucinated args
     become structured ToolResult errors, never state changes.
  2. Read-only on the workspace. None of these tools edit repo files.
  3. Sandbox exec is delegated to ctx.exec_in_sandbox so tier-1 tests can
     mock without monkey-patching globals.
  4. Every install_* tool requires evidence — see evidence.py.
  5. install_via_curl is restricted to a closed domain whitelist.
  6. The three report_* terminal tools return a sentinel ToolResult that
     the loop wrapper recognizes as "stop and exit with this status".

Phase 1 (LLM loop) consumes these via the v2-compatible ToolSchema /
ToolResult / ToolCallable types, so this file plugs into the existing
provider wiring without changes.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from phalanx.ci_fixer_v2.tools.base import ToolResult, ToolSchema
from phalanx.ci_fixer_v3.sre_setup.evidence import evidence_check
from phalanx.ci_fixer_v3.sre_setup.schemas import (
    BlockedReason,
    Capability,
    ObservedTokenStatus,
    SREToolContext,
)

# ────────────────────────────────────────────────────────────────────────
# Constraints
# ────────────────────────────────────────────────────────────────────────

# install_via_curl is intentionally narrow. Adding a domain here is a
# code change with review. NEVER take an LLM-supplied domain.
_CURL_DOMAIN_WHITELIST: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",  # GH release artifacts
        "astral.sh",
        "get.pnpm.io",
        "sh.rustup.rs",
        "deno.land",
        "go.dev",
    }
)

# Package-name shape: alnum + `-` + `_` + `.` + `+` (for pip extras like
# `uv[cli]` we accept the brackets too). Rejects shell metachars so a tool
# call like install_pip(["foo;rm -rf /"]) becomes a validation error.
_PIP_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9._+\-]+(?:\[[A-Za-z0-9._+\-,]+\])?(?:[<>=!~][^\s;|&`$]+)?$"
)
_APT_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]*$")  # Debian package naming
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

_MAX_READ_FILE_BYTES = 200_000


# ────────────────────────────────────────────────────────────────────────
# Public type aliases (mirror v2's ToolCallable shape but typed for SRE ctx)
# ────────────────────────────────────────────────────────────────────────

# Note: tools have signature (SREToolContext, dict[str, Any]) -> Awaitable[ToolResult].
# We don't formally type this here (Callable import would be unused at runtime;
# narrow type aliases under TYPE_CHECKING create their own warnings). The
# concrete handler functions below carry the full signature.


def _log_call(ctx: SREToolContext, *, tool: str, args: dict[str, Any], result: ToolResult) -> None:
    """Append a structured record of this tool call to ctx.install_log.

    Audit trail used by Task.output.setup_log[]. Sensitive args (none today,
    but future tools may) should be redacted before logging — for now all
    args go in verbatim.
    """
    ctx.install_log.append(
        {
            "tool": tool,
            "args": args,
            "ok": result.ok,
            "data": result.data if result.ok else None,
            "error": result.error,
        }
    )


# ────────────────────────────────────────────────────────────────────────
# 1. read_file
# ────────────────────────────────────────────────────────────────────────

_READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description=(
        "Read a file from the cloned repo (read-only). Use this to inspect "
        "workflow YAML, pyproject.toml, package.json, .pre-commit-config.yaml, "
        "or any setup-relevant file before deciding what to install. "
        f"Files larger than {_MAX_READ_FILE_BYTES // 1000}KB are truncated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path (e.g., 'pyproject.toml').",
            }
        },
        "required": ["path"],
    },
)


async def _read_file_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        result = ToolResult(ok=False, error="read_file: 'path' must be a non-empty string")
        _log_call(ctx, tool="read_file", args=args, result=result)
        return result

    if path.startswith("/") or ".." in path.split("/"):
        result = ToolResult(
            ok=False,
            error=f"read_file: rejected path (absolute or traversal): {path!r}",
        )
        _log_call(ctx, tool="read_file", args=args, result=result)
        return result

    workspace = Path(ctx.workspace_path).resolve()
    target = (workspace / path).resolve()
    try:
        target.relative_to(workspace)
    except ValueError:
        result = ToolResult(ok=False, error=f"read_file: {path!r} resolves outside workspace")
        _log_call(ctx, tool="read_file", args=args, result=result)
        return result

    if not target.is_file():
        result = ToolResult(ok=False, error=f"read_file: not a file: {path}")
        _log_call(ctx, tool="read_file", args=args, result=result)
        return result

    try:
        raw = target.read_bytes()
    except OSError as exc:
        result = ToolResult(ok=False, error=f"read_file: read failed: {exc}")
        _log_call(ctx, tool="read_file", args=args, result=result)
        return result

    truncated = len(raw) > _MAX_READ_FILE_BYTES
    body = raw[:_MAX_READ_FILE_BYTES].decode("utf-8", errors="replace")
    result = ToolResult(
        ok=True,
        data={"path": path, "content": body, "bytes": len(raw), "truncated": truncated},
    )
    _log_call(ctx, tool="read_file", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 2. list_workflows
# ────────────────────────────────────────────────────────────────────────

_LIST_WORKFLOWS_SCHEMA = ToolSchema(
    name="list_workflows",
    description=(
        "List all .github/workflows/*.yml + .yaml files in the repo. Helps "
        "you discover what CI workflows exist before reading them."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)


async def _list_workflows_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    workspace = Path(ctx.workspace_path).resolve()
    wf_dir = workspace / ".github" / "workflows"
    if not wf_dir.is_dir():
        result = ToolResult(ok=True, data={"workflows": []})
        _log_call(ctx, tool="list_workflows", args=args, result=result)
        return result

    found = sorted(
        str(p.relative_to(workspace))
        for p in wf_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    result = ToolResult(ok=True, data={"workflows": found})
    _log_call(ctx, tool="list_workflows", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 3. check_command_available
# ────────────────────────────────────────────────────────────────────────

_CHECK_CMD_SCHEMA = ToolSchema(
    name="check_command_available",
    description=(
        "Check whether a command/binary exists in the sandbox PATH. Returns "
        "the resolved version when available. Use after each install to "
        "verify it took, and to verify each failing-command's first token "
        "before reporting ready."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Single shell-safe token (e.g., 'uv'). No spaces or pipes.",
            }
        },
        "required": ["name"],
    },
)


async def _check_command_available_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    name = args.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
        result = ToolResult(
            ok=False,
            error=(
                f"check_command_available: invalid 'name' (must match {_TOOL_NAME_RE.pattern}), "
                f"got {name!r}"
            ),
        )
        _log_call(ctx, tool="check_command_available", args=args, result=result)
        return result

    # `command -v` is portable across sh; --version probe varies, so we
    # invoke once and accept whatever the tool emits on stdout.
    quoted = shlex.quote(name)
    cmd = f"command -v {quoted} >/dev/null 2>&1 && {quoted} --version 2>&1 | head -1"
    exec_result = await ctx.exec_in_sandbox(ctx.container_id, cmd)

    if exec_result.exit_code != 0:
        result = ToolResult(ok=True, data={"name": name, "found": False, "version": ""})
    else:
        # ExecResult doesn't currently surface stdout on success. For Phase 0
        # we record found=True with empty version; Phase 1 will extend
        # ExecResult or wrap the call to capture stdout.
        result = ToolResult(ok=True, data={"name": name, "found": True, "version": ""})
    _log_call(ctx, tool="check_command_available", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 4. install_apt
# ────────────────────────────────────────────────────────────────────────

_INSTALL_APT_SCHEMA = ToolSchema(
    name="install_apt",
    description=(
        "Install Debian/apt packages in the sandbox. REQUIRES evidence_file "
        "and evidence_line pointing to where in the repo the package is "
        "mentioned (e.g., a workflow YAML 'sudo apt install gettext' line)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": "Debian package names (e.g., ['gettext', 'libffi-dev']).",
            },
            "evidence_file": {
                "type": "string",
                "description": "Repo-relative file referencing this package.",
            },
            "evidence_line": {
                "type": "integer",
                "minimum": 1,
                "description": "1-indexed line number in evidence_file.",
            },
        },
        "required": ["packages", "evidence_file", "evidence_line"],
    },
)


async def _install_apt_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    packages = args.get("packages")
    ev_file = args.get("evidence_file")
    ev_line = args.get("evidence_line")

    if not isinstance(packages, list) or not packages or len(packages) > 20:
        result = ToolResult(
            ok=False,
            error="install_apt: 'packages' must be a non-empty list (≤20 items)",
        )
        _log_call(ctx, tool="install_apt", args=args, result=result)
        return result

    invalid = [p for p in packages if not isinstance(p, str) or not _APT_PACKAGE_RE.match(p)]
    if invalid:
        result = ToolResult(
            ok=False,
            error=(
                f"install_apt: invalid package name(s) {invalid!r} — must match "
                f"{_APT_PACKAGE_RE.pattern}"
            ),
        )
        _log_call(ctx, tool="install_apt", args=args, result=result)
        return result

    if not isinstance(ev_file, str) or not isinstance(ev_line, int):
        result = ToolResult(
            ok=False,
            error="install_apt: 'evidence_file' (str) and 'evidence_line' (int) required",
        )
        _log_call(ctx, tool="install_apt", args=args, result=result)
        return result

    ok, reason = evidence_check(ctx.workspace_path, ev_file, ev_line, packages)
    if not ok:
        result = ToolResult(ok=False, error=reason)
        _log_call(ctx, tool="install_apt", args=args, result=result)
        return result

    pkg_args = " ".join(shlex.quote(p) for p in packages)
    cmd = f"apt-get update -qq && apt-get install -y --no-install-recommends {pkg_args}"
    exec_result = await ctx.exec_in_sandbox(ctx.container_id, cmd, as_root=True)

    if exec_result.exit_code != 0:
        result = ToolResult(
            ok=False,
            error=(
                f"install_apt: apt-get failed (exit {exec_result.exit_code}): "
                f"{(exec_result.stderr_tail or '')[:300]}"
            ),
        )
        _log_call(ctx, tool="install_apt", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            "packages": packages,
            "method": "apt",
            "evidence_ref": f"{ev_file}:{ev_line}",
        },
    )
    _log_call(ctx, tool="install_apt", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 5. install_pip
# ────────────────────────────────────────────────────────────────────────

_INSTALL_PIP_SCHEMA = ToolSchema(
    name="install_pip",
    description=(
        "Install Python packages via pip in the sandbox. REQUIRES evidence_file "
        "and evidence_line pointing to where the package is mentioned in the "
        "repo (workflow YAML 'uses: astral-sh/setup-uv', pyproject deps, etc.)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
            },
            "evidence_file": {"type": "string"},
            "evidence_line": {"type": "integer", "minimum": 1},
        },
        "required": ["packages", "evidence_file", "evidence_line"],
    },
)


async def _install_pip_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    packages = args.get("packages")
    ev_file = args.get("evidence_file")
    ev_line = args.get("evidence_line")

    if not isinstance(packages, list) or not packages or len(packages) > 20:
        result = ToolResult(
            ok=False, error="install_pip: 'packages' must be a non-empty list (≤20 items)"
        )
        _log_call(ctx, tool="install_pip", args=args, result=result)
        return result

    invalid = [p for p in packages if not isinstance(p, str) or not _PIP_PACKAGE_RE.match(p)]
    if invalid:
        result = ToolResult(
            ok=False,
            error=(
                f"install_pip: invalid package spec(s) {invalid!r} — shell metachars not "
                "allowed; pip extras and version specifiers OK"
            ),
        )
        _log_call(ctx, tool="install_pip", args=args, result=result)
        return result

    if not isinstance(ev_file, str) or not isinstance(ev_line, int):
        result = ToolResult(
            ok=False,
            error="install_pip: 'evidence_file' (str) and 'evidence_line' (int) required",
        )
        _log_call(ctx, tool="install_pip", args=args, result=result)
        return result

    # Evidence checks against the BARE package names (strip extras, version
    # specifiers) since workflow YAML usually says `astral-sh/setup-uv` not
    # the literal `uv==0.8`.
    bare = [re.split(r"[<>=!~\[]", p, maxsplit=1)[0] for p in packages]
    ok, reason = evidence_check(ctx.workspace_path, ev_file, ev_line, bare)
    if not ok:
        result = ToolResult(ok=False, error=reason)
        _log_call(ctx, tool="install_pip", args=args, result=result)
        return result

    pkg_args = " ".join(shlex.quote(p) for p in packages)
    cmd = f"pip install --quiet --no-cache-dir {pkg_args}"
    exec_result = await ctx.exec_in_sandbox(ctx.container_id, cmd)

    if exec_result.exit_code != 0:
        result = ToolResult(
            ok=False,
            error=(
                f"install_pip: pip failed (exit {exec_result.exit_code}): "
                f"{(exec_result.stderr_tail or '')[:300]}"
            ),
        )
        _log_call(ctx, tool="install_pip", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            "packages": packages,
            "method": "pip",
            "evidence_ref": f"{ev_file}:{ev_line}",
        },
    )
    _log_call(ctx, tool="install_pip", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 6. install_via_curl
# ────────────────────────────────────────────────────────────────────────

_INSTALL_CURL_SCHEMA = ToolSchema(
    name="install_via_curl",
    description=(
        "Install a tool by piping its installer script through sh. RESTRICTED "
        "to a closed domain whitelist; arbitrary URLs will be rejected. "
        "REQUIRES evidence_file + evidence_line."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "install_url": {
                "type": "string",
                "description": "Full HTTPS URL of the install script.",
            },
            "evidence_file": {"type": "string"},
            "evidence_line": {"type": "integer", "minimum": 1},
        },
        "required": ["tool_name", "install_url", "evidence_file", "evidence_line"],
    },
)


def _domain_of(url: str) -> str | None:
    """Extract host portion from an HTTPS URL. Returns None for non-https."""
    if not url.startswith("https://"):
        return None
    rest = url[len("https://") :]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return host.lower() or None


async def _install_via_curl_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    tool_name = args.get("tool_name")
    install_url = args.get("install_url")
    ev_file = args.get("evidence_file")
    ev_line = args.get("evidence_line")

    if not isinstance(tool_name, str) or not _TOOL_NAME_RE.match(tool_name):
        result = ToolResult(
            ok=False,
            error=f"install_via_curl: invalid tool_name {tool_name!r}",
        )
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    if not isinstance(install_url, str):
        result = ToolResult(ok=False, error="install_via_curl: install_url must be a string")
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    host = _domain_of(install_url)
    if host is None:
        result = ToolResult(
            ok=False,
            error=f"install_via_curl: install_url must be HTTPS, got {install_url!r}",
        )
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    if host not in _CURL_DOMAIN_WHITELIST:
        result = ToolResult(
            ok=False,
            error=(
                f"install_via_curl: domain {host!r} not in whitelist. "
                f"Allowed: {sorted(_CURL_DOMAIN_WHITELIST)}"
            ),
        )
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    if not isinstance(ev_file, str) or not isinstance(ev_line, int):
        result = ToolResult(
            ok=False,
            error="install_via_curl: 'evidence_file' (str) and 'evidence_line' (int) required",
        )
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    # Evidence: tool_name OR the install URL host should appear near the line.
    ok, reason = evidence_check(ctx.workspace_path, ev_file, ev_line, [tool_name, host])
    if not ok:
        result = ToolResult(ok=False, error=reason)
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    quoted_url = shlex.quote(install_url)
    cmd = f"curl -fsSL {quoted_url} | sh"
    exec_result = await ctx.exec_in_sandbox(ctx.container_id, cmd, as_root=True)

    if exec_result.exit_code != 0:
        result = ToolResult(
            ok=False,
            error=(
                f"install_via_curl: install script failed (exit {exec_result.exit_code}): "
                f"{(exec_result.stderr_tail or '')[:300]}"
            ),
        )
        _log_call(ctx, tool="install_via_curl", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            "tool": tool_name,
            "method": "curl",
            "url": install_url,
            "evidence_ref": f"{ev_file}:{ev_line}",
        },
    )
    _log_call(ctx, tool="install_via_curl", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# 7-9. Terminal report_* tools
# ────────────────────────────────────────────────────────────────────────
#
# These don't execute anything in the sandbox. They return a sentinel
# ToolResult shape that the Phase-1 loop wrapper recognizes as "stop the
# loop with this final_status". Validation still applies — bad payloads
# from the LLM become tool errors, NOT terminal events.

_TERMINAL_MARKER = "_sre_setup_terminal"


_REPORT_READY_SCHEMA = ToolSchema(
    name="report_ready",
    description=(
        "Terminal: signal that the sandbox is fully provisioned for the "
        "observed failing commands. Call this only after verifying every "
        "first-token via check_command_available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "capabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "version": {"type": "string"},
                        "install_method": {
                            "type": "string",
                            "enum": ["apt", "pip", "curl", "preinstalled"],
                        },
                        "evidence_ref": {"type": "string"},
                    },
                    "required": ["tool", "version", "install_method", "evidence_ref"],
                },
            },
            "observed_token_status": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "first_token": {"type": "string"},
                        "found": {"type": "boolean"},
                    },
                    "required": ["cmd", "first_token", "found"],
                },
            },
        },
        "required": ["capabilities", "observed_token_status"],
    },
)


def _validate_capabilities(raw: Any) -> list[Capability] | str:
    if not isinstance(raw, list):
        return "capabilities must be a list"
    out: list[Capability] = []
    valid_methods = {"apt", "pip", "curl", "preinstalled"}
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            return f"capabilities[{i}] must be an object"
        for f in ("tool", "version", "install_method", "evidence_ref"):
            if f not in c:
                return f"capabilities[{i}] missing field {f!r}"
        if c["install_method"] not in valid_methods:
            return f"capabilities[{i}].install_method invalid (got {c['install_method']!r})"
        out.append(
            Capability(
                tool=str(c["tool"]),
                version=str(c["version"]),
                install_method=str(c["install_method"]),
                evidence_ref=str(c["evidence_ref"]),
            )
        )
    return out


def _validate_token_status(raw: Any) -> list[ObservedTokenStatus] | str:
    if not isinstance(raw, list):
        return "observed_token_status must be a list"
    out: list[ObservedTokenStatus] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            return f"observed_token_status[{i}] must be an object"
        for f in ("cmd", "first_token", "found"):
            if f not in t:
                return f"observed_token_status[{i}] missing field {f!r}"
        if not isinstance(t["found"], bool):
            return f"observed_token_status[{i}].found must be bool"
        out.append(
            ObservedTokenStatus(
                cmd=str(t["cmd"]), first_token=str(t["first_token"]), found=t["found"]
            )
        )
    return out


async def _report_ready_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    caps = _validate_capabilities(args.get("capabilities"))
    if isinstance(caps, str):
        result = ToolResult(ok=False, error=f"report_ready: {caps}")
        _log_call(ctx, tool="report_ready", args=args, result=result)
        return result
    tokens = _validate_token_status(args.get("observed_token_status"))
    if isinstance(tokens, str):
        result = ToolResult(ok=False, error=f"report_ready: {tokens}")
        _log_call(ctx, tool="report_ready", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            _TERMINAL_MARKER: True,
            "final_status": "READY",
            "capabilities": [c.__dict__ for c in caps],
            "observed_token_status": [t.__dict__ for t in tokens],
        },
    )
    _log_call(ctx, tool="report_ready", args=args, result=result)
    return result


_REPORT_PARTIAL_SCHEMA = ToolSchema(
    name="report_partial",
    description=(
        "Terminal: some installs succeeded, some gaps remain. Use when budget "
        "exhausts or you can't fully install a tool but want to surface what "
        "you did get done."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "capabilities": _REPORT_READY_SCHEMA.input_schema["properties"]["capabilities"],
            "gaps_remaining": {
                "type": "array",
                "items": {"type": "string"},
                "description": "First-tokens still missing from sandbox.",
            },
            "reason": {
                "type": "string",
                "description": "One-sentence why-it's-partial.",
            },
        },
        "required": ["capabilities", "gaps_remaining", "reason"],
    },
)


async def _report_partial_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    caps = _validate_capabilities(args.get("capabilities"))
    if isinstance(caps, str):
        result = ToolResult(ok=False, error=f"report_partial: {caps}")
        _log_call(ctx, tool="report_partial", args=args, result=result)
        return result

    gaps = args.get("gaps_remaining")
    if not isinstance(gaps, list) or not all(isinstance(g, str) for g in gaps):
        result = ToolResult(
            ok=False, error="report_partial: 'gaps_remaining' must be a list of strings"
        )
        _log_call(ctx, tool="report_partial", args=args, result=result)
        return result

    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        result = ToolResult(ok=False, error="report_partial: 'reason' must be a non-empty string")
        _log_call(ctx, tool="report_partial", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            _TERMINAL_MARKER: True,
            "final_status": "PARTIAL",
            "capabilities": [c.__dict__ for c in caps],
            "gaps_remaining": gaps,
            "reason": reason,
        },
    )
    _log_call(ctx, tool="report_partial", args=args, result=result)
    return result


_REPORT_BLOCKED_SCHEMA = ToolSchema(
    name="report_blocked",
    description=(
        "Terminal: the sandbox cannot be set up to faithfully replicate "
        "upstream CI for a specific structural reason (matrix expressions, "
        "custom container, sudo denied, etc.). Run will be ESCALATED."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": [r.value for r in BlockedReason],
                "description": "Closed enum from BlockedReason.",
            },
            "evidence": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                },
                "description": (
                    "Where in the repo this blocker is evidenced. Optional but "
                    "strongly recommended."
                ),
            },
        },
        "required": ["reason"],
    },
)


async def _report_blocked_handler(ctx: SREToolContext, args: dict[str, Any]) -> ToolResult:
    reason_raw = args.get("reason")
    if not isinstance(reason_raw, str):
        result = ToolResult(ok=False, error="report_blocked: 'reason' must be a string")
        _log_call(ctx, tool="report_blocked", args=args, result=result)
        return result
    try:
        reason = BlockedReason(reason_raw)
    except ValueError:
        valid = [r.value for r in BlockedReason]
        result = ToolResult(
            ok=False,
            error=f"report_blocked: 'reason' must be one of {valid}, got {reason_raw!r}",
        )
        _log_call(ctx, tool="report_blocked", args=args, result=result)
        return result

    evidence = args.get("evidence")
    if evidence is not None and not isinstance(evidence, dict):
        result = ToolResult(
            ok=False, error="report_blocked: 'evidence' must be an object if provided"
        )
        _log_call(ctx, tool="report_blocked", args=args, result=result)
        return result

    result = ToolResult(
        ok=True,
        data={
            _TERMINAL_MARKER: True,
            "final_status": "BLOCKED",
            "blocked_reason": reason.value,
            "evidence": evidence,
        },
    )
    _log_call(ctx, tool="report_blocked", args=args, result=result)
    return result


# ────────────────────────────────────────────────────────────────────────
# Public registry — Phase 1 loop wrapper consumes this
# ────────────────────────────────────────────────────────────────────────

SRE_SETUP_TOOLS: list[tuple[ToolSchema, Any]] = [
    (_READ_FILE_SCHEMA, _read_file_handler),
    (_LIST_WORKFLOWS_SCHEMA, _list_workflows_handler),
    (_CHECK_CMD_SCHEMA, _check_command_available_handler),
    (_INSTALL_APT_SCHEMA, _install_apt_handler),
    (_INSTALL_PIP_SCHEMA, _install_pip_handler),
    (_INSTALL_CURL_SCHEMA, _install_via_curl_handler),
    (_REPORT_READY_SCHEMA, _report_ready_handler),
    (_REPORT_PARTIAL_SCHEMA, _report_partial_handler),
    (_REPORT_BLOCKED_SCHEMA, _report_blocked_handler),
]


def is_terminal_result(result: ToolResult) -> bool:
    """True iff the result came from a report_* terminal tool (success path).

    The Phase-1 loop wrapper uses this to know when to break out of the
    iteration. Validation-error results from report_* tools are NOT terminal —
    the loop keeps going so the LLM can retry with corrected args.
    """
    return bool(result.ok and result.data.get(_TERMINAL_MARKER))
