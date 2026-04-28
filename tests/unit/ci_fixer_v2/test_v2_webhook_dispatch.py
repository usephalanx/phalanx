"""Unit test for the webhook's v1-vs-v2 dispatch branch.

Spec §14 cutover: when `settings.phalanx_ci_fixer_v2_enabled` is True,
the webhook handler dispatches to `execute_v2_task`; otherwise to v1's
`execute_task`. Both share the same `ci_fixer` queue (N1-scoped worker).
"""

from __future__ import annotations

import pytest


def _make_event():
    from phalanx.ci_fixer.events import CIFailureEvent

    return CIFailureEvent(
        provider="github_actions",
        repo_full_name="acme/widget",
        branch="fail/lint",
        commit_sha="a" * 40,
        build_id="123",
        build_url="https://gh/actions/runs/123",
        failed_jobs=["Lint"],
        pr_number=7,
        pr_author="alice",
        raw_payload={"_log_preview": "E501 line too long"},
    )


def _patch_db_and_integration(monkeypatch, has_integration: bool = True):
    """Stub the DB lookups inside _dispatch_ci_fix so the test never
    touches Postgres."""
    from contextlib import asynccontextmanager

    class _FakeIntegration:
        id = "integ-1"
        allowed_authors = None
        max_attempts = 3
        auto_commit = False
        cifixer_version = "v2"  # v3 routing branch added in ci_webhooks.py

    class _FakeResult:
        def __init__(self, value):
            self._value = value
            self._list_value: list = []

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            return self

        def all(self):
            return self._list_value

    class _FakeSession:
        calls_made: list[str] = []

        async def execute(self, *_a, **_k):
            # Sequence of calls in _dispatch_ci_fix:
            #  1. CIIntegration lookup
            #  2. CIFixRun build_id guard
            #  3. CIFixRun commit-window dedup
            #  4. CIFixRun attempt count
            idx = len(self.calls_made)
            self.calls_made.append("call")
            if idx == 0:
                return _FakeResult(_FakeIntegration() if has_integration else None)
            if idx in (1, 2):
                return _FakeResult(None)
            # attempts list
            r = _FakeResult(None)
            r._list_value = []
            return r

        def add(self, obj):
            # CIFixRun row being added — give it a stable id for the test.
            obj.id = "new-run-id"

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

    @asynccontextmanager
    async def fake_get_db():
        yield _FakeSession()

    import phalanx.api.routes.ci_webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod, "get_db", fake_get_db)


@pytest.fixture
def _patch_v1_and_v2_tasks(monkeypatch):
    """Replace both task apply_async with recording fakes."""
    called = {"v1": 0, "v2": 0, "last_args": None, "last_queue": None}

    class _FakeTask:
        def __init__(self, label):
            self.label = label

        def apply_async(self, args=None, queue=None, **_kw):
            called[self.label] += 1
            called["last_args"] = args
            called["last_queue"] = queue

    # Patch the lazy imports inside _dispatch_ci_fix. The function does
    # `from phalanx.agents.ci_fixer import execute_task` at call time,
    # so we patch those module attributes.
    import phalanx.agents.ci_fixer as v1_mod
    import phalanx.agents.ci_fixer_v2_task as v2_mod

    monkeypatch.setattr(v1_mod, "execute_task", _FakeTask("v1"))
    monkeypatch.setattr(v2_mod, "execute_v2_task", _FakeTask("v2"))
    return called


async def test_webhook_dispatches_v1_when_flag_off(monkeypatch, _patch_v1_and_v2_tasks):
    _patch_db_and_integration(monkeypatch)

    # Flag off — settings attribute is resolved via module-level `settings`.
    import phalanx.api.routes.ci_webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod.settings, "phalanx_ci_fixer_v2_enabled", False)

    from phalanx.api.routes.ci_webhooks import _dispatch_ci_fix

    result = await _dispatch_ci_fix(_make_event())

    assert result is not None
    assert _patch_v1_and_v2_tasks["v1"] == 1
    assert _patch_v1_and_v2_tasks["v2"] == 0
    assert _patch_v1_and_v2_tasks["last_args"] == ["new-run-id"]
    assert _patch_v1_and_v2_tasks["last_queue"] == "ci_fixer"


async def test_webhook_dispatches_v2_when_flag_on(monkeypatch, _patch_v1_and_v2_tasks):
    _patch_db_and_integration(monkeypatch)

    import phalanx.api.routes.ci_webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod.settings, "phalanx_ci_fixer_v2_enabled", True)

    from phalanx.api.routes.ci_webhooks import _dispatch_ci_fix

    result = await _dispatch_ci_fix(_make_event())

    assert result is not None
    assert _patch_v1_and_v2_tasks["v2"] == 1
    assert _patch_v1_and_v2_tasks["v1"] == 0
    assert _patch_v1_and_v2_tasks["last_queue"] == "ci_fixer"


async def test_webhook_returns_none_when_no_integration_even_with_v2_flag(
    monkeypatch, _patch_v1_and_v2_tasks
):
    # The flag doesn't change the "no integration → no dispatch" behavior.
    _patch_db_and_integration(monkeypatch, has_integration=False)

    import phalanx.api.routes.ci_webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod.settings, "phalanx_ci_fixer_v2_enabled", True)

    from phalanx.api.routes.ci_webhooks import _dispatch_ci_fix

    result = await _dispatch_ci_fix(_make_event())

    assert result is None
    assert _patch_v1_and_v2_tasks["v1"] == 0
    assert _patch_v1_and_v2_tasks["v2"] == 0
