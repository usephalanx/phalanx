"""Unit tests for the fetch_ci_log tool.

Tests monkeypatch the `_fetch_log_via_v1` seam so no HTTP is made. Each
test covers a branch of the handler: happy path, missing api key, unknown
provider (simulated by patching the seam to raise KeyError), generic
fetch failure, and missing required input.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg  # registers builtins
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools import diagnosis


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def _ctx(**overrides) -> AgentContext:
    defaults = dict(
        ci_fix_run_id="r1",
        repo_full_name="acme/widget",
        repo_workspace_path="/tmp/ws",
        original_failing_command="ruff check app/",
        ci_api_key="test-token",
        ci_provider="github_actions",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


async def test_fetch_ci_log_happy_path(monkeypatch):
    captured_args: dict[str, object] = {}

    async def fake_fetch(provider, repo_full_name, build_id, failed_jobs, pr_number, api_key):
        captured_args["provider"] = provider
        captured_args["repo_full_name"] = repo_full_name
        captured_args["build_id"] = build_id
        captured_args["failed_jobs"] = failed_jobs
        captured_args["api_key"] = api_key
        return "FAILED tests/test_foo.py::test_bar\nAssertionError\n"

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", fake_fetch)

    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "job-42", "failed_jobs": ["lint"]})

    assert result.ok is True
    assert "AssertionError" in result.data["log_text"]
    assert result.data["provider"] == "github_actions"
    assert result.data["job_id"] == "job-42"
    assert result.data["char_count"] == len(result.data["log_text"])
    # Seam was called with the right repo + api key from context.
    assert captured_args["repo_full_name"] == "acme/widget"
    assert captured_args["api_key"] == "test-token"
    assert captured_args["build_id"] == "job-42"
    assert captured_args["failed_jobs"] == ["lint"]


async def test_fetch_ci_log_missing_job_id_rejected():
    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {})
    assert result.ok is False
    assert "job_id" in (result.error or "")


async def test_fetch_ci_log_missing_api_key_rejected():
    ctx = _ctx(ci_api_key=None)
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "ci_api_key" in (result.error or "")


async def test_fetch_ci_log_unknown_provider_returns_clean_error(monkeypatch):
    async def raise_keyerror(*_a, **_k):
        raise KeyError("nonesuch")

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", raise_keyerror)
    ctx = _ctx(ci_provider="nonesuch")
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "unsupported_provider" in (result.error or "")


async def test_fetch_ci_log_wraps_generic_exception_as_fetch_failed(monkeypatch):
    async def raise_runtime(*_a, **_k):
        raise RuntimeError("HTTP 502")

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", raise_runtime)
    ctx = _ctx()
    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})
    assert result.ok is False
    assert "fetch_failed" in (result.error or "")


# ── Fingerprint side-effect (fix #1) ─────────────────────────────────────
# fetch_ci_log MUST parse the fetched log, compute the stable
# sha256[:16] identity, update ctx.fingerprint_hash, and persist to
# CIFixRun.fingerprint_hash. All failures are soft-logged — never abort
# the run, since a missing fingerprint degrades memory retrieval but
# doesn't prevent the agent from working.


async def test_fetch_ci_log_computes_and_persists_fingerprint(monkeypatch):
    # Fake the log fetch so we don't hit HTTP.
    async def fake_fetch(*_a, **_k):
        return "src/api.py:42:101: E501 line too long\nFound 1 error.\n"

    monkeypatch.setattr(diagnosis, "_fetch_log_via_v1", fake_fetch)

    # Fake the v1 parser + fingerprint helper via the single public
    # compute_fingerprint entry point. We DON'T patch parse_log directly;
    # instead we patch the whole side-effect helper to isolate this test.
    captured = {}

    async def fake_helper(ctx, log_text):
        captured["log_text"] = log_text
        ctx.fingerprint_hash = "abcdef1234567890"
        return "abcdef1234567890"

    monkeypatch.setattr(
        diagnosis, "_compute_and_persist_fingerprint", fake_helper
    )

    ctx = _ctx()
    assert ctx.fingerprint_hash is None

    tool = tools_base.get("fetch_ci_log")
    result = await tool.handler(ctx, {"job_id": "j1"})

    assert result.ok is True
    assert ctx.fingerprint_hash == "abcdef1234567890"
    assert result.data["fingerprint_hash"] == "abcdef1234567890"
    assert "E501" in captured["log_text"]


async def test_compute_and_persist_fingerprint_writes_ctx_and_db(monkeypatch):
    # Fake v1 parse_log + compute_fingerprint so the helper can run
    # without pulling in the full v1 dep graph at runtime.
    import sys
    import types

    fake_parsed = object()
    fake_log_parser = types.ModuleType("phalanx.ci_fixer.log_parser")

    def fake_parse_log(_raw):
        return fake_parsed

    fake_log_parser.parse_log = fake_parse_log
    monkeypatch.setitem(sys.modules, "phalanx.ci_fixer.log_parser", fake_log_parser)

    fake_v1_agent = types.ModuleType("phalanx.agents.ci_fixer")

    def fake_compute_fingerprint(parsed):
        assert parsed is fake_parsed
        return "cafebabedeadbeef"

    fake_v1_agent.compute_fingerprint = fake_compute_fingerprint
    monkeypatch.setitem(sys.modules, "phalanx.agents.ci_fixer", fake_v1_agent)

    persisted = {}

    async def fake_persist(run_id, fp):
        persisted["run_id"] = run_id
        persisted["fp"] = fp

    monkeypatch.setattr(
        diagnosis, "_persist_fingerprint_to_ci_fix_run", fake_persist
    )

    ctx = _ctx()
    result = await diagnosis._compute_and_persist_fingerprint(ctx, "some log")

    assert result == "cafebabedeadbeef"
    assert ctx.fingerprint_hash == "cafebabedeadbeef"
    assert persisted["run_id"] == ctx.ci_fix_run_id
    assert persisted["fp"] == "cafebabedeadbeef"


async def test_compute_and_persist_fingerprint_skips_when_already_set(monkeypatch):
    # If the bootstrap already seeded ctx.fingerprint_hash (from
    # CIFixRun.fingerprint_hash), don't overwrite.
    async def fake_persist(*_a, **_k):
        raise AssertionError("persist must not fire when hash already set")

    monkeypatch.setattr(
        diagnosis, "_persist_fingerprint_to_ci_fix_run", fake_persist
    )

    ctx = _ctx()
    ctx.fingerprint_hash = "preset_hash_1234"
    result = await diagnosis._compute_and_persist_fingerprint(ctx, "log")
    assert result == "preset_hash_1234"
    assert ctx.fingerprint_hash == "preset_hash_1234"


async def test_compute_and_persist_fingerprint_parse_failure_is_soft(monkeypatch):
    # If parse_log throws (e.g., corrupt log), fingerprint computation
    # silently fails. Tool still returns ok — agent can continue without
    # memory retrieval.
    import sys
    import types

    fake_log_parser = types.ModuleType("phalanx.ci_fixer.log_parser")

    def fake_parse_log(_raw):
        raise ValueError("corrupt log")

    fake_log_parser.parse_log = fake_parse_log
    monkeypatch.setitem(sys.modules, "phalanx.ci_fixer.log_parser", fake_log_parser)

    ctx = _ctx()
    result = await diagnosis._compute_and_persist_fingerprint(ctx, "log")
    assert result is None
    assert ctx.fingerprint_hash is None


async def test_compute_and_persist_fingerprint_db_failure_is_soft(monkeypatch):
    # DB-write failure (e.g., connection blip) leaves ctx populated so
    # in-memory use still works, but returns None for caller awareness.
    import sys
    import types

    fake_log_parser = types.ModuleType("phalanx.ci_fixer.log_parser")
    fake_log_parser.parse_log = lambda _raw: object()
    monkeypatch.setitem(sys.modules, "phalanx.ci_fixer.log_parser", fake_log_parser)

    fake_v1_agent = types.ModuleType("phalanx.agents.ci_fixer")
    fake_v1_agent.compute_fingerprint = lambda _p: "fp_on_ctx_only"
    monkeypatch.setitem(sys.modules, "phalanx.agents.ci_fixer", fake_v1_agent)

    async def raise_db(*_a, **_k):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(
        diagnosis, "_persist_fingerprint_to_ci_fix_run", raise_db
    )

    ctx = _ctx()
    # The compute succeeded so the helper returns the hash; DB failure
    # only affects CIFixRun.fingerprint_hash which degrades v1 outcome
    # tracking post-run but not this run's ability to fix.
    result = await diagnosis._compute_and_persist_fingerprint(ctx, "log")
    assert result == "fp_on_ctx_only"
    assert ctx.fingerprint_hash == "fp_on_ctx_only"
