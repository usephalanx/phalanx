"""Tier-1 tests for SRE setup tools (Phase 0).

Every tool's input validation, evidence enforcement, domain whitelist,
and terminal-marker semantics. No Docker, no LLM, no Postgres. Target
runtime < 1s for the whole file.

Mirrors the agentic SRE design doc §9 tier-1 tests. Each test explicitly
maps back to a constraint we promised to enforce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio  # noqa: F401  - imported for the asyncio plugin

from phalanx.ci_fixer_v3.provisioner import ExecResult
from phalanx.ci_fixer_v3.sre_setup.schemas import BlockedReason, SREToolContext
from phalanx.ci_fixer_v3.sre_setup.tools import (
    SRE_SETUP_TOOLS,
    is_terminal_result,
)

if TYPE_CHECKING:
    from pathlib import Path

# pyproject.toml sets asyncio_mode = "auto", so async test functions are
# auto-detected. No pytestmark needed; sync tests stay sync without warnings.


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Repo skeleton with workflow YAML + pyproject.toml.

    Mirrors a humanize-style repo: setup-uv action + ruff + uvx tox.
    """
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "lint.yml").write_text(
        "name: Lint\n"
        "on: [push]\n"
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: astral-sh/setup-uv@v8.0.0\n"
        "      - run: uvx --with tox-uv tox -e mypy\n"
        "      - run: sudo apt-get install -y gettext\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\ndependencies = ['ruff>=0.5']\n"
    )
    return tmp_path


def make_ctx(
    workspace: Path,
    *,
    exec_result: ExecResult | None = None,
    exec_responses: list[ExecResult] | None = None,
) -> SREToolContext:
    """Build an SREToolContext with a controllable mock exec.

    Either:
      - exec_result: every call returns this (most tests)
      - exec_responses: returned in order, one per call (sequencing tests)
    """
    if exec_responses is None:
        exec_responses = [exec_result or ExecResult(ok=True, exit_code=0)]
    queue = list(exec_responses)
    calls: list[tuple] = []

    async def fake_exec(container_id: str, cmd: str, **kwargs):
        calls.append((container_id, cmd, kwargs))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    ctx = SREToolContext(
        container_id="cont-fake",
        workspace_path=str(workspace),
        exec_in_sandbox=fake_exec,
    )
    # Stash for assertions
    ctx._calls = calls  # type: ignore[attr-defined]
    return ctx


def get_handler(name: str):
    for schema, handler in SRE_SETUP_TOOLS:
        if schema.name == name:
            return handler
    raise KeyError(name)


# ────────────────────────────────────────────────────────────────────────
# read_file
# ────────────────────────────────────────────────────────────────────────


async def test_read_file_returns_content_for_real_file(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("read_file")
    r = await h(ctx, {"path": "pyproject.toml"})
    assert r.ok
    assert "ruff" in r.data["content"]
    assert r.data["bytes"] > 0
    assert r.data["truncated"] is False


async def test_read_file_rejects_path_traversal(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("read_file")
    r = await h(ctx, {"path": "../etc/passwd"})
    assert not r.ok
    assert "rejected path" in (r.error or "")


async def test_read_file_rejects_absolute_path(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("read_file")
    r = await h(ctx, {"path": "/etc/passwd"})
    assert not r.ok


async def test_read_file_rejects_missing_path_arg(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("read_file")
    r = await h(ctx, {})
    assert not r.ok
    assert "non-empty" in (r.error or "")


async def test_read_file_rejects_nonfile(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("read_file")
    r = await h(ctx, {"path": ".github/workflows"})  # directory, not file
    assert not r.ok
    assert "not a file" in (r.error or "")


async def test_read_file_truncates_oversize(tmp_path: Path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 250_000)
    ctx = make_ctx(tmp_path)
    h = get_handler("read_file")
    r = await h(ctx, {"path": "big.txt"})
    assert r.ok
    assert r.data["truncated"] is True
    assert len(r.data["content"]) <= 200_000


# ────────────────────────────────────────────────────────────────────────
# list_workflows
# ────────────────────────────────────────────────────────────────────────


async def test_list_workflows_returns_yml_files(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("list_workflows")
    r = await h(ctx, {})
    assert r.ok
    assert ".github/workflows/lint.yml" in r.data["workflows"]


async def test_list_workflows_empty_when_no_dir(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    h = get_handler("list_workflows")
    r = await h(ctx, {})
    assert r.ok
    assert r.data["workflows"] == []


# ────────────────────────────────────────────────────────────────────────
# check_command_available
# ────────────────────────────────────────────────────────────────────────


async def test_check_command_available_found(workspace):
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    h = get_handler("check_command_available")
    r = await h(ctx, {"name": "uv"})
    assert r.ok
    assert r.data["found"] is True


async def test_check_command_available_not_found(workspace):
    ctx = make_ctx(
        workspace,
        exec_result=ExecResult(ok=False, exit_code=1, stderr_tail="not found"),
    )
    h = get_handler("check_command_available")
    r = await h(ctx, {"name": "uv"})
    assert r.ok
    assert r.data["found"] is False


async def test_check_command_available_rejects_shell_metachars(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("check_command_available")
    for bad in ["uv ; rm -rf /", "uv|cat", "uv&", "uv$(whoami)", ""]:
        r = await h(ctx, {"name": bad})
        assert not r.ok, f"expected reject for {bad!r}"
        assert "invalid 'name'" in (r.error or "")


# ────────────────────────────────────────────────────────────────────────
# install_apt
# ────────────────────────────────────────────────────────────────────────


async def test_install_apt_happy_path(workspace):
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    h = get_handler("install_apt")
    r = await h(
        ctx,
        {
            "packages": ["gettext"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 9,
        },
    )
    assert r.ok, r.error
    assert r.data["method"] == "apt"
    assert r.data["evidence_ref"] == ".github/workflows/lint.yml:9"


async def test_install_apt_rejects_shell_metachars_in_package(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("install_apt")
    r = await h(
        ctx,
        {
            "packages": ["gettext;rm -rf /"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 9,
        },
    )
    assert not r.ok
    assert "invalid package name" in (r.error or "")


async def test_install_apt_requires_evidence_match(workspace):
    """Even with valid file+line, if the package name doesn't appear in the
    window, the tool refuses. This is the core "no install without
    evidence" enforcement (gap #2 from the design v1 review)."""
    ctx = make_ctx(workspace)
    h = get_handler("install_apt")
    r = await h(
        ctx,
        {
            "packages": ["postgresql-server-dev"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 5,  # line 5 is `runs-on: ubuntu-latest` — no postgres
        },
    )
    assert not r.ok
    assert "none of" in (r.error or "")


async def test_install_apt_rejects_too_many_packages(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("install_apt")
    r = await h(
        ctx,
        {
            "packages": [f"pkg{i}" for i in range(21)],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 9,
        },
    )
    assert not r.ok
    assert "non-empty list" in (r.error or "")


async def test_install_apt_returns_apt_failure_as_tool_error(workspace):
    """When apt-get itself fails, surface as ToolResult error (not raise)."""
    ctx = make_ctx(
        workspace,
        exec_result=ExecResult(ok=False, exit_code=100, stderr_tail="E: package not found"),
    )
    h = get_handler("install_apt")
    r = await h(
        ctx,
        {
            "packages": ["gettext"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 9,
        },
    )
    assert not r.ok
    assert "exit 100" in (r.error or "")
    assert "package not found" in (r.error or "")


# ────────────────────────────────────────────────────────────────────────
# install_pip
# ────────────────────────────────────────────────────────────────────────


async def test_install_pip_happy_path_with_setup_uv_evidence(workspace):
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    h = get_handler("install_pip")
    r = await h(
        ctx,
        {
            "packages": ["uv"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,  # line 7 is `uses: astral-sh/setup-uv@v8.0.0`
        },
    )
    assert r.ok, r.error
    assert r.data["method"] == "pip"


async def test_install_pip_strips_extras_for_evidence_check(workspace):
    """`uv[cli]==0.8.4` should match evidence `uv` (or `setup-uv` via the
    workflow). We strip extras+version before evidence check."""
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    h = get_handler("install_pip")
    r = await h(
        ctx,
        {
            "packages": ["uv[cli]==0.8.4"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert r.ok, r.error


async def test_install_pip_rejects_shell_injection(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("install_pip")
    r = await h(
        ctx,
        {
            "packages": ["uv;rm -rf /"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert not r.ok
    assert "shell metachars" in (r.error or "")


async def test_install_pip_rejects_unevidenced_package(workspace):
    """LLM tries to install numpy 'because all Python repos use it'.
    Workflow YAML doesn't mention numpy. Tool refuses."""
    ctx = make_ctx(workspace)
    h = get_handler("install_pip")
    r = await h(
        ctx,
        {
            "packages": ["numpy"],
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert not r.ok
    assert "none of" in (r.error or "")


# ────────────────────────────────────────────────────────────────────────
# install_via_curl
# ────────────────────────────────────────────────────────────────────────


async def test_install_via_curl_rejects_non_whitelisted_domain(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("install_via_curl")
    r = await h(
        ctx,
        {
            "tool_name": "evil",
            "install_url": "https://evil.example.com/install.sh",
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert not r.ok
    assert "not in whitelist" in (r.error or "")


async def test_install_via_curl_rejects_http(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("install_via_curl")
    r = await h(
        ctx,
        {
            "tool_name": "uv",
            "install_url": "http://astral.sh/uv/install.sh",  # plain http
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert not r.ok
    assert "must be HTTPS" in (r.error or "")


async def test_install_via_curl_accepts_whitelisted_domain_with_evidence(workspace):
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    h = get_handler("install_via_curl")
    r = await h(
        ctx,
        {
            "tool_name": "uv",
            "install_url": "https://astral.sh/uv/install.sh",
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 7,
        },
    )
    assert r.ok, r.error


async def test_install_via_curl_requires_evidence(workspace):
    """Even on a whitelisted domain, if neither tool_name nor host is in
    the evidence window, tool refuses."""
    ctx = make_ctx(workspace)
    h = get_handler("install_via_curl")
    r = await h(
        ctx,
        {
            "tool_name": "deno",
            "install_url": "https://deno.land/x/install/install.sh",
            "evidence_file": ".github/workflows/lint.yml",
            "evidence_line": 5,  # line 5 doesn't mention deno
        },
    )
    assert not r.ok


# ────────────────────────────────────────────────────────────────────────
# Terminal report_* tools
# ────────────────────────────────────────────────────────────────────────


async def test_report_ready_returns_terminal_marker(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_ready")
    r = await h(
        ctx,
        {
            "capabilities": [
                {
                    "tool": "uv",
                    "version": "0.8.4",
                    "install_method": "pip",
                    "evidence_ref": ".github/workflows/lint.yml:7",
                }
            ],
            "observed_token_status": [
                {"cmd": "uvx tox -e mypy", "first_token": "uvx", "found": True}
            ],
        },
    )
    assert r.ok
    assert is_terminal_result(r) is True
    assert r.data["final_status"] == "READY"


async def test_report_ready_rejects_invalid_install_method(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_ready")
    r = await h(
        ctx,
        {
            "capabilities": [
                {
                    "tool": "uv",
                    "version": "0.8.4",
                    "install_method": "magic",  # not in enum
                    "evidence_ref": ".github/workflows/lint.yml:7",
                }
            ],
            "observed_token_status": [],
        },
    )
    assert not r.ok
    assert "install_method invalid" in (r.error or "")
    assert is_terminal_result(r) is False  # error results are NOT terminal


async def test_report_partial_returns_terminal_marker(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_partial")
    r = await h(
        ctx,
        {
            "capabilities": [],
            "gaps_remaining": ["uvx"],
            "reason": "loop budget exhausted",
        },
    )
    assert r.ok
    assert is_terminal_result(r) is True
    assert r.data["final_status"] == "PARTIAL"


async def test_report_partial_requires_nonempty_reason(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_partial")
    r = await h(ctx, {"capabilities": [], "gaps_remaining": ["uvx"], "reason": ""})
    assert not r.ok
    assert "non-empty string" in (r.error or "")


async def test_report_blocked_with_valid_enum(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_blocked")
    for reason in BlockedReason:
        r = await h(ctx, {"reason": reason.value})
        assert r.ok, f"reason={reason.value} should be accepted"
        assert is_terminal_result(r)
        assert r.data["blocked_reason"] == reason.value


async def test_report_blocked_rejects_unknown_reason(workspace):
    ctx = make_ctx(workspace)
    h = get_handler("report_blocked")
    r = await h(ctx, {"reason": "im-just-a-string"})
    assert not r.ok
    assert "must be one of" in (r.error or "")


# ────────────────────────────────────────────────────────────────────────
# Audit log
# ────────────────────────────────────────────────────────────────────────


async def test_install_log_records_every_call(workspace):
    """Every tool call appends one entry to ctx.install_log — used as
    Task.output.setup_log[] in the production path."""
    ctx = make_ctx(workspace, exec_result=ExecResult(ok=True, exit_code=0))
    rh = get_handler("read_file")
    lh = get_handler("list_workflows")
    await rh(ctx, {"path": "pyproject.toml"})
    await lh(ctx, {})
    await rh(ctx, {"path": "../bad"})  # rejected — still logged
    assert len(ctx.install_log) == 3
    assert ctx.install_log[0]["tool"] == "read_file"
    assert ctx.install_log[0]["ok"] is True
    assert ctx.install_log[2]["ok"] is False  # the rejection


# ────────────────────────────────────────────────────────────────────────
# Schema sanity
# ────────────────────────────────────────────────────────────────────────


def test_all_nine_tools_registered():
    names = [s.name for s, _ in SRE_SETUP_TOOLS]
    expected = {
        "read_file",
        "list_workflows",
        "check_command_available",
        "install_apt",
        "install_pip",
        "install_via_curl",
        "report_ready",
        "report_partial",
        "report_blocked",
    }
    assert set(names) == expected, f"got {set(names)}"


def test_each_schema_is_valid_json_schema_shape():
    """ToolSchema input_schema must declare type=object so OpenAI/Anthropic
    accept it. Trivial structural sanity check."""
    for schema, _ in SRE_SETUP_TOOLS:
        s = schema.input_schema
        assert s.get("type") == "object", schema.name
        assert "properties" in s, schema.name
        assert "required" in s, schema.name
