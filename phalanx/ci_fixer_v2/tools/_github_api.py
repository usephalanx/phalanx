"""Shared GitHub REST API seam for v2 tools.

Any tool that calls the GitHub REST API routes through `github_get` (or
`github_post`, etc., as they land). Tests monkeypatch the seam at the
call-site module (e.g. `diagnosis._call_github_api`) rather than patching
here, so each tool's tests stay focused on its own logic.

Why a separate module:
  - Keeps httpx imports off the import path when only unit tests run with
    monkeypatched seams.
  - Centralizes retry / header conventions so we don't re-implement them
    across five tools.
"""

from __future__ import annotations

from typing import Any


async def github_get(
    path: str,
    api_key: str,
    accept: str = "application/vnd.github+json",
    timeout: float = 30.0,
) -> tuple[int, str, Any]:
    """Perform a GET against https://api.github.com.

    Returns (status_code, response_text, parsed_json_or_None).

    The parsed JSON is None when the response isn't JSON (e.g., when
    `accept='application/vnd.github.diff'` returns raw unified-diff text).
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"https://api.github.com{path}", headers=headers)

    parsed: Any = None
    content_type = r.headers.get("content-type", "")
    if "json" in content_type:
        try:
            parsed = r.json()
        except Exception:
            parsed = None

    return r.status_code, r.text, parsed


async def github_post(
    path: str,
    api_key: str,
    json_body: dict[str, Any],
    timeout: float = 30.0,
) -> tuple[int, str, Any]:
    """Perform a POST against https://api.github.com with a JSON body.

    Returns (status_code, response_text, parsed_json_or_None). Matches the
    shape of `github_get` so callers can handle both uniformly.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"https://api.github.com{path}",
            headers=headers,
            json=json_body,
        )

    parsed: Any = None
    content_type = r.headers.get("content-type", "")
    if "json" in content_type:
        try:
            parsed = r.json()
        except Exception:
            parsed = None

    return r.status_code, r.text, parsed
