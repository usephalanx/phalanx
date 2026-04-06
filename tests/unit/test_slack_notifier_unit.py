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

from phalanx.workflow.slack_notifier import (
    SlackNotifier,
    _NON_FATAL_ROLES,
    _GROUP_ICONS,
    _DEFAULT_GROUP_ICON,
    _group_icon,
    _task_group,
)


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
    task_id=None,
    seq=1,
    role="builder",
    title="Build Something",
    status="COMPLETED",
    output=None,
    error=None,
    started_at=None,
    completed_at=None,
    phase_name=None,
):
    t = MagicMock()
    t.id = task_id or f"task-{seq}"
    t.sequence_num = seq
    t.agent_role = role
    t.title = title
    t.status = status
    t.output = output
    t.error = error
    t.started_at = started_at
    t.completed_at = completed_at
    t.phase_name = phase_name
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
    """Patch AsyncWebClient so chat_postMessage and chat_update return fake ts values."""
    mock_client = AsyncMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ts": post_return_ts})
    mock_client.chat_update = AsyncMock(return_value={"ts": post_return_ts})
    return patch(
        "phalanx.workflow.slack_notifier.AsyncWebClient",
        return_value=mock_client,
    ), mock_client


def _make_notifier_with_board(tasks, *, channel_id="C123", thread_ts="1711111111.000100"):
    """Create an enabled notifier pre-loaded with a board snapshot (simulates after post_progress_board)."""
    from phalanx.workflow.slack_notifier import _BoardTask, _task_group
    notifier = _make_notifier(channel_id=channel_id, thread_ts=thread_ts, enabled=True)
    notifier._progress_ts = "1711111111.board"
    notifier._board_tasks = [
        _BoardTask(
            id=t.id,
            title=t.title,
            sequence_num=t.sequence_num,
            agent_role=t.agent_role,
            group=_task_group(t.agent_role, getattr(t, "phase_name", None)),
        )
        for t in sorted(tasks, key=lambda x: x.sequence_num)
    ]
    notifier._task_statuses = {t.id: "PENDING" for t in notifier._board_tasks}
    return notifier


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


# ── Progress board helpers ────────────────────────────────────────────────────


class TestGroupIcon:
    def test_known_group_returns_correct_icon(self):
        assert _group_icon("backend") == "⚙️"
        assert _group_icon("frontend") == "🖥️"
        assert _group_icon("database") == "🗄️"
        assert _group_icon("qa") == "🧪"
        assert _group_icon("security") == "🔒"
        assert _group_icon("infrastructure") == "🏗️"
        assert _group_icon("planning") == "📐"

    def test_case_insensitive(self):
        assert _group_icon("Backend") == _group_icon("backend")
        assert _group_icon("FRONTEND") == _group_icon("frontend")

    def test_unknown_group_returns_default(self):
        assert _group_icon("SomeRandomGroup") == _DEFAULT_GROUP_ICON
        assert _group_icon("") == _DEFAULT_GROUP_ICON

    def test_all_known_keys_in_group_icons(self):
        for key in _GROUP_ICONS:
            assert _group_icon(key) == _GROUP_ICONS[key]


class TestTaskGroup:
    def test_prefers_phase_name_when_present(self):
        assert _task_group("builder", "Backend API") == "Backend API"

    def test_strips_whitespace_from_phase_name(self):
        assert _task_group("builder", "  Frontend  ") == "Frontend"

    def test_falls_back_to_role_when_phase_name_none(self):
        assert _task_group("planner", None) == "Planning"
        assert _task_group("builder", None) == "Implementation"
        assert _task_group("qa", None) == "QA"

    def test_falls_back_to_other_for_unknown_role(self):
        assert _task_group("mystery_agent", None) == "Other"

    def test_empty_phase_name_string_falls_back(self):
        assert _task_group("builder", "") == "Implementation"
        assert _task_group("builder", "   ") == "Implementation"


# ── post_progress_board() ─────────────────────────────────────────────────────


class TestPostProgressBoard:
    async def test_posts_initial_board_and_stores_ts(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="planner", title="Plan", phase_name="Planning"),
            _make_task(task_id="t2", seq=2, role="builder", title="Build API", phase_name="Backend API"),
        ]
        notifier = _make_notifier()
        patcher, mock_client = _patch_client(post_return_ts="9999.board")

        with patcher:
            await notifier.post_progress_board(tasks)

        assert notifier._progress_ts == "9999.board"
        mock_client.chat_postMessage.assert_called_once()

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        tasks = [_make_task(task_id="t1")]

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.post_progress_board(tasks)

        mock_cls.assert_not_called()
        assert notifier._progress_ts is None

    async def test_no_op_when_empty_task_list(self):
        notifier = _make_notifier()

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.post_progress_board([])

        mock_cls.assert_not_called()

    async def test_all_tasks_start_pending(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="planner"),
            _make_task(task_id="t2", seq=2, role="builder"),
        ]
        notifier = _make_notifier()
        patcher, _ = _patch_client()

        with patcher:
            await notifier.post_progress_board(tasks)

        assert notifier._task_statuses == {"t1": "PENDING", "t2": "PENDING"}

    async def test_board_task_snapshots_sorted_by_sequence(self):
        tasks = [
            _make_task(task_id="t3", seq=3, role="builder", title="Task C"),
            _make_task(task_id="t1", seq=1, role="planner", title="Task A"),
            _make_task(task_id="t2", seq=2, role="reviewer", title="Task B"),
        ]
        notifier = _make_notifier()
        patcher, _ = _patch_client()

        with patcher:
            await notifier.post_progress_board(tasks)

        seqs = [bt.sequence_num for bt in notifier._board_tasks]
        assert seqs == [1, 2, 3]

    async def test_posts_blocks_in_initial_call(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier()
        patcher, mock_client = _patch_client()

        with patcher:
            await notifier.post_progress_board(tasks)

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "blocks" in call_kwargs
        blocks = call_kwargs["blocks"]
        assert any(b.get("type") == "header" for b in blocks)


# ── _build_progress_blocks() ──────────────────────────────────────────────────


class TestBuildProgressBlocks:
    def _setup_notifier_with_tasks(self, tasks):
        return _make_notifier_with_board(tasks)

    def test_header_shows_done_count(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="planner", phase_name="Planning"),
            _make_task(task_id="t2", seq=2, role="builder", phase_name="Backend"),
        ]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "COMPLETED"

        blocks = notifier._build_progress_blocks()

        header_text = blocks[0]["text"]["text"]
        assert "1/2" in header_text

    def test_groups_tasks_by_phase_name(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="builder", title="API", phase_name="Backend"),
            _make_task(task_id="t2", seq=2, role="builder", title="DB", phase_name="Database"),
        ]
        notifier = _make_notifier_with_board(tasks)

        blocks = notifier._build_progress_blocks()

        block_texts = [b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"]
        assert any("Backend" in t for t in block_texts)
        assert any("Database" in t for t in block_texts)

    def test_same_phase_tasks_in_same_group(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="builder", title="API", phase_name="Backend"),
            _make_task(task_id="t2", seq=2, role="builder", title="Auth", phase_name="Backend"),
        ]
        notifier = _make_notifier_with_board(tasks)

        blocks = notifier._build_progress_blocks()

        section_blocks = [b for b in blocks if b.get("type") == "section"]
        assert len(section_blocks) == 1  # both in same Backend group
        text = section_blocks[0]["text"]["text"]
        assert "API" in text
        assert "Auth" in text

    def test_pending_task_shows_empty_box(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        # status is PENDING by default

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "◻" in section_text

    def test_in_progress_task_shows_hourglass(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "IN_PROGRESS"

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "⏳" in section_text

    def test_completed_task_shows_checkmark(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "COMPLETED"

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "✅" in section_text

    def test_failed_task_shows_x(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "FAILED"

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "❌" in section_text

    def test_failed_non_fatal_shows_warning(self):
        tasks = [_make_task(task_id="t1", seq=1, role="qa", phase_name="QA")]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "FAILED_NON_FATAL"

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "⚠️" in section_text

    def test_group_shows_group_done_count(self):
        tasks = [
            _make_task(task_id="t1", seq=1, role="builder", title="A", phase_name="Backend"),
            _make_task(task_id="t2", seq=2, role="builder", title="B", phase_name="Backend"),
        ]
        notifier = _make_notifier_with_board(tasks)
        notifier._task_statuses["t1"] = "COMPLETED"

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "1/2" in section_text

    def test_group_icon_appears_in_section(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)

        blocks = notifier._build_progress_blocks()

        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "⚙️" in section_text  # backend icon


# ── task_started/completed/failed (board update path) ────────────────────────


class TestTaskStarted:
    async def test_updates_status_to_in_progress(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier.task_started(tasks[0])

        assert notifier._task_statuses["t1"] == "IN_PROGRESS"
        mock_client.chat_update.assert_called_once()

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        task = _make_task(task_id="t1")

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.task_started(task)

        mock_cls.assert_not_called()

    async def test_no_op_if_task_not_in_board(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        unknown_task = _make_task(task_id="unknown-id", seq=99, role="builder")

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier.task_started(unknown_task)

        mock_client.chat_update.assert_not_called()


class TestTaskCompleted:
    async def test_updates_status_to_completed(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier.task_completed(tasks[0])

        assert notifier._task_statuses["t1"] == "COMPLETED"
        mock_client.chat_update.assert_called_once()

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        task = _make_task(task_id="t1")

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.task_completed(task)

        mock_cls.assert_not_called()


class TestTaskFailed:
    @pytest.mark.parametrize("role", sorted(_NON_FATAL_ROLES))
    async def test_non_fatal_roles_set_failed_non_fatal_status(self, role):
        task = _make_task(task_id="t1", seq=1, role=role, phase_name="QA")
        notifier = _make_notifier_with_board([task])

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier.task_failed(task)

        assert notifier._task_statuses["t1"] == "FAILED_NON_FATAL"
        mock_client.chat_update.assert_called_once()

    async def test_fatal_role_sets_failed_status(self):
        task = _make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")
        notifier = _make_notifier_with_board([task])

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier.task_failed(task)

        assert notifier._task_statuses["t1"] == "FAILED"

    async def test_no_op_when_disabled(self):
        notifier = _make_notifier(enabled=False)
        task = _make_task(task_id="t1")

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier.task_failed(task)

        mock_cls.assert_not_called()

    async def test_failed_status_reflected_in_board_blocks(self):
        task = _make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")
        notifier = _make_notifier_with_board([task])

        patcher, _ = _patch_client()
        with patcher:
            await notifier.task_failed(task)

        blocks = notifier._build_progress_blocks()
        section_text = next(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "❌" in section_text


# ── _update_board() ───────────────────────────────────────────────────────────


class TestUpdateBoard:
    async def test_calls_chat_update_with_correct_ts(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)

        patcher, mock_client = _patch_client()
        with patcher:
            await notifier._update_board()

        call_kwargs = mock_client.chat_update.call_args.kwargs
        assert call_kwargs["ts"] == "1711111111.board"
        assert call_kwargs["channel"] == "C123"
        assert "blocks" in call_kwargs

    async def test_no_op_when_no_progress_ts(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)
        notifier._progress_ts = None  # board never posted

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier._update_board()

        mock_cls.assert_not_called()

    async def test_no_op_when_no_board_tasks(self):
        notifier = _make_notifier()
        notifier._progress_ts = "9999.board"
        # _board_tasks is empty (default)

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient") as mock_cls:
            await notifier._update_board()

        mock_cls.assert_not_called()

    async def test_swallows_chat_update_exception(self):
        tasks = [_make_task(task_id="t1", seq=1, role="builder", phase_name="Backend")]
        notifier = _make_notifier_with_board(tasks)

        mock_client = AsyncMock()
        mock_client.chat_update.side_effect = Exception("Slack down")

        with patch("phalanx.workflow.slack_notifier.AsyncWebClient", return_value=mock_client):
            await notifier._update_board()  # must not raise


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
