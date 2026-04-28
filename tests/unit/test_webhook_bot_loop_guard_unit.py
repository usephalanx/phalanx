"""Tier-1 unit tests for the webhook bot-loop guard (bug #11 A1).

These tests don't require Postgres — they mock `get_db` and verify the
matching logic in `_is_phalanx_fix_commit`. The real DB integration is
covered by tests/integration/v3_harness_t2/test_webhook_bot_loop_guard.py.

Why both: the matching logic (`head_sha.startswith(stored_short_prefix)`)
is the bug-prone bit. Tier-1 nails it down at <1s per test run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.api.routes import ci_webhooks


class _FakeRun:
    def __init__(self, *, fix_commit_sha: str | None, hours_old: float = 0.01):
        self.fix_commit_sha = fix_commit_sha
        self.created_at = datetime.now(UTC) - timedelta(hours=hours_old)


def _patch_db_returning(monkeypatch, runs: list[_FakeRun]) -> None:
    """Wire `get_db` so it yields a session whose execute() returns scalars()
    iterator producing `runs`. The function under test only reads, so a
    minimal stub works."""

    class _ScalarIter:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return iter(self._items)

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=_ScalarIter(runs))

    @asynccontextmanager
    async def fake_get_db():
        yield fake_session

    monkeypatch.setattr(ci_webhooks, "get_db", fake_get_db)


async def test_full_sha_exact_match(monkeypatch):
    """v3 stores full 40-char sha; webhook gives full sha; must match."""
    full = "f6097af8756ef9161d20bd8c94c168d2eff8d05a"
    _patch_db_returning(monkeypatch, [_FakeRun(fix_commit_sha=full)])
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", full) is True


async def test_short_prefix_stored_v1v2_style(monkeypatch):
    """v1/v2 stored 8-char prefix; head_sha.startswith(short) → True."""
    full = "abcdef1234567890" + "0" * 24
    short = "abcdef12"
    _patch_db_returning(monkeypatch, [_FakeRun(fix_commit_sha=short)])
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", full) is True


async def test_no_rows_returns_false(monkeypatch):
    _patch_db_returning(monkeypatch, [])
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", "deadbeef" * 5) is False


async def test_mismatched_prefix_returns_false(monkeypatch):
    """Stored fix is for SHA X; webhook is for SHA Y → must not match."""
    _patch_db_returning(monkeypatch, [_FakeRun(fix_commit_sha="aaaaaaaa")])
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", "bbbbbbbb" + "0" * 32) is False


async def test_null_fix_commit_sha_does_not_match(monkeypatch):
    """A CIFixRun whose engineer FAILED leaves fix_commit_sha=None.
    Some_string.startswith(None) raises — the guard must filter Nones."""
    _patch_db_returning(monkeypatch, [_FakeRun(fix_commit_sha=None)])
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", "cccccccc" + "0" * 32) is False


async def test_multiple_rows_one_match(monkeypatch):
    """Common case post-fix: many recent CIFixRuns; only one matches our
    head_sha. Loop must short-circuit on first match (the function does
    `for ... in result.scalars(): if startswith: return True`)."""
    _patch_db_returning(
        monkeypatch,
        [
            _FakeRun(fix_commit_sha="11111111"),
            _FakeRun(fix_commit_sha="22222222"),
            _FakeRun(fix_commit_sha="match123"),  # matches
            _FakeRun(fix_commit_sha="44444444"),
        ],
    )
    head_sha = "match123" + "0" * 32
    assert await ci_webhooks._is_phalanx_fix_commit("any/repo", head_sha) is True
