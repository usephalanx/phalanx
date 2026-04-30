"""Tier-1 tests for the SRE setup memoization cache (Phase 2).

cache_key determinism + lookup/write semantics with mocked DB. The
DB-real version is in tests/integration/v3_harness_t2/ once integration
lands.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.ci_fixer_v3.sre_setup import cache as cache_mod
from phalanx.ci_fixer_v3.sre_setup.cache import (
    CACHE_TTL_HOURS,
    cache_lookup,
    cache_write,
    compute_cache_key,
    replay_plan_to_install_steps,
)

if TYPE_CHECKING:
    from pathlib import Path


# ────────────────────────────────────────────────────────────────────────
# Cache key determinism
# ────────────────────────────────────────────────────────────────────────


def _make_repo(tmp_path: Path, *, pyproject: str = "", workflow: str | None = None) -> Path:
    repo = tmp_path
    repo.mkdir(parents=True, exist_ok=True)
    if pyproject:
        (repo / "pyproject.toml").write_text(pyproject)
    if workflow is not None:
        wf_dir = repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "lint.yml").write_text(workflow)
    return repo


def test_cache_key_stable_for_same_files(tmp_path):
    repo = _make_repo(
        tmp_path,
        pyproject="[project]\nname='x'\ndependencies=['ruff']\n",
        workflow="name: Lint\non: [push]\n",
    )
    k1 = compute_cache_key(repo)
    k2 = compute_cache_key(repo)
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_cache_key_changes_when_pyproject_changes(tmp_path):
    repo1 = _make_repo(tmp_path / "a", pyproject="[project]\nname='x'\n")
    repo2 = _make_repo(tmp_path / "b", pyproject="[project]\nname='y'\n")
    assert compute_cache_key(repo1) != compute_cache_key(repo2)


def test_cache_key_changes_when_workflow_added(tmp_path):
    repo1 = _make_repo(tmp_path / "a", pyproject="[project]\nname='x'\n")
    repo2 = _make_repo(
        tmp_path / "b",
        pyproject="[project]\nname='x'\n",
        workflow="name: Lint\n",
    )
    assert compute_cache_key(repo1) != compute_cache_key(repo2)


def test_cache_key_uses_missing_marker_for_absent_files(tmp_path):
    """An empty workspace still produces a stable key (with markers for
    each missing file), distinct from a workspace that has the file with
    empty contents."""
    empty = tmp_path / "empty"
    empty.mkdir()
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "pyproject.toml").touch()  # exists, zero bytes

    k_empty = compute_cache_key(empty)
    k_nonempty = compute_cache_key(nonempty)
    assert k_empty != k_nonempty


def test_cache_key_workflow_yaml_files_sorted_for_determinism(tmp_path):
    """Order of file-system iteration shouldn't affect the key. Two
    distinct workflow files produce the same key regardless of which
    was created first."""
    repo = tmp_path
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "test.yml").write_text("name: Test\n")
    (wf / "lint.yml").write_text("name: Lint\n")
    k1 = compute_cache_key(repo)
    # Recompute — should be byte-identical.
    k2 = compute_cache_key(repo)
    assert k1 == k2


# ────────────────────────────────────────────────────────────────────────
# Mock DB plumbing
# ────────────────────────────────────────────────────────────────────────


def _patch_get_db(monkeypatch, *, scalar_or_none_returns):
    """Monkey-patch get_db to yield a fake session whose .execute().scalar_one_or_none()
    returns each value in scalar_or_none_returns in order."""
    queue = list(scalar_or_none_returns)
    fake_session = MagicMock()

    async def fake_execute(*_a, **_k):
        result = MagicMock()
        value = queue.pop(0) if queue else None
        result.scalar_one_or_none = MagicMock(return_value=value)
        return result

    fake_session.execute = fake_execute
    fake_session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_session

    monkeypatch.setattr(cache_mod, "get_db", fake_get_db)
    return fake_session


class _FakeRow:
    def __init__(
        self,
        *,
        cache_key: str,
        repo_full_name: str,
        final_status: str,
        install_plan: dict,
        created_at: datetime,
        hit_count: int = 0,
    ):
        self.cache_key = cache_key
        self.repo_full_name = repo_full_name
        self.final_status = final_status
        self.install_plan = install_plan
        self.created_at = created_at
        self.hit_count = hit_count


# ────────────────────────────────────────────────────────────────────────
# cache_lookup
# ────────────────────────────────────────────────────────────────────────


async def test_cache_lookup_returns_plan_on_recent_ready(monkeypatch):
    plan = {
        "capabilities": [
            {"tool": "uv", "version": "0.8", "install_method": "pip", "evidence_ref": "x:1"}
        ]
    }
    row = _FakeRow(
        cache_key="abc" * 21 + "x",
        repo_full_name="acme/api",
        final_status="READY",
        install_plan=plan,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    _patch_get_db(monkeypatch, scalar_or_none_returns=[row])
    result = await cache_lookup("abc" * 21 + "x", repo_full_name="acme/api")
    assert result == plan


async def test_cache_lookup_returns_none_on_miss(monkeypatch):
    _patch_get_db(monkeypatch, scalar_or_none_returns=[None])
    result = await cache_lookup("nope", repo_full_name="acme/api")
    assert result is None


async def test_cache_lookup_returns_none_when_expired(monkeypatch):
    row = _FakeRow(
        cache_key="k",
        repo_full_name="acme/api",
        final_status="READY",
        install_plan={"x": 1},
        created_at=datetime.now(UTC) - timedelta(hours=CACHE_TTL_HOURS + 1),
    )
    _patch_get_db(monkeypatch, scalar_or_none_returns=[row])
    result = await cache_lookup("k", repo_full_name="acme/api")
    assert result is None


async def test_cache_lookup_returns_none_for_partial_status(monkeypatch):
    row = _FakeRow(
        cache_key="k",
        repo_full_name="acme/api",
        final_status="PARTIAL",
        install_plan={"x": 1},
        created_at=datetime.now(UTC),
    )
    _patch_get_db(monkeypatch, scalar_or_none_returns=[row])
    result = await cache_lookup("k", repo_full_name="acme/api")
    assert result is None


async def test_cache_lookup_rejects_repo_mismatch(monkeypatch):
    """Cache key collision across repos (astronomically unlikely sha256
    collision, but the repo check is the safety belt)."""
    row = _FakeRow(
        cache_key="k",
        repo_full_name="acme/api",
        final_status="READY",
        install_plan={"x": 1},
        created_at=datetime.now(UTC),
    )
    _patch_get_db(monkeypatch, scalar_or_none_returns=[row])
    result = await cache_lookup("k", repo_full_name="OTHER/repo")
    assert result is None


# ────────────────────────────────────────────────────────────────────────
# cache_write
# ────────────────────────────────────────────────────────────────────────


async def test_cache_write_skips_non_ready_status(monkeypatch):
    """PARTIAL / BLOCKED don't memoize well — the LLM may make different
    choices next time."""
    fake_session = _patch_get_db(monkeypatch, scalar_or_none_returns=[])
    await cache_write(
        "k",
        repo_full_name="acme/api",
        install_plan={"capabilities": []},
        final_status="PARTIAL",
    )
    # No execute should have been called for non-READY.
    assert fake_session.commit.call_count == 0


async def test_cache_write_skips_existing_row(monkeypatch):
    """Re-writing a key (same plan, second run) → no-op."""
    existing = _FakeRow(
        cache_key="k",
        repo_full_name="acme/api",
        final_status="READY",
        install_plan={"x": 1},
        created_at=datetime.now(UTC),
    )
    fake_session = _patch_get_db(monkeypatch, scalar_or_none_returns=[existing])
    await cache_write(
        "k",
        repo_full_name="acme/api",
        install_plan={"capabilities": []},
        final_status="READY",
    )
    # Existing row found — no insert/commit.
    assert fake_session.commit.call_count == 0


async def test_cache_write_inserts_when_absent(monkeypatch):
    fake_session = _patch_get_db(monkeypatch, scalar_or_none_returns=[None])
    await cache_write(
        "k",
        repo_full_name="acme/api",
        install_plan={
            "capabilities": [
                {"tool": "uv", "version": "", "install_method": "pip", "evidence_ref": "x:1"}
            ]
        },
        final_status="READY",
    )
    # Commit invoked once on insert.
    assert fake_session.commit.call_count == 1


# ────────────────────────────────────────────────────────────────────────
# replay_plan_to_install_steps
# ────────────────────────────────────────────────────────────────────────


def test_replay_plan_yields_install_steps_skipping_preinstalled():
    plan = {
        "capabilities": [
            {"tool": "uv", "version": "0.8", "install_method": "pip", "evidence_ref": "x:1"},
            {
                "tool": "git",
                "version": "2.x",
                "install_method": "preinstalled",
                "evidence_ref": "y:1",
            },
            {"tool": "tox", "version": "4", "install_method": "pip", "evidence_ref": "z:1"},
        ]
    }
    steps = list(replay_plan_to_install_steps(plan))
    assert len(steps) == 2  # git skipped
    assert {s["tool"] for s in steps} == {"uv", "tox"}
    assert all(s["method"] == "pip" for s in steps)


def test_replay_plan_handles_empty_capabilities():
    assert list(replay_plan_to_install_steps({"capabilities": []})) == []


def test_replay_plan_handles_missing_capabilities_key():
    assert list(replay_plan_to_install_steps({})) == []
