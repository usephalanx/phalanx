"""Tier-1 tests for v1.7.1 setup recipe cache.

Covers:
  - compute_cache_key stability + invalidation under file content change
  - lookup miss / hit / invalidated entry skipped
  - store + lookup roundtrip
  - invalidate masks earlier validated entries
  - per-repo file isolation (repo A's writes don't affect repo B's lookups)

All file-system; no LLM, no Postgres.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from phalanx.agents._v171_setup_cache import (
    SetupRecipe,
    compute_cache_key,
    invalidate,
    lookup,
    store,
)


@pytest.fixture
def cache_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "cache"


@pytest.fixture
def workspace():
    """A tempdir we'll populate with fake dep files per-test."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # Default minimal pyproject so cache_key has SOMETHING to hash
        (ws / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1"\n'
        )
        yield ws


def _store_validated(cache_dir, workspace, repo, tier="0",
                     commands=None, source="test"):
    return store(
        cache_dir=cache_dir,
        repo_full_name=repo,
        workflow_path=".github/workflows/test.yml",
        workspace_path=workspace,
        tier=tier,
        commands=commands or ["pip install -e ."],
        source=source,
        validated=True,
        validation_evidence={"exit_codes": [0]},
    )


# ─── compute_cache_key ───────────────────────────────────────────────────────


class TestCacheKey:
    def test_same_inputs_same_key(self, workspace):
        k1 = compute_cache_key(
            repo_full_name="acme/widget",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        k2 = compute_cache_key(
            repo_full_name="acme/widget",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert k1 == k2

    def test_repo_change_changes_key(self, workspace):
        k1 = compute_cache_key(
            repo_full_name="acme/widget",
            workflow_path="x", workspace_path=workspace,
        )
        k2 = compute_cache_key(
            repo_full_name="other/repo",
            workflow_path="x", workspace_path=workspace,
        )
        assert k1 != k2

    def test_workflow_path_change_changes_key(self, workspace):
        k1 = compute_cache_key(
            repo_full_name="r", workflow_path="a.yml",
            workspace_path=workspace,
        )
        k2 = compute_cache_key(
            repo_full_name="r", workflow_path="b.yml",
            workspace_path=workspace,
        )
        assert k1 != k2

    def test_pyproject_content_change_changes_key(self, workspace):
        k1 = compute_cache_key(
            repo_full_name="r", workflow_path="x",
            workspace_path=workspace,
        )
        # Modify pyproject content
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.2"\n'
        )
        k2 = compute_cache_key(
            repo_full_name="r", workflow_path="x",
            workspace_path=workspace,
        )
        assert k1 != k2, "key should change when pyproject content changes"

    def test_lockfile_addition_changes_key(self, workspace):
        k1 = compute_cache_key(
            repo_full_name="r", workflow_path="x",
            workspace_path=workspace,
        )
        # Add a lockfile
        (workspace / "uv.lock").write_text("# locked\n")
        k2 = compute_cache_key(
            repo_full_name="r", workflow_path="x",
            workspace_path=workspace,
        )
        assert k1 != k2, "adding uv.lock should change the cache key"

    def test_workflow_path_none_treated_as_empty(self, workspace):
        # Tier 1 lockfile-only detection has no workflow_path
        k1 = compute_cache_key(
            repo_full_name="r", workflow_path=None,
            workspace_path=workspace,
        )
        k2 = compute_cache_key(
            repo_full_name="r", workflow_path="",
            workspace_path=workspace,
        )
        assert k1 == k2


# ─── lookup ──────────────────────────────────────────────────────────────────


class TestLookup:
    def test_miss_when_no_cache_file(self, cache_dir, workspace):
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path="x", workspace_path=workspace,
        )
        assert recipe is None

    def test_hit_after_store(self, cache_dir, workspace):
        _store_validated(cache_dir, workspace, "acme/widget")
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="acme/widget",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is not None
        assert recipe.commands == ["pip install -e ."]

    def test_miss_when_dep_file_changes(self, cache_dir, workspace):
        _store_validated(cache_dir, workspace, "acme/widget")
        # Change pyproject content — cache key changes — should miss
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.99"\n'
        )
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="acme/widget",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is None

    def test_unvalidated_entry_skipped(self, cache_dir, workspace):
        # Manually write an unvalidated entry
        store(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path="x", workspace_path=workspace,
            tier="0", commands=["x"], source="t",
            validated=False, validation_evidence={},
        )
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path="x", workspace_path=workspace,
        )
        assert recipe is None

    def test_newest_validated_entry_wins(self, cache_dir, workspace):
        _store_validated(
            cache_dir, workspace, "r", commands=["old cmd"], source="old",
        )
        _store_validated(
            cache_dir, workspace, "r", commands=["new cmd"], source="new",
        )
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is not None
        assert recipe.commands == ["new cmd"]
        assert recipe.source == "new"


# ─── invalidate ──────────────────────────────────────────────────────────────


class TestInvalidate:
    def test_invalidate_masks_validated_entry(self, cache_dir, workspace):
        _store_validated(cache_dir, workspace, "r")
        # Confirm hit before invalidate
        assert lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        ) is not None

        invalidate(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
            reason="test_invalidation",
        )
        # Lookup should now miss (newest entry is unvalidated)
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is None

    def test_invalidate_then_revalidate_returns_new_recipe(
        self, cache_dir, workspace
    ):
        _store_validated(cache_dir, workspace, "r", commands=["v1"])
        invalidate(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
            reason="dep_drift",
        )
        _store_validated(cache_dir, workspace, "r", commands=["v2"])
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="r",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is not None
        assert recipe.commands == ["v2"]


# ─── Per-repo isolation ──────────────────────────────────────────────────────


class TestRepoIsolation:
    def test_repo_a_lookup_doesnt_see_repo_b_writes(self, cache_dir, workspace):
        _store_validated(cache_dir, workspace, "acme/widget")
        # Lookup for a different repo should miss even though the cache
        # file for acme/widget exists.
        recipe = lookup(
            cache_dir=cache_dir, repo_full_name="other/repo",
            workflow_path=".github/workflows/test.yml",
            workspace_path=workspace,
        )
        assert recipe is None


# ─── Persistence shape ───────────────────────────────────────────────────────


class TestPersistedShape:
    def test_jsonl_line_is_valid_json(self, cache_dir, workspace):
        _store_validated(cache_dir, workspace, "r")
        # Confirm the on-disk line parses as JSON
        cache_file = cache_dir / (
            __import__("hashlib").sha256(b"r").hexdigest()[:16] + ".jsonl"
        )
        assert cache_file.is_file()
        line = cache_file.read_text().strip().splitlines()[-1]
        parsed = json.loads(line)
        assert parsed["validated"] is True
        assert parsed["tier"] == "0"
        assert "produced_at" in parsed
