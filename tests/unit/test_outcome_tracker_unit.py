"""
Unit tests for phalanx/ci_fixer/outcome_tracker.py

Tests async helpers with mocked DB and mocked httpx — no real network or DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.outcome_tracker import (
    _check_pr_outcome,
    _mark_outcome_checked,
    _parse_iso,
    _poll_all_pending,
    _process_run,
    _record_outcome,
    _update_fingerprint,
)
from phalanx.db.models import CIFailureFingerprint, CIFixOutcome, CIFixRun

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_run(
    created_hours_ago: float = 5.0,
    fix_pr_number: int = 42,
    fingerprint_hash: str = "abc123def456abcd",
    outcome_checked: bool = False,
) -> CIFixRun:
    run = MagicMock(spec=CIFixRun)
    run.id = str(uuid.uuid4())
    run.repo_full_name = "acme/backend"
    run.integration_id = str(uuid.uuid4())
    run.fix_pr_number = fix_pr_number
    run.fingerprint_hash = fingerprint_hash
    run.validation_tool_version = "ruff 0.4.1"
    run.fix_commit_sha = "deadbeef"
    run.ci_provider = "github_actions"
    run.outcome_checked = outcome_checked
    run.created_at = datetime.now(UTC) - timedelta(hours=created_hours_ago)
    return run


# ── _parse_iso ─────────────────────────────────────────────────────────────────
# (also tested in test_ci_fixer_p2.py — just a quick sanity check here)


def test_parse_iso_z_suffix():
    dt = _parse_iso("2026-04-10T14:30:00Z")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_none():
    assert _parse_iso(None) is None


# ── _check_pr_outcome ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_pr_outcome_merged():
    """Returns outcome='merged' when PR has merged_at timestamp."""
    run = _make_run()

    merged_at = "2026-04-10T15:00:00Z"
    pr_data = {
        "state": "closed",
        "merged_at": merged_at,
        "closed_at": "2026-04-10T15:00:00Z",
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = pr_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value="ghp_token"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "merged"
    assert result["merged_at"] is not None


@pytest.mark.asyncio
async def test_check_pr_outcome_closed_unmerged():
    """Returns outcome='closed_unmerged' when PR is closed but not merged."""
    run = _make_run()

    pr_data = {
        "state": "closed",
        "merged_at": None,
        "closed_at": "2026-04-10T15:00:00Z",
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = pr_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value="ghp_token"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "closed_unmerged"


@pytest.mark.asyncio
async def test_check_pr_outcome_open():
    """Returns outcome='open' when PR is still open."""
    run = _make_run()

    pr_data = {
        "state": "open",
        "merged_at": None,
        "closed_at": None,
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = pr_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value="ghp_token"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "open"


@pytest.mark.asyncio
async def test_check_pr_outcome_not_found():
    """Returns outcome='not_found' for 404."""
    run = _make_run()

    mock_response = MagicMock()
    mock_response.status_code = 404

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value="ghp_token"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_check_pr_outcome_no_token():
    """No GitHub token → returns 'open' without calling GitHub."""
    run = _make_run()

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value=None):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "open"


@pytest.mark.asyncio
async def test_check_pr_outcome_network_error():
    """Network error → returns 'open' without raising."""
    run = _make_run()

    with patch("phalanx.ci_fixer.outcome_tracker._get_github_token", new_callable=AsyncMock, return_value="ghp_token"), \
         patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
        result = await _check_pr_outcome(run)

    assert result["outcome"] == "open"


# ── _record_outcome ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_outcome_writes_row():
    """_record_outcome inserts a CIFixOutcome row via the DB session."""
    run = _make_run()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        await _record_outcome(
            run,
            poll_number=1,
            outcome={"outcome": "merged", "pr_state": "closed",
                     "merged_at": datetime.now(UTC), "closed_at": None},
        )

    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()
    # Verify the row that was added has the right fields
    added_row = mock_session.add.call_args[0][0]
    assert isinstance(added_row, CIFixOutcome)
    assert added_row.ci_fix_run_id == run.id
    assert added_row.poll_number == 1
    assert added_row.outcome == "merged"


# ── _update_fingerprint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_fingerprint_creates_new_row_on_success():
    """When no existing fingerprint, creates a new one with success_count=1."""
    run = _make_run()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    # Mock query returning None (no existing fingerprint)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        await _update_fingerprint(run, success=True)

    mock_session.add.assert_called_once()
    added = mock_session.add.call_args[0][0]
    assert isinstance(added, CIFailureFingerprint)
    assert added.success_count == 1
    assert added.failure_count == 0


@pytest.mark.asyncio
async def test_update_fingerprint_increments_failure_count():
    """Increments failure_count on existing fingerprint when success=False."""
    run = _make_run()

    existing_fp = MagicMock(spec=CIFailureFingerprint)
    existing_fp.success_count = 2
    existing_fp.failure_count = 1
    existing_fp.seen_count = 3

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_fp

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        await _update_fingerprint(run, success=False)

    assert existing_fp.failure_count == 2
    assert existing_fp.seen_count == 4
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_update_fingerprint_no_hash_skips():
    """When run.fingerprint_hash is None, nothing is written."""
    run = _make_run(fingerprint_hash=None)
    run.fingerprint_hash = None  # explicit None

    with patch("phalanx.ci_fixer.outcome_tracker.get_db") as mock_db:
        await _update_fingerprint(run, success=True)

    # DB should never be touched
    mock_db.assert_not_called()


# ── _mark_outcome_checked ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_outcome_checked():
    """Sets outcome_checked=True on the run."""
    run = _make_run()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        await _mark_outcome_checked(run)

    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()


# ── _process_run ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_run_poll1_due():
    """run created 5h ago → poll 1 (4h threshold) is due."""
    run = _make_run(created_hours_ago=5.0)
    now = datetime.now(UTC)

    with patch("phalanx.ci_fixer.outcome_tracker._check_pr_outcome", new_callable=AsyncMock) as mock_check, \
         patch("phalanx.ci_fixer.outcome_tracker._record_outcome", new_callable=AsyncMock) as mock_record, \
         patch("phalanx.ci_fixer.outcome_tracker._update_fingerprint", new_callable=AsyncMock) as mock_update, \
         patch("phalanx.ci_fixer.outcome_tracker._mark_outcome_checked", new_callable=AsyncMock) as mock_mark, \
         patch("phalanx.ci_fixer.outcome_tracker.get_db") as mock_db:

        # No polls done yet
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_db.return_value = mock_ctx

        mock_check.return_value = {
            "outcome": "merged", "pr_state": "closed",
            "merged_at": datetime.now(UTC), "closed_at": None
        }

        await _process_run(run, now)

    # Poll 1 should have been recorded (4h threshold crossed at 5h)
    assert mock_record.call_count >= 1
    mock_update.assert_called_once_with(run, success=True)
    # Final poll (72h) not crossed → not marked checked
    mock_mark.assert_not_called()


@pytest.mark.asyncio
async def test_process_run_all_polls_done():
    """Run where all 3 polls already recorded → nothing new happens."""
    run = _make_run(created_hours_ago=80.0)
    now = datetime.now(UTC)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db") as mock_db:
        mock_result = MagicMock()
        mock_result.all.return_value = [(1,), (2,), (3,)]  # all polls done
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_db.return_value = mock_ctx

        with patch("phalanx.ci_fixer.outcome_tracker._check_pr_outcome", new_callable=AsyncMock) as mock_check:
            await _process_run(run, now)

    # Nothing new to check
    mock_check.assert_not_called()


@pytest.mark.asyncio
async def test_process_run_no_created_at():
    """run.created_at is None → early return, no polls."""
    run = _make_run()
    run.created_at = None
    now = datetime.now(UTC)

    with patch("phalanx.ci_fixer.outcome_tracker._check_pr_outcome", new_callable=AsyncMock) as mock_check:
        await _process_run(run, now)

    mock_check.assert_not_called()


# ── _poll_all_pending ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_all_pending_no_runs():
    """When no runs need checking, nothing happens."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx):
        await _poll_all_pending()  # should complete without errors


@pytest.mark.asyncio
async def test_poll_all_pending_exception_per_run_does_not_crash():
    """Exception in _process_run for one run does not abort the whole poll."""
    run1 = _make_run(created_hours_ago=5.0)
    run2 = _make_run(created_hours_ago=5.0)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [run1, run2]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    call_count = {"n": 0}

    async def broken_process(run, now):
        call_count["n"] += 1
        raise RuntimeError("simulated DB error")

    with patch("phalanx.ci_fixer.outcome_tracker.get_db", return_value=mock_ctx), \
         patch("phalanx.ci_fixer.outcome_tracker._process_run", side_effect=broken_process):
        await _poll_all_pending()

    # Both runs were attempted despite the first one failing
    assert call_count["n"] == 2
