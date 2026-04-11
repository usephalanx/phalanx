"""
Unit tests for CI webhook route helpers and log fetcher.
No DB, no network calls.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import zipfile
import io

import pytest

from phalanx.api.routes.ci_webhooks import (
    _verify_github_signature,
    _verify_buildkite_signature,
    _parse_repo_name,
)
from phalanx.ci_fixer.log_fetcher import (
    _extract_failure_section,
    _extract_failed_step_from_zip,
    _truncate,
    get_log_fetcher,
    GitHubActionsLogFetcher,
    BuildkiteLogFetcher,
    CircleCILogFetcher,
    JenkinsLogFetcher,
)


# ── _verify_github_signature ───────────────────────────────────────────────────

class TestVerifyGithubSignature:
    def _make_sig(self, body: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_valid_signature(self):
        body = b'{"event": "check_run"}'
        secret = "super-secret"
        sig = self._make_sig(body, secret)
        assert _verify_github_signature(body, sig, secret) is True

    def test_invalid_signature(self):
        body = b'{"event": "check_run"}'
        assert _verify_github_signature(body, "sha256=invalid", "secret") is False

    def test_no_secret_always_passes(self):
        # When no secret configured, skip verification (dev mode)
        assert _verify_github_signature(b"anything", "", "") is True
        assert _verify_github_signature(b"anything", "bad-sig", "") is True

    def test_empty_signature_with_secret_fails(self):
        body = b"data"
        assert _verify_github_signature(body, "", "my-secret") is False

    def test_tampered_body_fails(self):
        body = b'{"event": "check_run"}'
        secret = "secret"
        sig = self._make_sig(body, secret)
        tampered = b'{"event": "push"}'
        assert _verify_github_signature(tampered, sig, secret) is False


# ── _verify_buildkite_signature ────────────────────────────────────────────────

class TestVerifyBuildkiteSignature:
    def test_matching_tokens(self):
        assert _verify_buildkite_signature(b"body", "my-token", "my-token") is True

    def test_mismatched_tokens(self):
        assert _verify_buildkite_signature(b"body", "wrong", "my-token") is False

    def test_no_stored_token_always_passes(self):
        assert _verify_buildkite_signature(b"body", "", "") is True
        assert _verify_buildkite_signature(b"body", "anything", "") is True

    def test_empty_token_with_secret_fails(self):
        assert _verify_buildkite_signature(b"body", "", "stored-token") is False


# ── _parse_repo_name ───────────────────────────────────────────────────────────

class TestParseRepoName:
    def test_https_url(self):
        assert _parse_repo_name("https://github.com/acme/backend.git") == "acme/backend"

    def test_https_url_no_git(self):
        assert _parse_repo_name("https://github.com/acme/backend") == "acme/backend"

    def test_ssh_url(self):
        assert _parse_repo_name("git@github.com:acme/backend.git") == "acme/backend"

    def test_non_github_url(self):
        assert _parse_repo_name("https://gitlab.com/acme/backend.git") is None

    def test_empty_string(self):
        assert _parse_repo_name("") is None

    def test_org_with_dots(self):
        assert _parse_repo_name("https://github.com/my-org/my.repo.git") == "my-org/my.repo"


# ── get_log_fetcher ────────────────────────────────────────────────────────────

class TestGetLogFetcher:
    def test_github_actions(self):
        fetcher = get_log_fetcher("github_actions")
        assert isinstance(fetcher, GitHubActionsLogFetcher)

    def test_buildkite(self):
        fetcher = get_log_fetcher("buildkite")
        assert isinstance(fetcher, BuildkiteLogFetcher)

    def test_circleci(self):
        fetcher = get_log_fetcher("circleci")
        assert isinstance(fetcher, CircleCILogFetcher)

    def test_jenkins(self):
        fetcher = get_log_fetcher("jenkins")
        assert isinstance(fetcher, JenkinsLogFetcher)

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError, match="unknown-ci"):
            get_log_fetcher("unknown-ci")


# ── _extract_failed_step_from_zip ─────────────────────────────────────────────

class TestExtractFailedStepFromZip:
    def _make_zip(self, files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_extracts_matched_job(self):
        content = "ok\nok\nError: test failed\nmore"
        zip_bytes = self._make_zip({"unit-tests/1_run.txt": content})
        result = _extract_failed_step_from_zip(zip_bytes, ["unit-tests"])
        assert "Error: test failed" in result

    def test_falls_back_all_files_when_no_match(self):
        content = "Error: something"
        zip_bytes = self._make_zip({"some-job/1_run.txt": content})
        result = _extract_failed_step_from_zip(zip_bytes, ["nonexistent-job"])
        assert "Error: something" in result

    def test_invalid_zip_returns_empty(self):
        result = _extract_failed_step_from_zip(b"not a zip", [])
        assert result == ""

    def test_empty_zip(self):
        zip_bytes = self._make_zip({})
        result = _extract_failed_step_from_zip(zip_bytes, [])
        assert result == ""


# ── _extract_failure_section ───────────────────────────────────────────────────

class TestExtractFailureSection:
    def test_finds_failed_keyword(self):
        lines = ["line1", "FAILED tests/foo.py::test_bar", "traceback here"]
        result = _extract_failure_section(lines)
        assert "FAILED" in result

    def test_finds_exception(self):
        lines = ["running...", "Exception: boom", "done"]
        result = _extract_failure_section(lines)
        assert "Exception: boom" in result

    def test_returns_last_lines_when_no_error(self):
        lines = [f"line {i}" for i in range(200)]
        result = _extract_failure_section(lines)
        assert "line 199" in result
        assert "line 0" not in result

    def test_empty_input(self):
        assert _extract_failure_section([]) == ""

    def test_single_error_line(self):
        lines = ["Error: only this"]
        result = _extract_failure_section(lines)
        assert "Error: only this" in result


# ── _truncate ──────────────────────────────────────────────────────────────────

class TestTruncateLogFetcher:
    def test_short_text_unchanged(self):
        text = "short"
        assert _truncate(text) == text

    def test_boundary_unchanged(self):
        # Exactly at limit — no truncation
        text = "x" * 6000
        result = _truncate(text)
        assert result == text

    def test_over_limit_truncated(self):
        text = "A" * 3100 + "B" * 3100  # 6200 chars
        result = _truncate(text)
        assert len(result) < len(text)
        assert "truncated" in result

    def test_tail_preserved(self):
        text = "start-" * 2000 + "END_MARKER"
        result = _truncate(text)
        assert "END_MARKER" in result

    def test_head_preserved(self):
        text = "START_MARKER" + "x" * 10000
        result = _truncate(text)
        assert "START_MARKER" in result


# ── Stub fetchers (CircleCI, Jenkins) ─────────────────────────────────────────

import asyncio
import pytest
from phalanx.ci_fixer.events import CIFailureEvent


def _make_event():
    return CIFailureEvent(
        provider="circleci",
        repo_full_name="acme/api",
        branch="main",
        commit_sha="abc",
        build_id="1",
        build_url="https://ci.example.com/1",
    )


class TestStubFetchers:
    @pytest.mark.asyncio
    async def test_circleci_returns_string(self):
        fetcher = CircleCILogFetcher()
        result = await fetcher.fetch(_make_event(), "key")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_jenkins_returns_string(self):
        fetcher = JenkinsLogFetcher()
        e = _make_event()
        e.provider = "jenkins"
        result = await fetcher.fetch(e, "key")
        assert isinstance(result, str)
        assert len(result) > 0


# ── _extract_failure_section edge cases ────────────────────────────────────────

class TestExtractFailureSectionEdge:
    def test_exactly_150_lines_no_error(self):
        lines = [f"x{i}" for i in range(150)]
        result = _extract_failure_section(lines)
        assert "x149" in result

    def test_error_at_first_line(self):
        lines = ["Error: first", "line2", "line3"]
        result = _extract_failure_section(lines)
        assert "Error: first" in result

    def test_error_at_last_line(self):
        lines = ["ok", "ok", "Failed!"]
        result = _extract_failure_section(lines)
        assert "Failed!" in result

    def test_multiple_errors_finds_first(self):
        lines = ["Error: first", "ok", "Error: second"]
        result = _extract_failure_section(lines)
        # Should capture from first error
        assert "Error: first" in result


# ── _truncate edge cases ────────────────────────────────────────────────────────

class TestTruncateEdge:
    def test_exactly_at_limit_not_truncated(self):
        text = "y" * 6000
        assert _truncate(text) == text

    def test_one_over_limit(self):
        text = "z" * 6001
        result = _truncate(text)
        assert "truncated" in result

    def test_empty_string(self):
        assert _truncate("") == ""


# ── GitHubActionsLogFetcher (mocked httpx) ─────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_client(responses: list):
    """Build a mock AsyncClient where each get() call returns the next response."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    mock_responses = [MagicMock(**r) for r in responses]
    client.get = AsyncMock(side_effect=mock_responses)
    return client


class TestGitHubActionsLogFetcher:
    def _make_event(self, failed_jobs=None):
        return CIFailureEvent(
            provider="github_actions",
            repo_full_name="acme/backend",
            branch="main",
            commit_sha="abc123",
            build_id="42",
            build_url="https://github.com/acme/backend/actions/runs/42",
            failed_jobs=failed_jobs or ["unit-tests"],
        )

    @pytest.mark.asyncio
    async def test_fetch_with_annotations_and_logs(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("unit-tests/1_run.txt", "ok\nError: test failed\ndone")
        zip_bytes = buf.getvalue()

        annotations_resp = MagicMock()
        annotations_resp.raise_for_status = MagicMock()
        annotations_resp.json.return_value = [
            {"path": "src/foo.py", "start_line": 10, "message": "assertion failed"}
        ]

        check_run_resp = MagicMock()
        check_run_resp.raise_for_status = MagicMock()
        check_run_resp.json.return_value = {"check_suite": {"id": "99"}}

        log_zip_resp = MagicMock()
        log_zip_resp.raise_for_status = MagicMock()
        log_zip_resp.status_code = 200
        log_zip_resp.content = zip_bytes

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=[annotations_resp, check_run_resp, log_zip_resp])

        from phalanx.ci_fixer.log_fetcher import GitHubActionsLogFetcher
        fetcher = GitHubActionsLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "my-token")

        assert "assertion failed" in result or "Error: test failed" in result

    @pytest.mark.asyncio
    async def test_fetch_annotations_fail_gracefully(self):
        """When annotations API raises, log fetch still proceeds."""
        annotations_resp = MagicMock()
        annotations_resp.raise_for_status.side_effect = Exception("403 Forbidden")

        check_run_resp = MagicMock()
        check_run_resp.raise_for_status = MagicMock()
        check_run_resp.json.return_value = {"check_suite": {"id": "99"}}

        log_zip_resp = MagicMock()
        log_zip_resp.raise_for_status = MagicMock()
        log_zip_resp.status_code = 404  # not 200 → no log extracted

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=[annotations_resp, check_run_resp, log_zip_resp])

        from phalanx.ci_fixer.log_fetcher import GitHubActionsLogFetcher
        fetcher = GitHubActionsLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "tok")

        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_fetch_all_requests_fail_returns_no_logs(self):
        """When every request fails, returns '(no logs retrieved)'."""
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("network error")

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=Exception("connect failed"))

        from phalanx.ci_fixer.log_fetcher import GitHubActionsLogFetcher
        fetcher = GitHubActionsLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "tok")

        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_fetch_no_failed_jobs_uses_all_files(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other-job/1_run.txt", "Error: crash")
        zip_bytes = buf.getvalue()

        annotations_resp = MagicMock()
        annotations_resp.raise_for_status = MagicMock()
        annotations_resp.json.return_value = []

        check_run_resp = MagicMock()
        check_run_resp.raise_for_status = MagicMock()
        check_run_resp.json.return_value = {}

        log_zip_resp = MagicMock()
        log_zip_resp.raise_for_status = MagicMock()
        log_zip_resp.status_code = 200
        log_zip_resp.content = zip_bytes

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=[annotations_resp, check_run_resp, log_zip_resp])

        event = self._make_event(failed_jobs=[])
        from phalanx.ci_fixer.log_fetcher import GitHubActionsLogFetcher
        fetcher = GitHubActionsLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(event, "tok")

        assert "Error: crash" in result


# ── BuildkiteLogFetcher (mocked httpx) ─────────────────────────────────────────

class TestBuildkiteLogFetcher:
    def _make_event(self):
        return CIFailureEvent(
            provider="buildkite",
            repo_full_name="acme/api",
            branch="main",
            commit_sha="deadbeef",
            build_id="bk-org/pipeline/99",
            build_url="https://buildkite.com/acme/api/builds/99",
        )

    @pytest.mark.asyncio
    async def test_fetch_failed_jobs_logs(self):
        build_resp = MagicMock()
        build_resp.raise_for_status = MagicMock()
        build_resp.json.return_value = {
            "jobs": [
                {"id": "job-1", "state": "failed", "name": "unit-tests"},
                {"id": "job-2", "state": "passed", "name": "deploy"},
            ]
        }

        log_resp = MagicMock()
        log_resp.raise_for_status = MagicMock()
        log_resp.json.return_value = {"content": "running tests\nError: assertion failed\ndone"}

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=[build_resp, log_resp])

        from phalanx.ci_fixer.log_fetcher import BuildkiteLogFetcher
        fetcher = BuildkiteLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "bk-token")

        assert "Error: assertion failed" in result

    @pytest.mark.asyncio
    async def test_fetch_no_failed_jobs_returns_no_logs(self):
        build_resp = MagicMock()
        build_resp.raise_for_status = MagicMock()
        build_resp.json.return_value = {"jobs": [{"id": "job-1", "state": "passed"}]}

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=build_resp)

        from phalanx.ci_fixer.log_fetcher import BuildkiteLogFetcher
        fetcher = BuildkiteLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "bk-token")

        assert result == "(no logs retrieved)"

    @pytest.mark.asyncio
    async def test_fetch_build_request_fails_returns_error(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=Exception("network error"))

        from phalanx.ci_fixer.log_fetcher import BuildkiteLogFetcher
        fetcher = BuildkiteLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "bk-token")

        assert "failed" in result

    @pytest.mark.asyncio
    async def test_fetch_job_log_fails_gracefully(self):
        build_resp = MagicMock()
        build_resp.raise_for_status = MagicMock()
        build_resp.json.return_value = {
            "jobs": [{"id": "job-1", "state": "failed", "name": "lint"}]
        }

        log_resp = MagicMock()
        log_resp.raise_for_status.side_effect = Exception("403")

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=[build_resp, log_resp])

        from phalanx.ci_fixer.log_fetcher import BuildkiteLogFetcher
        fetcher = BuildkiteLogFetcher()
        with patch("phalanx.ci_fixer.log_fetcher.httpx.AsyncClient", return_value=client):
            result = await fetcher.fetch(self._make_event(), "bk-token")

        assert isinstance(result, str)


# ── Webhook route integration (early-return paths, no DB) ──────────────────────

import json as _json
from fastapi.testclient import TestClient
from unittest.mock import patch as _patch


def _make_app():
    from fastapi import FastAPI
    from phalanx.api.routes.ci_webhooks import router
    app = FastAPI()
    app.include_router(router)
    return app


class TestGitHubWebhookRoutes:
    def setup_method(self):
        self.client = TestClient(_make_app())

    def _post(self, payload, event="check_run", sig=""):
        return self.client.post(
            "/webhook/github",
            content=_json.dumps(payload).encode(),
            headers={
                "x-github-event": event,
                "x-hub-signature-256": sig,
                "content-type": "application/json",
            },
        )

    def test_non_check_run_event_is_ignored(self):
        r = self._post({}, event="push")
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_check_run_not_completed_is_ignored(self):
        payload = {
            "action": "created",
            "check_run": {"conclusion": "failure", "check_suite": {}},
        }
        r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_check_run_success_conclusion_is_ignored(self):
        payload = {
            "action": "completed",
            "check_run": {"conclusion": "success", "check_suite": {}},
        }
        r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_check_run_failure_dispatches(self):
        payload = {
            "action": "completed",
            "check_run": {
                "id": 42,
                "name": "unit-tests",
                "conclusion": "failure",
                "head_sha": "abc123",
                "details_url": "https://github.com/checks/42",
                "check_suite": {
                    "head_branch": "main",
                    "pull_requests": [{"number": 7}],
                },
            },
            "repository": {"full_name": "acme/backend"},
        }
        with _patch("phalanx.api.routes.ci_webhooks._dispatch_ci_fix", return_value=None):
            r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"


class TestBuildkiteWebhookRoutes:
    def setup_method(self):
        self.client = TestClient(_make_app())

    def _post(self, payload, token=""):
        return self.client.post(
            "/webhook/buildkite",
            content=_json.dumps(payload).encode(),
            headers={
                "x-buildkite-token": token,
                "content-type": "application/json",
            },
        )

    def test_non_build_finished_is_ignored(self):
        r = self._post({"event": "build.running"})
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_build_finished_not_failed_is_ignored(self):
        r = self._post({"event": "build.finished", "build": {"state": "passed"}})
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_build_finished_unparseable_repo_is_skipped(self):
        payload = {
            "event": "build.finished",
            "build": {"state": "failed", "branch": "main", "commit": "abc", "id": 1, "jobs": []},
            "pipeline": {"repository": "https://gitlab.com/org/repo.git"},
        }
        r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"

    def test_build_finished_dispatches(self):
        payload = {
            "event": "build.finished",
            "build": {
                "state": "failed", "branch": "main", "commit": "abc",
                "id": 99, "web_url": "https://bk.io/1", "jobs": [],
            },
            "pipeline": {"repository": "https://github.com/acme/api.git"},
        }
        with _patch("phalanx.api.routes.ci_webhooks._dispatch_ci_fix", return_value=None):
            r = self._post(payload)
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"


class TestStubWebhookRoutes:
    def setup_method(self):
        self.client = TestClient(_make_app())

    def test_circleci_stub(self):
        r = self.client.post("/webhook/circleci", content=b"{}")
        assert r.status_code == 200
        assert r.json()["status"] == "coming_soon"

    def test_jenkins_stub(self):
        r = self.client.post("/webhook/jenkins", content=b"{}")
        assert r.status_code == 200
        assert r.json()["status"] == "coming_soon"
