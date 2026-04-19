"""Unit tests for phalanx.ci_fixer_v2.diag — preflight check runner.

Every external dependency (OpenAI/Anthropic SDKs, DB, Redis, docker) is
monkeypatched so the diag harness itself is tested without touching
real infrastructure. The individual check functions use their own
lazy imports so monkeypatching their module-level seams is sufficient.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import diag


async def test_check_env_vars_pass(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ok")
    result = await diag.check_env_vars()
    assert result.ok is True
    assert result.name == "env_vars"


async def test_check_env_vars_fail(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ok")
    result = await diag.check_env_vars()
    assert result.ok is False
    assert "OPENAI_API_KEY" in result.detail


async def test_run_diagnostics_skips_named_checks(monkeypatch):
    # Skip every check but env_vars — which we pre-pass.
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    skip = frozenset(
        {
            "openai_model",
            "anthropic_model",
            "database",
            "redis",
            "docker",
            "sandbox_images",
        }
    )
    results = await diag.run_diagnostics(skip=skip)
    # 7 default checks total, env_vars ran (real), others marked skipped.
    by_name = {r.name: r for r in results}
    assert by_name["env_vars"].ok is True
    for name in skip:
        assert by_name[name].detail == "skipped"
        assert by_name[name].ok is True


async def test_run_diagnostics_includes_ci_integration_when_repo_supplied(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    async def fake_ci_check(repo_full_name):
        return diag.DiagResult(
            "ci_integration", True, f"mocked pass for {repo_full_name}"
        )

    monkeypatch.setattr(diag, "check_ci_integration", fake_ci_check)

    skip = frozenset(
        {
            "openai_model",
            "anthropic_model",
            "database",
            "redis",
            "docker",
            "sandbox_images",
        }
    )
    results = await diag.run_diagnostics(repo="acme/widget", skip=skip)
    names = [r.name for r in results]
    assert "ci_integration" in names
    ci_row = next(r for r in results if r.name == "ci_integration")
    assert ci_row.ok is True
    assert "acme/widget" in ci_row.detail


async def test_render_table_contains_pass_fail_labels():
    results = [
        diag.DiagResult("env_vars", True, "all set"),
        diag.DiagResult("docker", False, "not reachable"),
    ]
    rendered = diag._render_table(results)
    assert "[PASS] env_vars" in rendered
    assert "[FAIL] docker" in rendered
    assert "not reachable" in rendered


async def test_main_async_returns_zero_when_all_pass(monkeypatch, capsys):
    async def fake_run(repo=None, skip=frozenset()):
        return [diag.DiagResult("fake", True, "ok")]

    monkeypatch.setattr(diag, "run_diagnostics", fake_run)

    import argparse

    args = argparse.Namespace(repo=None, skip=[])
    exit_code = await diag.main_async(args)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "All 1 checks passed" in out


async def test_main_async_returns_one_when_any_fail(monkeypatch, capsys):
    async def fake_run(repo=None, skip=frozenset()):
        return [
            diag.DiagResult("ok_one", True, ""),
            diag.DiagResult("bad_one", False, "broken"),
        ]

    monkeypatch.setattr(diag, "run_diagnostics", fake_run)

    import argparse

    args = argparse.Namespace(repo=None, skip=[])
    exit_code = await diag.main_async(args)
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "1 check(s) failed" in out


async def test_check_openai_model_catches_sdk_error(monkeypatch):
    # Simulate a model-not-found 404 from the OpenAI SDK.
    class _BadClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                async def create(**_kw):
                    raise RuntimeError("model 'gpt-5.4' does not exist")

        def __init__(self, **_kw):
            pass

    import sys
    import types

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _BadClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = await diag.check_openai_model()
    assert result.ok is False
    assert "does not exist" in result.detail


async def test_check_anthropic_model_catches_sdk_error(monkeypatch):
    class _BadClient:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**_kw):
                raise RuntimeError("model 'claude-sonnet-4-6' unavailable")

        def __init__(self, **_kw):
            pass

    import sys
    import types

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.AsyncAnthropic = _BadClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    result = await diag.check_anthropic_model()
    assert result.ok is False
    assert "unavailable" in result.detail


async def test_check_docker_missing_binary(monkeypatch):
    def _raise(*_a, **_k):
        raise FileNotFoundError("docker not on PATH")

    monkeypatch.setattr(diag.subprocess, "run", _raise)
    result = await diag.check_docker()
    assert result.ok is False
    assert "not on PATH" in result.detail


async def test_check_sandbox_images_detects_missing(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "phalanx-sandbox:python\nubuntu:22.04\n"
        stderr = ""

    def _run(*_a, **_k):
        return _Result()

    monkeypatch.setattr(diag.subprocess, "run", _run)
    result = await diag.check_sandbox_images()
    assert result.ok is False
    # node + multi are missing — call them out.
    assert "phalanx-sandbox:node" in result.detail
    assert "phalanx-sandbox:multi" in result.detail


async def test_check_sandbox_images_all_present(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "\n".join(
            [
                "phalanx-sandbox:python",
                "phalanx-sandbox:node",
                "phalanx-sandbox:multi",
                "postgres:16",
            ]
        )
        stderr = ""

    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: _Result())
    result = await diag.check_sandbox_images()
    assert result.ok is True
    assert "all 3 present" in result.detail
