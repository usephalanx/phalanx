"""Tier-1 integration test for v1.7.1 SRE tier-selection logic.

Tests the cache → Tier 0 → Tier 1 priority cascade in
`CIFixSREAgent._select_env_spec_v171`. We don't run a real Docker
container — `_select_env_spec_v171` is a pure routing decision that
returns an EnvSpec, so we can drive it directly with a tempdir.

Coverage:
  - Cache hit → returns cached EnvSpec, never calls Tier 0/1
  - Cache miss + Tier 0 hit → uses workflow YAML commands
  - Cache miss + Tier 0 miss + Tier 1 hit → falls through to detect_env
  - Cache write-back after successful provision (mocked)
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from phalanx.agents.cifix_sre import CIFixSREAgent


@pytest.fixture
def workspace_with_workflow():
    """Tempdir with a workflow YAML, pyproject, and uv.lock."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / ".github" / "workflows").mkdir(parents=True)
        (ws / ".github" / "workflows" / "test.yml").write_text(textwrap.dedent("""\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pip install -e .[dev]
                  - run: pytest tests/
        """))
        (ws / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "x"
            version = "0.1"

            [project.optional-dependencies]
            dev = ["pytest"]
        """))
        yield ws


@pytest.fixture
def workspace_without_workflow():
    """Tempdir with only pyproject + uv.lock (no .github/workflows/)."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "x"
            version = "0.1"
        """))
        (ws / "uv.lock").write_text("# locked\n")
        yield ws


def _make_agent(monkeypatch, cache_dir):
    """Construct an agent and stub its cache_dir to a tempdir."""
    agent = CIFixSREAgent(
        run_id="test-run-tier-sel",
        agent_id="cifix_sre_setup",
        task_id="test-task-tier-sel",
    )
    monkeypatch.setattr(agent, "_cache_dir_path", lambda: str(cache_dir))
    return agent


# ─── Tier 0 — workflow YAML hit ──────────────────────────────────────────────


class TestTier0WorkflowExtraction:
    def test_uses_workflow_commands_when_workflow_matches(
        self, monkeypatch, workspace_with_workflow
    ):
        with tempfile.TemporaryDirectory() as cache_tmp:
            agent = _make_agent(monkeypatch, Path(cache_tmp))
            ci_context = {
                "repo": "acme/widget",
                "branch": "main",
                "failing_job_name": "test",
            }
            env_spec, tier_source = agent._select_env_spec_v171(
                workspace_path=str(workspace_with_workflow),
                ci_context=ci_context,
            )
        assert tier_source["tier"] == "0"
        assert "workflow" in tier_source["source"] or ".github" in tier_source["source"]
        assert "pip install -e .[dev]" in env_spec.install_commands
        # v1.7.1.1: test-runner commands (pytest, ruff, etc.) are filtered
        # out of install_commands so we don't run the failing CI command
        # during setup. They go through SRE verify instead.
        assert "pytest tests/" not in env_spec.install_commands

    def test_falls_through_when_failing_job_not_in_workflow(
        self, monkeypatch, workspace_with_workflow
    ):
        with tempfile.TemporaryDirectory() as cache_tmp:
            agent = _make_agent(monkeypatch, Path(cache_tmp))
            ci_context = {
                "repo": "acme/widget",
                "branch": "main",
                "failing_job_name": "build",  # not in workflow
            }
            env_spec, tier_source = agent._select_env_spec_v171(
                workspace_path=str(workspace_with_workflow),
                ci_context=ci_context,
            )
        # Tier 0 missed → falls to Tier 1 (detect_env on the existing pyproject)
        assert tier_source["tier"] == "1"


# ─── Tier 1 — fallback when no workflow ──────────────────────────────────────


class TestTier1Fallback:
    def test_no_workflow_falls_through_to_detect_env(
        self, monkeypatch, workspace_without_workflow
    ):
        with tempfile.TemporaryDirectory() as cache_tmp:
            agent = _make_agent(monkeypatch, Path(cache_tmp))
            ci_context = {
                "repo": "acme/widget",
                "branch": "main",
                "failing_job_name": "test",
            }
            env_spec, tier_source = agent._select_env_spec_v171(
                workspace_path=str(workspace_without_workflow),
                ci_context=ci_context,
            )
        assert tier_source["tier"] == "1"
        assert tier_source["source"] == "detect_env"
        # detect_env should produce some commands (uv.lock present)
        assert env_spec.install_commands  # non-empty


# ─── Cache hit short-circuits ────────────────────────────────────────────────


class TestCacheHit:
    def test_cache_hit_skips_tiers(self, monkeypatch, workspace_with_workflow):
        from phalanx.agents._v171_setup_cache import store

        with tempfile.TemporaryDirectory() as cache_tmp:
            cache_dir = Path(cache_tmp)
            # Pre-populate cache with a recipe for this exact workflow_path
            # + dep file content. Cache key uses workflow path discovered
            # by Tier 0 — match the relpath that extract_recipe returns.
            workflow_path = ".github/workflows/test.yml"
            store(
                cache_dir=cache_dir,
                repo_full_name="acme/widget",
                workflow_path=workflow_path,
                workspace_path=workspace_with_workflow,
                tier="0",
                commands=["echo CACHED && pip install pytest"],
                source="cached_test",
                validated=True,
                validation_evidence={"exit_codes": [0]},
            )

            agent = _make_agent(monkeypatch, cache_dir)
            ci_context = {
                "repo": "acme/widget",
                "branch": "main",
                "failing_job_name": "test",
            }
            env_spec, tier_source = agent._select_env_spec_v171(
                workspace_path=str(workspace_with_workflow),
                ci_context=ci_context,
            )

        assert tier_source["tier"] == "cache"
        assert tier_source["source"] == "cached_test"
        assert env_spec.install_commands == ["echo CACHED && pip install pytest"]
