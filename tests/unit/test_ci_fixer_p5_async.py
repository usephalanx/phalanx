"""
Phase 5 async tests:
  - pattern_promoter._promote_patterns (mocked DB)
  - proactive_scanner.scan_pr_for_patterns (mocked httpx + DB)
  - proactive_scanner._post_comment (mocked httpx)
  - proactive_scanner._record_scan (mocked DB)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.proactive_scanner import (
    ProactiveFinding,
    _post_comment,
    _record_scan,
    scan_pr_for_patterns,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _mock_db(rows=None, scalar=None):
    """Build a mock get_db() context that returns rows or a scalar."""
    mock_result = MagicMock()
    if rows is not None:
        mock_result.scalars.return_value.all.return_value = rows
        mock_result.all.return_value = rows
    if scalar is not None:
        mock_result.scalar_one_or_none.return_value = scalar

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx, mock_session


# ── scan_pr_for_patterns ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_pr_github_fetch_failure():
    """GitHub returns non-200 → empty findings."""
    mock_response = MagicMock()
    mock_response.status_code = 403

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        findings = await scan_pr_for_patterns("acme/backend", 1, "abc", "token")

    assert findings == []


@pytest.mark.asyncio
async def test_scan_pr_no_python_files():
    """PR only has non-Python files → no ruff/mypy/pytest findings."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"filename": "README.md"},
        {"filename": "src/styles.css"},
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    pattern = MagicMock()
    pattern.tool = "ruff"
    pattern.fingerprint_hash = "abc123"
    pattern.description = "unused import"
    pattern.total_success_count = 10

    mock_db_ctx, _ = _mock_db(rows=[pattern])

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("phalanx.ci_fixer.proactive_scanner.get_db", return_value=mock_db_ctx):
        findings = await scan_pr_for_patterns("acme/backend", 1, "abc", "token")

    assert findings == []


@pytest.mark.asyncio
async def test_scan_pr_with_python_files_finds_patterns():
    """PR has Python files + registry has ruff pattern → finding returned."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"filename": "src/foo.py"},
        {"filename": "src/bar.py"},
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    pattern = MagicMock()
    pattern.tool = "ruff"
    pattern.fingerprint_hash = "fp_ruff"
    pattern.description = "F401 unused import"
    pattern.total_success_count = 7

    mock_db_ctx, _ = _mock_db(rows=[pattern])

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("phalanx.ci_fixer.proactive_scanner.get_db", return_value=mock_db_ctx):
        findings = await scan_pr_for_patterns("acme/backend", 1, "abc", "token")

    assert len(findings) == 1
    assert findings[0].tool == "ruff"
    assert findings[0].severity == "warning"  # total_success_count >= 5


@pytest.mark.asyncio
async def test_scan_pr_low_success_count_is_info():
    """Pattern with < 5 successes → severity='info' not 'warning'."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"filename": "src/foo.py"}]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    pattern = MagicMock()
    pattern.tool = "ruff"
    pattern.fingerprint_hash = "fp_low"
    pattern.description = "low confidence pattern"
    pattern.total_success_count = 2  # < 5 → info

    mock_db_ctx, _ = _mock_db(rows=[pattern])

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("phalanx.ci_fixer.proactive_scanner.get_db", return_value=mock_db_ctx):
        findings = await scan_pr_for_patterns("acme/backend", 1, "abc", "token")

    assert len(findings) == 1
    assert findings[0].severity == "info"


@pytest.mark.asyncio
async def test_scan_pr_network_error_returns_empty():
    """Network error → returns empty list without raising."""
    with patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
        findings = await scan_pr_for_patterns("acme/backend", 1, "abc", "token")

    assert findings == []


# ── _post_comment ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_comment_success():
    """Successfully posts comment and returns comment ID."""
    findings = [ProactiveFinding("fp1", "ruff", "pattern", "warning", ["src/foo.py"])]

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 12345}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        comment_id = await _post_comment(
            repo_full_name="acme/backend",
            pr_number=42,
            github_token="ghp_test",
            findings=findings,
        )

    assert comment_id == 12345


@pytest.mark.asyncio
async def test_post_comment_failure_returns_none():
    """Non-2xx response → returns None without raising."""
    findings = [ProactiveFinding("fp1", "ruff", "pattern", "warning", ["src/foo.py"])]

    mock_response = MagicMock()
    mock_response.status_code = 403

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        comment_id = await _post_comment(
            repo_full_name="acme/backend",
            pr_number=42,
            github_token="ghp_test",
            findings=findings,
        )

    assert comment_id is None


@pytest.mark.asyncio
async def test_post_comment_network_error_returns_none():
    with patch("httpx.AsyncClient", side_effect=Exception("network")):
        comment_id = await _post_comment(
            repo_full_name="acme/backend",
            pr_number=42,
            github_token="ghp_test",
            findings=[ProactiveFinding("fp1", "ruff", "p", "warning", [])],
        )
    assert comment_id is None


# ── _record_scan ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_scan_inserts_row():
    findings = [ProactiveFinding("fp1", "ruff", "pattern", "warning", ["src/foo.py"])]
    mock_ctx, mock_session = _mock_db()

    with patch("phalanx.ci_fixer.proactive_scanner.get_db", return_value=mock_ctx):
        await _record_scan(
            repo_full_name="acme/backend",
            pr_number=42,
            commit_sha="abc123",
            findings=findings,
            comment_posted=True,
            comment_id=99,
            duration_ms=150,
        )

    mock_session.add.assert_called_once()
    from phalanx.db.models import CIProactiveScan
    added = mock_session.add.call_args[0][0]
    assert isinstance(added, CIProactiveScan)
    assert added.pr_number == 42
    assert added.comment_posted is True
    assert added.comment_id == 99
    assert added.scan_duration_ms == 150


# ── pattern_promoter._promote_patterns ────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_patterns_eligible_creates_registry_entry():
    """An eligible fingerprint gets promoted to the registry."""
    from phalanx.ci_fixer.pattern_promoter import _promote_patterns

    # Simulate a row from the GROUP BY query
    row = MagicMock()
    row.fingerprint_hash = "abc123def456abcd"
    row.tool = "ruff"
    row.sample_errors = "unused import"
    row.last_good_patch_json = '[{"path":"src/foo.py","start_line":1,"end_line":1,"corrected_lines":["x\\n"],"reason":""}]'
    row.repo_count = 3  # >= MIN_REPOS_FOR_PROMOTION=2
    row.total_successes = 5

    # Pattern not yet in registry
    mock_result_group = MagicMock()
    mock_result_group.all.return_value = [row]

    mock_result_registry = MagicMock()
    mock_result_registry.scalar_one_or_none.return_value = None

    call_count = {"n": 0}
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_execute(stmt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return mock_result_group
        return mock_result_registry

    mock_session.execute = mock_execute

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.pattern_promoter.get_db", return_value=mock_ctx):
        await _promote_patterns()

    # Should have added one entry to the registry
    mock_session.add.assert_called_once()
    from phalanx.db.models import CIPatternRegistry
    added = mock_session.add.call_args[0][0]
    assert isinstance(added, CIPatternRegistry)
    assert added.fingerprint_hash == "abc123def456abcd"


@pytest.mark.asyncio
async def test_promote_patterns_ineligible_skipped():
    """Fingerprint below thresholds is not promoted."""
    from phalanx.ci_fixer.pattern_promoter import _promote_patterns

    row = MagicMock()
    row.fingerprint_hash = "low_confidence"
    row.tool = "ruff"
    row.sample_errors = "test"
    row.last_good_patch_json = None
    row.repo_count = 1  # < MIN_REPOS_FOR_PROMOTION
    row.total_successes = 2  # < MIN_GLOBAL_SUCCESS_COUNT

    mock_result = MagicMock()
    mock_result.all.return_value = [row]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.ci_fixer.pattern_promoter.get_db", return_value=mock_ctx):
        await _promote_patterns()

    # Nothing should have been added
    mock_session.add.assert_not_called()
