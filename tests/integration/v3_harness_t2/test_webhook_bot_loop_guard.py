"""Webhook bot-loop guard tests (bug #11 A1 mitigation).

When v3 commits a partial fix and pushes, the new commit triggers a fresh
CI build whose failures fire fresh webhooks. Without filtering, we'd
dispatch a parallel v3 run for the same PR (the bug surfaced 2026-04-28
on testbed PR #18, runs 23232a26 + 87b13279).

The fix: v3 engineer writes the commit SHA to CIFixRun.fix_commit_sha;
webhook handler queries `_is_phalanx_fix_commit(repo, head_sha)` and
returns ignored if matched.

These tests verify the lookup function — full webhook flow is covered
by manual/canary testing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from phalanx.api.routes.ci_webhooks import _is_phalanx_fix_commit
from phalanx.db.models import CIFixRun, CIIntegration

pytest_plugins = ["pytest_asyncio"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def integration_row(db_session, cifix_project):
    """Minimal CIIntegration referenced by ci_fix_runs.integration_id."""
    integ = CIIntegration(
        repo_full_name="usephalanx/test-bot-loop",
        ci_provider="github_actions",
        github_token="ghp_fake",
        enabled=True,
    )
    db_session.add(integ)
    await db_session.flush()
    return integ


async def _add_run(db_session, integration_row, *, fix_sha, when=None):
    row = CIFixRun(
        integration_id=integration_row.id,
        repo_full_name=integration_row.repo_full_name,
        branch="pr/123",
        commit_sha="0" * 40,
        ci_provider="github_actions",
        ci_build_id=f"build-{fix_sha[:6] if fix_sha else 'none'}",
        status="PENDING",
        attempt=1,
        fix_commit_sha=fix_sha,
    )
    db_session.add(row)
    await db_session.flush()
    if when is not None:
        # SQLAlchemy will set created_at via default; override after flush
        # to test the 1-hour window.
        from sqlalchemy import update as sa_update

        await db_session.execute(
            sa_update(CIFixRun).where(CIFixRun.id == row.id).values(created_at=when)
        )
        await db_session.flush()
    return row


async def test_returns_true_when_head_sha_matches_full_fix_sha(db_session, integration_row):
    """v3 stores the full 40-char sha; webhook gives the full 40-char sha;
    exact match should be detected."""
    full_sha = "f6097af8756ef9161d20bd8c94c168d2eff8d05a"
    await _add_run(db_session, integration_row, fix_sha=full_sha)
    await db_session.commit()  # so _is_phalanx_fix_commit (separate session) sees it

    try:
        assert await _is_phalanx_fix_commit(integration_row.repo_full_name, full_sha) is True
    finally:
        # Clean up across-session writes
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()


async def test_returns_true_for_v1v2_short_prefix_stored(db_session, integration_row):
    """v1/v2 stored 8-char prefix; head_sha.startswith(short) must still match."""
    full_sha = "abcdef1234567890" + "0" * 24
    short_prefix = "abcdef12"
    await _add_run(db_session, integration_row, fix_sha=short_prefix)
    await db_session.commit()

    try:
        assert await _is_phalanx_fix_commit(integration_row.repo_full_name, full_sha) is True
    finally:
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()


async def test_returns_false_when_no_matching_row(db_session, integration_row):
    """Different SHA → not our commit → must dispatch normally."""
    await _add_run(db_session, integration_row, fix_sha="aa" * 20)
    await db_session.commit()

    try:
        assert await _is_phalanx_fix_commit(integration_row.repo_full_name, "bb" * 20) is False
    finally:
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()


async def test_returns_false_for_old_match_outside_1h_window(db_session, integration_row):
    """An ancient fix_commit_sha shouldn't keep blocking webhooks forever.
    The query bounds to created_at >= now - 1 hour."""
    full_sha = "deadbeef" * 5
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    await _add_run(db_session, integration_row, fix_sha=full_sha, when=two_hours_ago)
    await db_session.commit()

    try:
        assert await _is_phalanx_fix_commit(integration_row.repo_full_name, full_sha) is False
    finally:
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()


async def test_returns_false_when_fix_commit_sha_null(db_session, integration_row):
    """A CIFixRun that didn't commit (engineer FAILED) leaves fix_commit_sha
    NULL. Must not match any head_sha."""
    await _add_run(db_session, integration_row, fix_sha=None)
    await db_session.commit()

    try:
        assert await _is_phalanx_fix_commit(integration_row.repo_full_name, "cc" * 20) is False
    finally:
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()


async def test_repo_scoped_does_not_match_other_repo(db_session, integration_row):
    """A fix in repo X must not block dispatch on repo Y, even if SHAs collide
    (low odds, but the function is repo-scoped on purpose)."""
    full_sha = "1234567890" * 4
    await _add_run(db_session, integration_row, fix_sha=full_sha)
    await db_session.commit()

    try:
        # Same SHA, different repo → no match
        assert await _is_phalanx_fix_commit("usephalanx/different-repo", full_sha) is False
    finally:
        await db_session.execute(
            CIFixRun.__table__.delete().where(
                CIFixRun.repo_full_name == integration_row.repo_full_name
            )
        )
        await db_session.commit()
