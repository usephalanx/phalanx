"""v1.7 real-TL runner — drives the actual GPT-5.4 TL agent against a corpus
fixture without DB / sandbox / GitHub.

What it does:
  1. Materializes the fixture's repo_files into a temp workspace.
  2. Patches the v2 tool registry so fetch_ci_log + GitHub-API tools return
     fixture-derived synthetic data instead of hitting prod GitHub.
  3. Runs the TL investigation loop with the production v1.7 system prompt
     against real GPT-5.4.
  4. Returns the parsed fix_spec dict.

Limitations:
  - No sandbox → validate_self_critique's c3 check soft-passes (returns
    "unverified_no_sandbox"). The harness still validates structural
    correctness via the corpus invariants.
  - Cost: each fixture run is one TL pass — typically 5-10 turns,
    ~$0.30-1.00 at GPT-5.4 reasoning rates. Runner caches outputs so
    re-runs are free.
  - Requires OPENAI_API_KEY (or OPENAI_BASE_URL) configured per settings.

Usage:
  from tests.integration.v3_harness.v17_real_tl_runner import run_real_tl_against_fixture
  out = run_real_tl_against_fixture(fixture, cache_dir="/tmp/v17_tl_cache")
  # `out` is the parsed TL output dict (root_cause, task_plan, env_requirements, ...)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

# Ensure repo root on path for both interactive and pytest invocations.
import sys

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import after path setup
from phalanx.agents.cifix_techlead import (  # noqa: E402
    _build_initial_message,
    _build_techlead_context,
    _build_techlead_llm,
    _parse_fix_spec_from_text,
    _run_investigation_loop,
    _TECHLEAD_TOOLS,
)
from phalanx.ci_fixer_v2.tools import base as tools_base  # noqa: E402

# Side-effect: ensure validate_self_critique is registered
from phalanx.agents import _tl_self_critique  # noqa: F401, E402

log = logging.getLogger(__name__)


# ─── Mocked tool helpers ───────────────────────────────────────────────────────


def _make_mock_tool(name: str, return_data: dict, original_schema):
    """Build a tool object that returns canned data when invoked. Reuses the
    original tool's schema so the LLM sees the same parameter signature.
    """

    async def _handler(ctx, tool_input: dict[str, Any]):
        return tools_base.ToolResult(ok=True, data=return_data)

    class _MockTool:
        schema = original_schema
        handler = staticmethod(_handler)

    return _MockTool()


@contextlib.contextmanager
def _patched_tools_registry(fixture):
    """Replace fetch_ci_log + GitHub-API tools with fixture-aware mocks for
    the duration of the with-block. Restores the original tools on exit so
    parallel test runs aren't affected.
    """
    # Lazy import — these populate the registry on import.
    import phalanx.ci_fixer_v2.tools.diagnosis  # noqa: F401, PLC0415
    import phalanx.ci_fixer_v2.tools.reading  # noqa: F401, PLC0415

    # Names we want to mock. read_file / glob / grep stay REAL (operate on
    # the temp workspace). validate_self_critique stays REAL (real check).
    to_mock = {
        "fetch_ci_log": {"log_text": fixture.ci_log_text, "tail": fixture.ci_log_text[-2000:]},
        "get_pr_diff": {
            "diff": "",
            "files_changed": [],
            "note": "synthetic fixture — no real PR diff available",
        },
        "get_pr_context": {
            "title": f"Synthetic fixture: {fixture.name}",
            "body": fixture.description,
            "head_branch": "fixture/synthetic",
            "head_sha": "0" * 40,
            "base_branch": "main",
            "author": "fixture-bot",
            "labels": [],
        },
        "get_ci_history": {"runs": [], "note": "no history for synthetic fixture"},
        "query_fingerprint": {"matches": [], "note": "no fingerprint match"},
        "git_blame": {"blame": [], "note": "no blame for synthetic fixture"},
    }

    saved: dict[str, Any] = {}
    try:
        for name, data in to_mock.items():
            try:
                original = tools_base.get(name)
            except Exception:  # noqa: BLE001
                # tool not registered yet — skip; LLM may call it but we can't intercept
                continue
            saved[name] = original
            tools_base.register(_make_mock_tool(name, data, original.schema))
        yield
    finally:
        for name, original in saved.items():
            tools_base.register(original)


# ─── Workspace materialization ────────────────────────────────────────────────


def _materialize_workspace(fixture, dest: Path) -> None:
    """Write fixture.repo_files into dest as actual files."""
    for rel_path, content in fixture.repo_files.items():
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Initialize git so any tool that runs `git rev-parse` doesn't blow up.
    import subprocess
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(dest),
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=fixture@local", "-c", "user.name=fixture",
         "add", "-A"],
        cwd=str(dest),
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=fixture@local", "-c", "user.name=fixture",
         "commit", "--quiet", "-m", "fixture state"],
        cwd=str(dest),
        check=False,
        capture_output=True,
    )


# ─── ci_context shape (mirrors what cifix_commander would persist) ────────────


def _ci_context_from_fixture(fixture) -> dict:
    return {
        "repo": f"fixture/{fixture.name}",
        "branch": "fixture/synthetic",
        "sha": "0" * 40,
        "pr_number": fixture.pr_number,
        "failing_command": fixture.failing_command,
        "failing_job_name": fixture.failing_job_name,
        "failing_job_id": "synthetic-job-1",
    }


# ─── Caching ──────────────────────────────────────────────────────────────────


def _fixture_cache_key(fixture) -> str:
    """Stable hash of the fixture inputs — re-cache only when fixture changes."""
    h = hashlib.sha256()
    h.update(fixture.name.encode())
    h.update(fixture.ci_log_text.encode())
    h.update(fixture.failing_command.encode())
    for path in sorted(fixture.repo_files):
        h.update(path.encode())
        h.update(fixture.repo_files[path].encode())
    return h.hexdigest()[:16]


def _load_cached(cache_dir: Path, fixture) -> dict | None:
    if not cache_dir.exists():
        return None
    cache_path = cache_dir / f"{fixture.name}.{_fixture_cache_key(fixture)}.json"
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text())
    except Exception:  # noqa: BLE001
        return None


def _write_cache(cache_dir: Path, fixture, output: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{fixture.name}.{_fixture_cache_key(fixture)}.json"
    cache_path.write_text(json.dumps(output, indent=2))


# ─── Public entry point ──────────────────────────────────────────────────────


async def _run_async(fixture, workspace_path: Path) -> dict:
    """Run the TL investigation loop against a synthetic workspace.

    Returns the parsed fix_spec dict. Raises if the loop fails to emit
    valid JSON.
    """
    ci_context = _ci_context_from_fixture(fixture)
    ctx = _build_techlead_context(
        run_id=f"fixture-{fixture.name}",
        ci_context=ci_context,
        workspace_path=str(workspace_path),
        integration=None,
    )
    initial_msg = _build_initial_message(ci_context)
    ctx.messages.append({"role": "user", "content": initial_msg})

    llm_call = _build_techlead_llm(tool_names=_TECHLEAD_TOOLS)

    fix_spec, turns_used, tool_calls_used = await _run_investigation_loop(
        ctx=ctx,
        llm_call=llm_call,
        max_turns=8,
        max_tool_calls=15,
        logger=log,
    )

    # Annotate run-level meta the renderer can show
    fix_spec["_meta"] = {
        "turns_used": turns_used,
        "tool_calls_used": tool_calls_used,
        "model": "gpt-5.4",
    }
    return fix_spec


def run_real_tl_against_fixture(
    fixture, *, cache_dir: str | Path | None = None, force: bool = False
) -> dict:
    """Synchronous wrapper. If `cache_dir` is set, caches outputs by fixture
    hash; pass `force=True` to skip cache.
    """
    if cache_dir:
        cache_path = Path(cache_dir)
        if not force:
            cached = _load_cached(cache_path, fixture)
            if cached is not None:
                cached.setdefault("_meta", {})["from_cache"] = True
                return cached

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY not set — cannot run real TL. "
            "Either set the env var or use the canned outputs."
        )

    with tempfile.TemporaryDirectory(prefix=f"v17-tl-{fixture.name}-") as tmp_str:
        workspace = Path(tmp_str)
        _materialize_workspace(fixture, workspace)
        with _patched_tools_registry(fixture):
            output = asyncio.run(_run_async(fixture, workspace))

    if cache_dir:
        _write_cache(Path(cache_dir), fixture, output)
    return output
