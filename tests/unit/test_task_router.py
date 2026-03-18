"""
Unit tests for forge/runtime/task_router.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from forge.runtime.task_router import TaskRouter, UnroutableTaskError


@pytest.fixture
def mock_celery():
    app = MagicMock()
    result = MagicMock()
    result.id = "celery-task-id-123"
    app.send_task.return_value = result
    return app


@pytest.fixture
def router(mock_celery):
    return TaskRouter(mock_celery)


class TestQueueMapping:
    def test_commander_maps_to_commander_queue(self, router):
        assert router.queue_for_role("commander") == "commander"

    def test_planner_maps_to_planner_queue(self, router):
        assert router.queue_for_role("planner") == "planner"

    def test_builder_maps_to_builder_queue(self, router):
        assert router.queue_for_role("builder") == "builder"

    def test_reviewer_maps_to_reviewer_queue(self, router):
        assert router.queue_for_role("reviewer") == "reviewer"

    def test_qa_maps_to_qa_queue(self, router):
        assert router.queue_for_role("qa") == "qa"

    def test_security_maps_to_security_queue(self, router):
        assert router.queue_for_role("security") == "security"

    def test_release_maps_to_release_queue(self, router):
        assert router.queue_for_role("release") == "release"

    def test_unknown_role_falls_back_to_default(self, router):
        assert router.queue_for_role("unknown_role") == "default"

    def test_ingestion_maps_to_ingestion_queue(self, router):
        assert router.queue_for_role("ingestion") == "ingestion"


class TestDispatch:
    def test_dispatch_returns_celery_task_id(self, router, mock_celery):
        task_id = router.dispatch(
            agent_role="planner",
            task_id="task-uuid",
            run_id="run-uuid",
        )
        assert task_id == "celery-task-id-123"

    def test_dispatch_calls_send_task_with_correct_name(self, router, mock_celery):
        router.dispatch(agent_role="builder", task_id="t1", run_id="r1")
        call_args = mock_celery.send_task.call_args
        assert call_args[0][0] == "forge.agents.builder.execute_task"

    def test_dispatch_passes_task_and_run_ids(self, router, mock_celery):
        router.dispatch(agent_role="reviewer", task_id="t-123", run_id="r-456")
        kwargs = mock_celery.send_task.call_args[1]["kwargs"]
        assert kwargs["task_id"] == "t-123"
        assert kwargs["run_id"] == "r-456"

    def test_dispatch_routes_to_correct_queue(self, router, mock_celery):
        router.dispatch(agent_role="qa", task_id="t1", run_id="r1")
        assert mock_celery.send_task.call_args[1]["queue"] == "qa"

    def test_dispatch_merges_payload_into_kwargs(self, router, mock_celery):
        router.dispatch(
            agent_role="security",
            task_id="t1",
            run_id="r1",
            payload={"assigned_agent_id": "sam"},
        )
        kwargs = mock_celery.send_task.call_args[1]["kwargs"]
        assert kwargs["assigned_agent_id"] == "sam"

    def test_dispatch_unknown_role_raises(self, router):
        with pytest.raises(UnroutableTaskError, match="No Celery task registered"):
            router.dispatch(agent_role="unknown_role", task_id="t1", run_id="r1")

    def test_dispatch_countdown_passed_through(self, router, mock_celery):
        router.dispatch(agent_role="planner", task_id="t1", run_id="r1", countdown=30)
        assert mock_celery.send_task.call_args[1]["countdown"] == 30


class TestUnroutableTaskError:
    def test_error_message_contains_role(self, router):
        with pytest.raises(UnroutableTaskError) as exc_info:
            router.dispatch(agent_role="bogus", task_id="t1", run_id="r1")
        assert "bogus" in str(exc_info.value)

    def test_error_message_lists_known_roles(self, router):
        with pytest.raises(UnroutableTaskError) as exc_info:
            router.dispatch(agent_role="bogus", task_id="t1", run_id="r1")
        assert "builder" in str(exc_info.value)
