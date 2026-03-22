"""
Unit tests for phalanx/workflow/slack_notifier.py

Coverage targets:
  - from_run(): flag off, no token, no channel, happy path
  - post(): disabled no-op, no channel, thread_ts injected, fallback to channel, error swallowed
  - run_started / run_planned / run_complete: message content + error resilience
  - task_started / task_completed / task_failed: content, non-fatal/fatal distinction
  - run_complete: PR link, showcase link, elapsed time, files_written aggregation

All Slack SDK calls are mocked — no real network calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.workflow.slack_notifier import SlackNotifier, _NON_FATAL_ROLES


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_notifier(*, channel_id="C123", thread_ts="1711111111.000100", enabled=True):
    return SlackNotifier(
        channel_id=channel_id,
        thread_ts=thread_ts,
        slack_token="xoxb-test-token",
        enabled=enabled,
    )


def _make_task(
    *,
    seq=1,
    role="builder",
    title="Build Something",
    status="COMPLETED",
    output=None,
    error=None,
    started_at=None,
    completed_at=None,
):
    t = MagicMock()
    t.sequence_num = seq
    t.agent_role = role
    t.title = title
    t.status = status
    t.output = output
    t.error = error
    t.started_at = started_at
    t.completed_at = completed_at
    return t


def _make_run(
    *,
    run_id="run-abc123",
    status="READY_TO_MERGE",
    pr_url=None,
    active_branch=None,
    created_at=None,
    started_at=None,
):
    r = MagicMock()
    r.id = run_id
    r.status = status
    r.pr_url = pr_url
    r.active_branch = active_branch
    r.created_at = created_at or datetime.now(UTC)
    r.started_at = started_at
    return r


def _patch_client(post_return_ts="1711111111.000200"):
    """Patch AsyncWebClient so chat_postMessage returns a fake ts."""
    mock_client = AsyncMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ts": post_return_ts})
    return patch(
        "phalanx.workflow.slack_notifier.AsyncWebClient",
        return_value=mock_client,
    ), mock_client


# ── from_run() ────────────────────────────────────────────────────────────────


class TestFromRun:
    async def test_returns_disabled_when_flag_off(self):
        session = AsyncMock()
        with patch("phalanx.workflow.slack_notifier.get_settings") as mock_settings:
            mock_settings.return_value.phalanx_enable_slack_threading = False
            mock_settings.return_value.slack_bot_token = "xoxb-token"
            notifier = await SlackNotifier.from_run("run-1", session)

        assert notifier._enabled is False
        session.execute.assert_not_called()

    async def test_returns_disabled_when_no_token(self):
        session = AsyncMock()
        with patch("phalanx.workflow.slack_notifier.get_settings") as mock_settings:
            mock_settings.return_value.phalanx_enable_slack_threading = True
            mock_settings.return_value.slack_bot_token = ""
            notifier = await SlackNotifier.from_run("run-1", session)

        assert notifier._enabled is False
        session.execute.assert_not_called()

    async def test_returns_disabled_when_no_channel_row(self):
        session = AsyncMock()
        result = MagicMock()
        result.one_or_none.return_value = None
        session.execute.return_value = result

        with patch("phalanx.workflow.slack_notifier.get_settings") as mock_settings:
            mock_settings.return_value.phalanx_enable_slack_threading = True
            mock_settings.return_value.slack_bot_token = "xoxb-token"
            notifier = await SlackNotifier.from_run("run-1", session)

        assert notifier._enabled is False

    async def test_loads_channel_and_thread_ts(self):
        session = AsyncMock()
        result = MagicMock()
        result.one_or_none.return_value = ("C0AJ3DCUS", "1711111111.000100")
        session.execute.return_value = result

        with patch("phalanx.workflow.slack_notifier.get_settings") as mock_settings:
            mock_settings.return_value.phalanx_enable_slack_threading = True
            mock_settings.return_value.slack_bot_token = "xoxb-token"
            notifier = await SlackNotifier.from_run("run-1", session)

        assert notifier._enabled is True
        assert notifier._channel_id == "C0AJ3DCUS"
        assert notifier._thread_ts == "1711111111.000100"

    async def test_handles_db_exception_gracefully(self):
        session = AsyncMock()
        session.execute.side_effect = Exception("DB error")

        with patch("phalanx.workflow.slack_notifier.get_settings") as mock_settings:
            mock_settings.return_value.phalanx_enable_slack_threading = True
            mock_settings.return_value.slack_bot_token = "xoxb-token"
            # Must not raise
            notifier = await SlackNotifier.from_run("run-1", session)

        assert notifier._enabled is False


# ── post() ────────────────────────────────────────────────────────────────────


class TestPost:
    async def test_returns_none_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        result = await notifier.post("hello")
        assert result is None

    async def test_returns_none_when_no_channel(self):
        notifier = _make_notifier(channel_id=None)
        result = await notifier.post("hello")
        assert result is None

    async def test_posts_with_thread_ts(self):
        notifier = _make_notifier(thread_ts="1711111111.000100")
        patcher, mock_client = _patch_client()

        with patcher:
            ts = await notifier.post("test message")

        assert ts == "1711111111.000200"
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1711111111.000100"
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["text"] == "test message"

    async def test_posts_to_channel_without_thread_ts(self):
        notifier = _make_notifier(thread_ts=None)
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.post("test message")

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in call_kwargs

    async def test_passes_blocks_when_provided(self):
        notifier = _make_notifier()
        patcher, mock_client = _patch_client()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

        with patcher:
            await notifier.post("fallback text", blocks=blocks)

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["blocks"] == blocks

    async def test_swallows_slack_sdk_exception(self):
        notifier = _make_notifier()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = Exception("Slack is down")

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient", return_value=mock_client):
            result = await notifier.post("message")  # must not raise

        assert result is None

    async def test_returns_ts_on_success(self):
        notifier = _make_notifier()
        patcher, _ = _patch_client(post_return_ts="9999.0001")

        with patcher:
            ts = await notifier.post("hi")

        assert ts == "9999.0001"


# ── run_started() ─────────────────────────────────────────────────────────────


class TestRunStarted:
    async def test_posts_title_in_message(self):
        notifier = _make_notifier()
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_started("Project Management Tool")

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "Project Management Tool" in text
        assert "🏗️" in text

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        # must not raise, must not call Slack
        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.run_started("anything")
        mock_cls.assert_not_called()


# ── run_planned() ─────────────────────────────────────────────────────────────


class TestRunPlanned:
    async def test_posts_task_count(self):
        tasks = [
            _make_task(seq=1, role="planner"),
            _make_task(seq=2, role="builder"),
            _make_task(seq=3, role="builder"),
            _make_task(seq=4, role="qa"),
        ]
        notifier = _make_notifier()
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_planned(tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "4 tasks" in text
        assert "builder×2" in text
        assert "planner×1" in text
        assert "qa×1" in text

    async def test_singular_task_label(self):
        tasks = [_make_task(seq=1, role="builder")]
        notifier = _make_notifier()
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_planned(tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "1 task " in text  # not "1 tasks"


# ── task_started() ────────────────────────────────────────────────────────────


class TestTaskStarted:
    async def test_formats_seq_and_role(self):
        notifier = _make_notifier()
        task = _make_task(seq=3, role="builder", title="Build REST API")
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_started(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "seq=03" in text
        assert "builder" in text
        assert "Build REST API" in text
        assert "⏳" in text


# ── task_completed() ──────────────────────────────────────────────────────────


class TestTaskCompleted:
    async def test_shows_checkmark_and_title(self):
        notifier = _make_notifier()
        task = _make_task(seq=2, role="builder", title="Database Models")
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_completed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "✅" in text
        assert "seq=02" in text
        assert "Database Models" in text

    async def test_includes_file_count_when_present(self):
        notifier = _make_notifier()
        task = _make_task(
            seq=2,
            role="builder",
            output={"files_written": ["a.py", "b.py", "c.py"]},
        )
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_completed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "3 files" in text

    async def test_no_file_count_when_no_output(self):
        notifier = _make_notifier()
        task = _make_task(seq=1, role="planner", output=None)
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_completed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "file" not in text

    async def test_includes_elapsed_when_timestamps_present(self):
        notifier = _make_notifier()
        started = datetime(2026, 3, 22, 15, 0, 0, tzinfo=UTC)
        completed = datetime(2026, 3, 22, 15, 2, 30, tzinfo=UTC)  # 2m30s
        task = _make_task(seq=2, role="builder", started_at=started, completed_at=completed)
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_completed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "2m" in text


# ── task_failed() ─────────────────────────────────────────────────────────────


class TestTaskFailed:
    @pytest.mark.parametrize("role", sorted(_NON_FATAL_ROLES))
    async def test_non_fatal_roles_use_warning_emoji(self, role):
        notifier = _make_notifier()
        task = _make_task(seq=4, role=role, status="FAILED", error="tests failed")
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_failed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text
        assert "non-fatal" in text
        assert "❌" not in text

    async def test_fatal_role_uses_x_emoji(self):
        notifier = _make_notifier()
        task = _make_task(seq=2, role="builder", status="FAILED", error="syntax error")
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_failed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "❌" in text
        assert "⚠️" not in text

    async def test_fatal_includes_error_snippet(self):
        notifier = _make_notifier()
        task = _make_task(
            seq=2, role="builder", status="FAILED", error="ImportError: no module named 'foo'"
        )
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_failed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "ImportError" in text

    async def test_non_fatal_omits_error_snippet(self):
        """Non-fatal failures don't expose error details (usually test output noise)."""
        notifier = _make_notifier()
        task = _make_task(
            seq=4, role="qa", status="FAILED", error="AssertionError: 1 != 2"
        )
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.task_failed(task)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "AssertionError" not in text


# ── run_complete() ────────────────────────────────────────────────────────────


class TestRunComplete:
    async def test_ready_to_merge_uses_rocket(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE")
        tasks = [_make_task(status="COMPLETED"), _make_task(status="COMPLETED")]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "🚀" in text
        assert "2/2" in text

    async def test_includes_pr_link_when_present(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE", pr_url="https://github.com/usephalanx/showcase/pull/42")
        tasks = [_make_task(status="COMPLETED")]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "github.com/usephalanx/showcase/pull/42" in text
        assert "PR ready" in text

    async def test_includes_showcase_link_when_branch_present(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE", active_branch="phalanx/run-abc12345")
        tasks = [_make_task(status="COMPLETED")]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "phalanx/run-abc12345" in text
        assert "Showcase" in text

    async def test_omits_pr_link_when_absent(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE", pr_url=None)
        tasks = [_make_task(status="COMPLETED")]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "PR" not in text

    async def test_aggregates_files_written(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE")
        tasks = [
            _make_task(status="COMPLETED", output={"files_written": ["a.py", "b.py"]}),
            _make_task(status="COMPLETED", output={"files_written": ["c.py"]}),
            _make_task(status="COMPLETED", output=None),
        ]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "Files: 3" in text

    async def test_includes_failed_count_when_nonzero(self):
        notifier = _make_notifier()
        run = _make_run(status="READY_TO_MERGE")
        tasks = [
            _make_task(seq=1, status="COMPLETED"),
            _make_task(seq=2, status="FAILED", role="qa"),
        ]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "1/2" in text
        assert "1 failed" in text

    async def test_includes_elapsed_time(self):
        notifier = _make_notifier()
        started = datetime(2026, 3, 22, 15, 0, 0, tzinfo=UTC)
        run = _make_run(status="READY_TO_MERGE", started_at=started)
        run.created_at = started
        tasks = [_make_task(status="COMPLETED")]
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.run_complete(run, tasks)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        # elapsed will be > 0 but we just check format
        assert "m" in text  # e.g. "14m05s"

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        run = _make_run()
        tasks = [_make_task(status="COMPLETED")]

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.run_complete(run, tasks)

        mock_cls.assert_not_called()
