"""The HTTP seam: attribution must survive all the way to what FetchSandbox reads.

Task-level tests prove the metadata is computed. These prove it isn't dropped by
the route's response model — the failure mode that would make the whole seam
useless while every unit test still passed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from phalanx.api.routes import find_bugs as find_bugs_route
from phalanx.api.routes import fix_bug as fix_bug_route

TOKEN = "test-probe-token"

WORKER_FIND_RESULT = {
    "available": True,
    "bugs": "1. src/webhooks.py:42 — duplicate fulfilment on repeated event_id",
    "account": "max1",
    "error": None,
    "grounding": {
        "spec": "paddle",
        "grounded": True,
        "grounding_fields": ["prompt"],
        "prompt_sha256": "abc123def4567890",
        "prompt_chars": 1840,
    },
}

WORKER_FIX_RESULT = {
    "available": True,
    "diff": "--- a/src/webhooks.py\n+++ b/src/webhooks.py\n",
    "summary": "Added event_id dedup before fulfilment.",
    "account": "max1",
    "error": None,
    "grounding": {
        "spec": "paddle",
        "grounded": True,
        "grounding_fields": ["fix_pattern"],
        "prompt_sha256": "0f0f0f0f0f0f0f0f",
        "prompt_chars": 2100,
    },
}


class _FakeAsyncResult:
    """Stands in for the Celery handle; `.get` is called via asyncio.to_thread."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.id = "fake-task-id"

    def get(self, timeout: int | None = None) -> dict:  # noqa: ARG002
        return self._payload


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("PHALANX_PROBE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(find_bugs_route.router, prefix="/v1")
    app.include_router(fix_bug_route.router, prefix="/v1")
    return TestClient(app)


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Capture the kwargs the routes enqueue, so we can assert `spec` is forwarded."""
    seen: dict = {}

    def _fake_find(kwargs=None, queue=None, **_):
        seen["find"] = kwargs
        return _FakeAsyncResult(WORKER_FIND_RESULT)

    def _fake_fix(kwargs=None, queue=None, **_):
        seen["fix"] = kwargs
        return _FakeAsyncResult(WORKER_FIX_RESULT)

    monkeypatch.setattr(find_bugs_route.find_bugs_task, "apply_async", _fake_find)
    monkeypatch.setattr(fix_bug_route.fix_bug_task, "apply_async", _fake_fix)
    return seen


class TestFindBugsRoute:
    def test_spec_is_forwarded_to_the_worker(self, client, captured):
        r = client.post(
            "/v1/find_bugs",
            headers={"x-probe-token": TOKEN},
            json={"git_url": "https://github.com/acme/app", "spec": "paddle"},
        )
        assert r.status_code == 200
        assert captured["find"]["spec"] == "paddle"

    def test_grounding_reaches_the_caller(self, client, captured):
        r = client.post(
            "/v1/find_bugs",
            headers={"x-probe-token": TOKEN},
            json={"git_url": "https://github.com/acme/app", "spec": "paddle"},
        )
        body = r.json()
        assert body["grounding"]["grounded"] is True
        assert body["grounding"]["spec"] == "paddle"
        assert body["grounding"]["prompt_sha256"] == "abc123def4567890"

    def test_request_without_spec_still_works(self, client, captured):
        """Backward compatibility: an older FetchSandbox that sends no `spec`
        must keep working unchanged."""
        r = client.post(
            "/v1/find_bugs",
            headers={"x-probe-token": TOKEN},
            json={"git_url": "https://github.com/acme/app"},
        )
        assert r.status_code == 200
        assert captured["find"]["spec"] is None

    def test_auth_still_required(self, client, captured):
        r = client.post("/v1/find_bugs", json={"git_url": "https://x/y", "spec": "paddle"})
        assert r.status_code == 401


class TestFixBugRoute:
    def test_spec_forwarded_and_grounding_returned(self, client, captured):
        r = client.post(
            "/v1/fix_bug",
            headers={"x-probe-token": TOKEN},
            json={
                "git_url": "https://github.com/acme/app",
                "bug": "duplicate fulfilment",
                "fix_pattern": "dedup on event_id before side effects",
                "spec": "paddle",
            },
        )
        assert r.status_code == 200
        assert captured["fix"]["spec"] == "paddle"
        assert captured["fix"]["fix_pattern"] == "dedup on event_id before side effects"
        assert r.json()["grounding"]["grounding_fields"] == ["fix_pattern"]

    def test_worker_without_grounding_degrades_to_null(self, client, monkeypatch):
        """A worker running older code returns no `grounding` key. The route must
        return null rather than 500 — the two sides deploy independently."""
        legacy = {k: v for k, v in WORKER_FIX_RESULT.items() if k != "grounding"}
        monkeypatch.setattr(
            fix_bug_route.fix_bug_task, "apply_async",
            lambda kwargs=None, queue=None, **_: _FakeAsyncResult(legacy),
        )
        r = client.post(
            "/v1/fix_bug",
            headers={"x-probe-token": TOKEN},
            json={"git_url": "https://x/y", "bug": "b"},
        )
        assert r.status_code == 200
        assert r.json()["grounding"] is None
        assert r.json()["diff"] == legacy["diff"]
