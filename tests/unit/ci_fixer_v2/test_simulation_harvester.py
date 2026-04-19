"""Unit tests for the harvester — mocks every GitHub call."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2.simulation import harvester
from phalanx.ci_fixer_v2.simulation.fixtures import iter_fixtures


def _repo_payload(license_key: str = "mit") -> dict:
    return {"license": {"key": license_key, "spdx_id": license_key.upper()}}


def _workflow_runs_payload(runs: list[dict]) -> dict:
    return {"workflow_runs": runs}


def _make_run(run_id: int, pr_number: int = 99, sha: str = "abc") -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "pull_requests": [{"number": pr_number}],
    }


def _pr_payload(pr_number: int = 99) -> dict:
    return {
        "number": pr_number,
        "title": "fix: lint",
        "body": "PR body with alice@example.com",
        "state": "open",
        "user": {"login": "alice"},
        "head": {"ref": "fix/lint"},
        "base": {"ref": "main"},
    }


def _jobs_payload(job_id: int = 777, conclusion: str = "failure") -> dict:
    return {"jobs": [{"id": job_id, "name": "Lint", "conclusion": conclusion}]}


def _patch_github(monkeypatch, routes: dict[str, tuple[int, str, object]]):
    """Install a fake `_call_github_get` that dispatches by path substring.

    Each route key is a substring that must appear in the request path;
    the first matching key wins. Values are (status, text, body).
    """
    async def fake(path, _token, accept="application/vnd.github+json"):
        for key, value in routes.items():
            if key in path:
                return value
        raise AssertionError(f"unmapped harvest route: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)


async def test_harvest_writes_one_fixture_happy_path(tmp_path, monkeypatch):
    routes = {
        # License lookup — /repos/{owner}/{repo}
        "/repos/astral-sh/ruff?": (200, "", _repo_payload("mit")),
        # That path is used by both repo lookup AND other calls; keyed by suffix
        "/repos/astral-sh/ruff\x00": (200, "", _repo_payload("mit")),
        # Dispatch order below covers the actual call order in _resolve_repo_license
        # vs. the runs endpoint. We use distinct suffixes where possible.
        "/actions/runs?": (200, "", _workflow_runs_payload([_make_run(1, 99)])),
        "/pulls/99": (200, "", _pr_payload(99)),
        "/actions/runs/1/jobs": (200, "", _jobs_payload(777)),
        "/actions/jobs/777/logs": (
            200,
            "ghp_" + "a" * 36 + " leaked\nFAILED tests/x.py::t\n",
            None,
        ),
    }
    # The repo-license lookup exact path is /repos/astral-sh/ruff (no '?').
    # Install that route separately.
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/astral-sh/ruff":
            return (200, "", _repo_payload("mit"))
        if path.startswith("/repos/astral-sh/ruff/actions/runs?"):
            return (200, "", _workflow_runs_payload([_make_run(1, 99)]))
        if path == "/repos/astral-sh/ruff/pulls/99":
            if accept == "application/vnd.github.diff":
                return (
                    200,
                    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
                    None,
                )
            return (200, "", _pr_payload(99))
        if path == "/repos/astral-sh/ruff/actions/runs/1/jobs":
            return (200, "", _jobs_payload(777))
        if path == "/repos/astral-sh/ruff/actions/jobs/777/logs":
            return (
                200,
                "ghp_" + "a" * 36 + " leaked\nFAILED tests/x.py::t\n",
                None,
            )
        raise AssertionError(f"unexpected harvest path: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)

    stats = await harvester.harvest_from_repo(
        repo_full_name="astral-sh/ruff",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="lint",
        days=7,
        limit=5,
    )
    assert stats.fixtures_written == 1
    assert stats.skipped_incompatible_license == 0

    # Loaded fixture should have redacted log + canonical meta.
    fixtures = list(iter_fixtures(tmp_path, language="python", failure_class="lint"))
    assert len(fixtures) == 1
    fx = fixtures[0]
    assert fx.meta.origin_repo == "astral-sh/ruff"
    assert fx.meta.license == "mit"
    assert fx.meta.origin_pr_number == 99
    assert "ghp_" not in fx.raw_log
    assert "<REDACTED:SECRET>" in fx.raw_log
    # PR body email redacted.
    assert "alice@example.com" not in (fx.pr_context or {}).get("body", "")


async def test_harvest_skips_gpl_licensed_repo(tmp_path, monkeypatch):
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/foo/bar":
            return (200, "", _repo_payload("gpl-3.0"))
        raise AssertionError("should short-circuit before further calls")

    monkeypatch.setattr(harvester, "_call_github_get", fake)

    stats = await harvester.harvest_from_repo(
        repo_full_name="foo/bar",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="lint",
    )
    assert stats.fixtures_written == 0
    assert stats.skipped_incompatible_license == 1


async def test_harvest_skips_run_with_no_attached_pr(tmp_path, monkeypatch):
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/x/y":
            return (200, "", _repo_payload("mit"))
        if path.startswith("/repos/x/y/actions/runs?"):
            # Run has empty pull_requests.
            return (
                200,
                "",
                _workflow_runs_payload([{"id": 1, "head_sha": "s", "pull_requests": []}]),
            )
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)

    stats = await harvester.harvest_from_repo(
        repo_full_name="x/y",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="test_fail",
        limit=3,
    )
    assert stats.fixtures_written == 0
    assert stats.skipped_no_pr == 1


async def test_harvest_logs_fetch_failure_counts_skipped_no_log(tmp_path, monkeypatch):
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/x/y":
            return (200, "", _repo_payload("mit"))
        if path.startswith("/repos/x/y/actions/runs?"):
            return (200, "", _workflow_runs_payload([_make_run(5, 11)]))
        if path == "/repos/x/y/pulls/11":
            return (
                200,
                "",
                _pr_payload(11) if accept != "application/vnd.github.diff" else None,
            ) if accept != "application/vnd.github.diff" else (
                200,
                "diff...\n",
                None,
            )
        if path == "/repos/x/y/actions/runs/5/jobs":
            return (200, "", _jobs_payload(conclusion="failure"))
        if path.endswith("/logs"):
            # Simulate log fetch failure.
            return (404, "", None)
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)

    stats = await harvester.harvest_from_repo(
        repo_full_name="x/y",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="lint",
        limit=2,
    )
    assert stats.skipped_no_log == 1
    assert stats.fixtures_written == 0


async def test_harvest_respects_limit(tmp_path, monkeypatch):
    # Feed 5 runs but ask for limit=2.
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/x/y":
            return (200, "", _repo_payload("mit"))
        if path.startswith("/repos/x/y/actions/runs?"):
            return (
                200,
                "",
                _workflow_runs_payload(
                    [_make_run(i, 100 + i, f"sha{i}") for i in range(1, 6)]
                ),
            )
        if path.startswith("/repos/x/y/pulls/"):
            if accept == "application/vnd.github.diff":
                return (200, "diff\n", None)
            return (200, "", _pr_payload(99))
        if "/actions/runs/" in path and path.endswith("/jobs"):
            return (200, "", _jobs_payload())
        if path.endswith("/logs"):
            return (200, "clean log\n", None)
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)
    # Short-circuit the rate-limit sleep so tests stay fast.
    import asyncio

    async def noop(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", noop)

    stats = await harvester.harvest_from_repo(
        repo_full_name="x/y",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="lint",
        limit=2,
    )
    assert stats.fixtures_written == 2


async def test_harvest_license_lookup_failure_returns_none_license(tmp_path, monkeypatch):
    async def fake(path, _token, accept="application/vnd.github+json"):
        if path == "/repos/x/y":
            return (404, "", None)
        if path.startswith("/repos/x/y/actions/runs?"):
            return (200, "", _workflow_runs_payload([]))
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(harvester, "_call_github_get", fake)

    stats = await harvester.harvest_from_repo(
        repo_full_name="x/y",
        github_token="tok",
        corpus_root=tmp_path,
        language="python",
        failure_class="lint",
    )
    # No license blocks license-gate check; harvester proceeds; 0 fixtures
    # because workflow_runs is empty.
    assert stats.skipped_incompatible_license == 0
    assert stats.fixtures_written == 0
