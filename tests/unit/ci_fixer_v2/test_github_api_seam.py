"""Focused tests for the shared GitHub API seam (phalanx/ci_fixer_v2/tools/_github_api.py).

All CI-Fixer-v2 tools that reach GitHub route through `github_get`. The
higher-level tool tests mock `_call_github_api` to stay focused on tool
logic; these tests exercise the real function with `httpx.MockTransport`
so the boundary code (headers, URL assembly, content-type branching)
is actually covered.
"""

from __future__ import annotations

import httpx
import pytest

from phalanx.ci_fixer_v2.tools._github_api import github_get, github_post


@pytest.fixture(autouse=True)
def _install_mock_transport(monkeypatch):
    """Redirect every AsyncClient built inside `github_get` through the
    mock transport the test installs under `_TRANSPORT_REGISTRY`.
    """
    registry: dict[str, httpx.MockTransport] = {}
    original_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        transport = registry.get("current")
        if transport is None:
            raise AssertionError("Test did not install a mock transport")
        return original_client(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield registry


def _install(registry: dict[str, httpx.MockTransport], handler):
    registry["current"] = httpx.MockTransport(handler)


async def test_github_get_builds_correct_request(_install_mock_transport):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"number": 1, "title": "ok"},
            headers={"content-type": "application/json"},
        )

    _install(_install_mock_transport, handler)

    status, text, body = await github_get("/repos/a/b/pulls/1", "tok-xyz")

    assert status == 200
    assert body == {"number": 1, "title": "ok"}
    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.github.com/repos/a/b/pulls/1"
    assert seen["headers"]["authorization"] == "Bearer tok-xyz"
    assert seen["headers"]["accept"] == "application/vnd.github+json"
    assert seen["headers"]["x-github-api-version"] == "2022-11-28"


async def test_github_get_respects_custom_accept_for_raw_diff(_install_mock_transport):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(
            200,
            text="diff --git a/x b/x\n",
            headers={"content-type": "text/plain"},
        )

    _install(_install_mock_transport, handler)

    status, text, body = await github_get(
        "/repos/a/b/pulls/1",
        "tok",
        accept="application/vnd.github.diff",
    )
    assert status == 200
    assert text.startswith("diff --git")
    assert body is None  # non-JSON content-type → parsed body is None
    assert seen["accept"] == "application/vnd.github.diff"


async def test_github_get_returns_none_body_when_json_parse_fails(_install_mock_transport):
    def handler(_request: httpx.Request) -> httpx.Response:
        # Claims JSON but sends invalid bytes; parse should return None.
        return httpx.Response(
            200,
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )

    _install(_install_mock_transport, handler)

    status, _text, body = await github_get("/x", "tok")
    assert status == 200
    assert body is None


async def test_github_get_propagates_non_200_status(_install_mock_transport):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    _install(_install_mock_transport, handler)

    status, _text, body = await github_get("/missing", "tok")
    assert status == 404
    assert body == {"message": "Not Found"}


async def test_github_post_sends_json_body_and_correct_headers(_install_mock_transport):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        import json as _j

        seen["payload"] = _j.loads(request.content.decode("utf-8"))
        return httpx.Response(
            201,
            json={"id": 42, "ok": True},
            headers={"content-type": "application/json"},
        )

    _install(_install_mock_transport, handler)

    status, _text, body = await github_post(
        "/repos/a/b/issues/1/comments",
        "tok-xyz",
        {"body": "hi"},
    )
    assert status == 201
    assert body == {"id": 42, "ok": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.github.com/repos/a/b/issues/1/comments"
    assert seen["headers"]["authorization"] == "Bearer tok-xyz"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["payload"] == {"body": "hi"}


async def test_github_post_returns_none_body_on_non_json_content_type(_install_mock_transport):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            text="plain text",
            headers={"content-type": "text/plain"},
        )

    _install(_install_mock_transport, handler)

    status, text, body = await github_post("/x", "tok", {"a": 1})
    assert status == 201
    assert text == "plain text"
    assert body is None


async def test_github_post_handles_json_parse_failure(_install_mock_transport):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            content=b"{bad json",
            headers={"content-type": "application/json"},
        )

    _install(_install_mock_transport, handler)

    status, _text, body = await github_post("/x", "tok", {})
    assert status == 201
    assert body is None
