"""Catches the celery-include bug class (humanize canary bug #1).

When a new v3 agent is added, two things must be in sync:
  1. phalanx/queue/celery_app.py `include=[...]` must list the module.
  2. phalanx/runtime/task_router.py `_ROLE_TO_QUEUE` and `_ROLE_TO_TASK`
     must register the role.

Both are easy to forget. Without (1), worker logs show:
  Received unregistered task of type 'phalanx.agents.cifix_X.execute_task'
  KeyError: ...
…and the Celery message goes to a dead-letter loop.

Without (2), TaskRouter.dispatch() raises UnroutableTaskError on any
attempt to enqueue that role. We hit the first form during canary #1,
costing one full deploy cycle to discover. This test makes both
self-checking on every commit.
"""

from __future__ import annotations

import pytest


_V3_AGENT_MODULES = (
    "phalanx.agents.cifix_commander",
    "phalanx.agents.cifix_techlead",
    "phalanx.agents.cifix_engineer",
    "phalanx.agents.cifix_sre",
)

_V3_CELERY_TASK_NAMES = (
    "phalanx.agents.cifix_commander.execute_run",
    "phalanx.agents.cifix_techlead.execute_task",
    "phalanx.agents.cifix_engineer.execute_task",
    "phalanx.agents.cifix_sre.execute_task",
)

_V3_AGENT_ROLES = ("cifix_commander", "cifix_techlead", "cifix_engineer", "cifix_sre")


@pytest.mark.parametrize("module_path", _V3_AGENT_MODULES)
def test_v3_agent_module_in_celery_include(module_path: str):
    """Every v3 agent module MUST appear in celery_app.Celery(include=[...]).

    This is the bug that caused canary #1 to fail with
    'Received unregistered task'. Worker boot-time imports are the only
    way @celery_app.task decorators register their tasks; queue
    subscription alone is not enough.
    """
    from phalanx.queue.celery_app import celery_app

    include = celery_app.conf.include or celery_app.main and []
    # Celery stores `include` either on .conf.include or via .main; both
    # paths resolve to the same list at runtime. Use the direct import
    # the worker uses.
    import phalanx.queue.celery_app as cm

    raw_include = cm.celery_app.conf.get("include") or []
    assert module_path in raw_include, (
        f"{module_path!r} missing from celery_app include list. "
        f"Currently: {sorted(raw_include)}. "
        "Add it next to the existing v3 entries; otherwise the worker "
        "will reject tasks routed to this agent's queue."
    )


def test_v3_celery_tasks_registered_after_loader_import():
    """celery_app.tasks is populated lazily; the worker triggers
    loader.import_default_modules() at boot. We trigger it here too,
    then assert all 4 v3 tasks are registered.
    """
    from phalanx.queue.celery_app import celery_app

    celery_app.loader.import_default_modules()
    registered = set(celery_app.tasks.keys())
    missing = [t for t in _V3_CELERY_TASK_NAMES if t not in registered]
    assert not missing, f"v3 tasks not registered: {missing}"


@pytest.mark.parametrize("role", _V3_AGENT_ROLES)
def test_v3_role_in_task_router(role: str):
    """TaskRouter.dispatch() looks up agent_role → queue + task name.
    Both maps must include the v3 role.
    """
    from phalanx.runtime.task_router import _ROLE_TO_QUEUE, _ROLE_TO_TASK

    assert role in _ROLE_TO_QUEUE, (
        f"{role!r} not in TaskRouter._ROLE_TO_QUEUE — "
        "TaskRouter.dispatch() will raise UnroutableTaskError"
    )
    assert role in _ROLE_TO_TASK, (
        f"{role!r} not in TaskRouter._ROLE_TO_TASK — "
        "TaskRouter.dispatch() can route the queue but won't know "
        "the celery task name"
    )


def test_v3_queues_match_router_mappings():
    """The queue NAME each role routes to must match the celery_app
    task_queues registration. Mismatches produce silently-dropped tasks
    (Celery routes to a queue no worker is listening on).
    """
    from phalanx.queue.celery_app import celery_app
    from phalanx.runtime.task_router import _ROLE_TO_QUEUE

    declared_queues = set((celery_app.conf.task_queues or {}).keys())
    for role in _V3_AGENT_ROLES:
        target_queue = _ROLE_TO_QUEUE[role]
        assert target_queue in declared_queues, (
            f"role={role!r} routes to queue={target_queue!r} but that "
            f"queue is not declared in celery_app.task_queues. "
            f"Declared: {sorted(declared_queues)}"
        )


def test_v3_persist_task_completion_helper_imports():
    """Bug #3 from the canary: leaf agents returned AgentResult but
    never wrote Task.status=COMPLETED. The fix lives in
    ci_fixer_v3/task_lifecycle.persist_task_completion. Make sure
    the helper is importable and callable shape is what the agents
    expect.
    """
    from phalanx.ci_fixer_v3.task_lifecycle import persist_task_completion

    import inspect

    sig = inspect.signature(persist_task_completion)
    params = list(sig.parameters)
    assert params == ["task_id", "result"], (
        f"persist_task_completion signature changed: {sig} — "
        "check every cifix_*.py execute_task wrapper still calls it correctly"
    )
