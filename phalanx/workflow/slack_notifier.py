"""
SlackNotifier — thread-safe, fire-and-forget Slack progress poster.

Responsibilities:
  - Post progress updates for a Run into a Slack thread anchored on the
    original acknowledgment message (WorkOrder.slack_thread_ts).
  - Format per-task start / complete / fail messages.
  - Post the final run completion summary with PR + showcase links.

Design invariants:
  - NEVER raises. All public methods are wrapped in try/except. A Slack
    outage must not affect the pipeline in any way.
  - Feature-gated: phalanx_enable_slack_threading=False → every method is
    a silent no-op. No Slack calls, no errors.
  - Thread-first: if slack_thread_ts is present, all messages reply to that
    thread. If absent (old runs, non-Slack path), falls back to main channel.
  - Constructed once per Run via SlackNotifier.from_run(run_id, session).
    Caller caches the instance; we never re-query the DB mid-run.

Usage (in Commander / Orchestrator):
    notifier = await SlackNotifier.from_run(run_id, session)
    await notifier.run_started(title="Project Management Tool")
    ...
    await notifier.task_completed(task)
    ...
    await notifier.run_complete(run, tasks)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from slack_sdk.web.async_client import AsyncWebClient

from phalanx.config.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phalanx.db.models import Run, Task

log = structlog.get_logger(__name__)

# Non-fatal agent roles: their FAILED state does not kill the pipeline.
# Reflected in emoji choice (⚠️ vs ❌).
_NON_FATAL_ROLES = frozenset({"qa", "reviewer", "verifier", "security", "integration_wiring"})

# Showcase GitHub org — used to build the demo link in run_complete.
_SHOWCASE_REPO = "https://github.com/usephalanx/showcase/tree"

# ── Progress board constants ───────────────────────────────────────────────────

# Map normalised group name → emoji. Unknown groups get _DEFAULT_GROUP_ICON.
_GROUP_ICONS: dict[str, str] = {
    "planning": "📐",
    "architecture": "📐",
    "backend": "⚙️",
    "backend api": "⚙️",
    "api": "⚙️",
    "database": "🗄️",
    "db": "🗄️",
    "frontend": "🖥️",
    "ui": "🖥️",
    "mobile": "📱",
    "mobile ios": "📱",
    "mobile android": "📱",
    "infrastructure": "🏗️",
    "infra": "🏗️",
    "devops": "🔄",
    "ci/cd": "🔄",
    "cicd": "🔄",
    "qa": "🧪",
    "testing": "🧪",
    "tests": "🧪",
    "security": "🔒",
    "code review": "👀",
    "review": "👀",
    "release": "🚀",
    "deployment": "🚀",
}
_DEFAULT_GROUP_ICON = "📦"

# Per-task status → display icon in the board.
_STATUS_ICONS: dict[str, str] = {
    "PENDING": "◻",
    "IN_PROGRESS": "⏳",
    "COMPLETED": "✅",
    "FAILED": "❌",
    "FAILED_NON_FATAL": "⚠️",
}

# Fallback group label when task.phase_name is None — derived from agent_role.
_ROLE_TO_GROUP: dict[str, str] = {
    "planner": "Planning",
    "builder": "Implementation",
    "component_builder": "Frontend",
    "page_assembler": "Frontend",
    "reviewer": "Code Review",
    "qa": "QA",
    "security": "Security",
    "release": "Release",
}


def _group_icon(group_name: str) -> str:
    """Return the emoji for a group name, defaulting to 📦 for unknown groups."""
    return _GROUP_ICONS.get(group_name.lower().strip(), _DEFAULT_GROUP_ICON)


def _task_group(agent_role: str, phase_name: str | None) -> str:
    """
    Resolve the display group for a task.
    Prefers phase_name (set by Claude during planning) and falls back to
    a role-derived label so old runs / non-enriched paths still work.
    """
    if phase_name and phase_name.strip():
        return phase_name.strip()
    return _ROLE_TO_GROUP.get(agent_role, "Other")


@dataclass
class _BoardTask:
    """Immutable snapshot of a task captured at progress-board creation time."""

    id: str
    title: str
    sequence_num: int
    agent_role: str
    group: str  # resolved display group (phase_name or derived)


class SlackNotifier:
    """
    Posts Run lifecycle events to a Slack thread.

    Construct via SlackNotifier.from_run() — do NOT call __init__ directly.
    """

    def __init__(
        self,
        *,
        channel_id: str | None,
        thread_ts: str | None,
        slack_token: str,
        enabled: bool,
    ) -> None:
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._token = slack_token
        self._enabled = enabled
        self._log = log.bind(
            component="slack_notifier",
            channel_id=channel_id,
            has_thread=thread_ts is not None,
        )

        # Progress board state — populated by post_progress_board()
        self._progress_ts: str | None = None  # ts of the board message
        self._board_tasks: list[_BoardTask] = []  # frozen task snapshots
        self._task_statuses: dict[str, str] = {}  # task_id → status string

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    async def from_run(cls, run_id: str, session: AsyncSession) -> SlackNotifier:
        """
        Load Slack context for the given run_id and return a configured notifier.

        If the feature flag is off, the token is missing, or the run has no
        Slack channel, returns a disabled no-op notifier — callers need not
        check; all methods will silently succeed.

        Query path: Run → WorkOrder (for slack_thread_ts) → Channel (for channel_id str).
        """
        from sqlalchemy import select  # noqa: PLC0415

        from phalanx.db.models import Channel, Run, WorkOrder  # noqa: PLC0415

        settings = get_settings()
        enabled = settings.phalanx_enable_slack_threading and bool(settings.slack_bot_token)

        if not enabled:
            return cls(channel_id=None, thread_ts=None, slack_token="", enabled=False)

        try:
            result = await session.execute(
                select(Channel.channel_id, WorkOrder.slack_thread_ts)
                .join(WorkOrder, WorkOrder.channel_id == Channel.id)
                .join(Run, Run.work_order_id == WorkOrder.id)
                .where(Run.id == run_id)
            )
            row = result.one_or_none()
        except Exception as exc:
            log.warning("slack_notifier.from_run_failed", run_id=run_id, error=str(exc))
            return cls(channel_id=None, thread_ts=None, slack_token="", enabled=False)

        if row is None:
            log.warning(
                "slack_notifier.no_channel_for_run",
                run_id=run_id,
                detail="Run has no linked Channel — possibly created via simulator or API",
            )
            return cls(channel_id=None, thread_ts=None, slack_token="", enabled=False)

        channel_id_str, thread_ts = row
        return cls(
            channel_id=channel_id_str,
            thread_ts=thread_ts,
            slack_token=settings.slack_bot_token,
            enabled=True,
        )

    # ── Run lifecycle events ──────────────────────────────────────────────────

    async def run_started(self, title: str) -> None:
        """
        Post the very first thread message when Commander picks up the WorkOrder.
        This anchors all subsequent messages in the thread.
        """
        await self.post(f"🏗️ Working on *{title}*…")
        self._log.info("slack_notifier.run_started", title=title)

    async def run_planned(self, tasks: list[Task]) -> None:
        """
        Posted after plan approval. Lists task count and agent breakdown.
        """
        if not self._enabled:
            return
        total = len(tasks)
        roles = {}
        for t in tasks:
            roles[t.agent_role] = roles.get(t.agent_role, 0) + 1
        role_summary = "  ".join(f"{r}×{n}" for r, n in sorted(roles.items()))

        await self.post(
            f"✅ *Plan approved* — {total} task{'s' if total != 1 else ''} queued\n{role_summary}"
        )

    async def run_complete(self, run: Run, tasks: list[Task]) -> None:
        """
        Final summary posted when Run reaches READY_TO_MERGE or AWAITING_SHIP_APPROVAL.
        Includes PR link and showcase link when available.
        """
        if not self._enabled:
            return
        completed = sum(1 for t in tasks if t.status == "COMPLETED")
        failed = sum(1 for t in tasks if t.status == "FAILED")
        total = len(tasks)

        files_written = sum(
            len((t.output or {}).get("files_written", [])) for t in tasks if t.output
        )

        elapsed_s = None
        started = getattr(run, "started_at", None) or getattr(run, "created_at", None)
        if started:
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            elapsed_s = int((datetime.now(UTC) - started).total_seconds())

        elapsed_str = ""
        if elapsed_s is not None:
            mins, secs = divmod(elapsed_s, 60)
            elapsed_str = f"  |  ⏱ {mins}m {secs:02}s"

        # Status icon
        run_status = run.status
        if run_status in ("READY_TO_MERGE", "SHIPPED"):
            icon = "🚀"
            headline = "Ready to ship!"
        elif run_status == "AWAITING_SHIP_APPROVAL":
            icon = "🔀"
            headline = "Awaiting ship approval"
        else:
            icon = "⚠️" if failed and completed >= total - failed else "💥"
            headline = "Completed with issues"

        lines = [
            f"{icon} *{headline}*",
            f"Tasks: {completed}/{total} completed"
            + (f", {failed} failed" if failed else "")
            + (f"  |  Files: {files_written}" if files_written else "")
            + elapsed_str,
        ]

        # PR link
        pr_url = getattr(run, "pr_url", None)
        if pr_url:
            lines.append(f"🔀 *PR ready* → <{pr_url}|View Pull Request>")

        # Showcase link (branch = phalanx/run-<run_id>)
        active_branch = getattr(run, "active_branch", None)
        if active_branch:
            showcase_url = f"{_SHOWCASE_REPO}/{active_branch}"
            lines.append(f"💻 *Showcase* → <{showcase_url}|Browse code>")

        await self.post("\n".join(lines))
        self._log.info(
            "slack_notifier.run_complete",
            run_status=run_status,
            completed=completed,
            total=total,
        )

    # ── Progress board ────────────────────────────────────────────────────────

    async def post_progress_board(self, tasks: list[Task]) -> None:
        """
        Post the live progress board to Slack after plan approval.

        Called once by the orchestrator before task dispatch begins.
        All subsequent task lifecycle events update this message in-place
        rather than posting new messages.
        """
        if not self._enabled or not tasks:
            return

        # Build frozen snapshots — we never re-query the DB from the notifier
        self._board_tasks = [
            _BoardTask(
                id=t.id,
                title=t.title,
                sequence_num=t.sequence_num,
                agent_role=t.agent_role,
                group=_task_group(t.agent_role, getattr(t, "phase_name", None)),
            )
            for t in sorted(tasks, key=lambda x: x.sequence_num)
        ]
        self._task_statuses = {t.id: "PENDING" for t in self._board_tasks}

        total = len(self._board_tasks)
        text = f"📋 Build Progress — 0/{total} complete"
        blocks = self._build_progress_blocks()
        ts = await self.post(text=text, blocks=blocks)
        self._progress_ts = ts
        self._log.info(
            "slack_notifier.progress_board_posted",
            task_count=total,
            has_ts=ts is not None,
        )

    def _build_progress_blocks(self) -> list[dict]:
        """Build Block Kit blocks for the current progress board state."""
        done = sum(1 for s in self._task_statuses.values() if s == "COMPLETED")
        total = len(self._board_tasks)

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📋 Build Progress  ·  {done}/{total} done",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        # Group tasks — preserve insertion order (sequence_num sorted at snapshot time)
        groups: dict[str, list[_BoardTask]] = {}
        for bt in self._board_tasks:
            groups.setdefault(bt.group, []).append(bt)

        for group_name, group_tasks in groups.items():
            icon = _group_icon(group_name)
            group_done = sum(1 for t in group_tasks if self._task_statuses.get(t.id) == "COMPLETED")
            group_total = len(group_tasks)

            task_lines = []
            for bt in group_tasks:
                status = self._task_statuses.get(bt.id, "PENDING")
                status_icon = _STATUS_ICONS.get(status, "◻")
                task_lines.append(f"{status_icon}  {bt.title[:80]}")

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{icon}  *{group_name}*  —  {group_done}/{group_total}\n"
                            + "\n".join(task_lines)
                        ),
                    },
                }
            )
            blocks.append({"type": "divider"})

        return blocks

    async def _update_board(self) -> None:
        """Rebuild and push an updated progress board to Slack."""
        if not self._enabled or not self._progress_ts or not self._board_tasks:
            return
        done = sum(1 for s in self._task_statuses.values() if s == "COMPLETED")
        total = len(self._board_tasks)
        text = f"📋 Build Progress — {done}/{total} complete"
        blocks = self._build_progress_blocks()
        try:
            client = AsyncWebClient(token=self._token)
            await client.chat_update(
                channel=self._channel_id,
                ts=self._progress_ts,
                text=text,
                blocks=blocks,
            )
        except Exception as exc:
            self._log.warning("slack_notifier.board_update_failed", error=str(exc))

    # ── Task lifecycle events ─────────────────────────────────────────────────

    async def task_started(self, task: Task) -> None:
        """Update progress board when a task goes IN_PROGRESS."""
        if not self._enabled:
            return
        if task.id in self._task_statuses:
            self._task_statuses[task.id] = "IN_PROGRESS"
            await self._update_board()

    async def task_completed(self, task: Task) -> None:
        """Update progress board when a task reaches COMPLETED."""
        if not self._enabled:
            return
        if task.id in self._task_statuses:
            self._task_statuses[task.id] = "COMPLETED"
            await self._update_board()

    async def task_failed(self, task: Task) -> None:
        """Update progress board when a task reaches FAILED."""
        if not self._enabled:
            return
        if task.id in self._task_statuses:
            is_non_fatal = task.agent_role in _NON_FATAL_ROLES
            self._task_statuses[task.id] = "FAILED_NON_FATAL" if is_non_fatal else "FAILED"
            await self._update_board()

    # ── Core post primitive ───────────────────────────────────────────────────

    async def post(self, text: str, blocks: list | None = None) -> str | None:
        """
        Post a message to the run's thread (or channel if no thread_ts).

        Returns the message ts on success, None on failure or if disabled.
        Never raises — Slack errors are logged as warnings.
        """
        if not self._enabled or not self._channel_id:
            return None

        try:
            client = AsyncWebClient(token=self._token)
            kwargs: dict = {"channel": self._channel_id, "text": text}
            if self._thread_ts:
                kwargs["thread_ts"] = self._thread_ts
            if blocks:
                kwargs["blocks"] = blocks

            resp = await client.chat_postMessage(**kwargs)
            return resp["ts"]

        except Exception as exc:
            self._log.warning("slack_notifier.post_failed", error=str(exc), text=text[:80])
            return None
