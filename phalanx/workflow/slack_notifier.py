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

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    async def from_run(cls, run_id: str, session: AsyncSession) -> "SlackNotifier":
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
        enabled = (
            settings.phalanx_enable_slack_threading
            and bool(settings.slack_bot_token)
        )

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
            f"✅ *Plan approved* — {total} task{'s' if total != 1 else ''} queued\n"
            f"{role_summary}"
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
            len((t.output or {}).get("files_written", []))
            for t in tasks
            if t.output
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

    # ── Task lifecycle events ─────────────────────────────────────────────────

    async def task_started(self, task: Task) -> None:
        """Posted when a task transitions to IN_PROGRESS."""
        if not self._enabled:
            return
        await self.post(
            f"⏳ `seq={task.sequence_num:02}` *{task.agent_role}* — _{task.title[:60]}_"
        )

    async def task_completed(self, task: Task) -> None:
        """Posted when a task reaches COMPLETED."""
        if not self._enabled:
            return
        files = len((task.output or {}).get("files_written", [])) if task.output else 0
        files_str = f"  [{files} file{'s' if files != 1 else ''}]" if files else ""

        elapsed_str = ""
        if task.started_at and task.completed_at:
            s_at = task.started_at
            c_at = task.completed_at
            if s_at.tzinfo is None:
                s_at = s_at.replace(tzinfo=UTC)
            if c_at.tzinfo is None:
                c_at = c_at.replace(tzinfo=UTC)
            secs = int((c_at - s_at).total_seconds())
            mins, rem = divmod(secs, 60)
            elapsed_str = f"  {mins}m{rem:02}s" if mins else f"  {rem}s"

        await self.post(
            f"✅ `seq={task.sequence_num:02}` *{task.agent_role}* — _{task.title[:60]}_"
            f"{elapsed_str}{files_str}"
        )

    async def task_failed(self, task: Task) -> None:
        """
        Posted when a task reaches FAILED.
        Non-fatal roles (qa, reviewer, etc.) use ⚠️ — fatal roles use ❌.
        """
        if not self._enabled:
            return
        is_non_fatal = task.agent_role in _NON_FATAL_ROLES
        icon = "⚠️" if is_non_fatal else "❌"
        suffix = " _(non-fatal)_" if is_non_fatal else ""

        error_snippet = ""
        if task.error and not is_non_fatal:
            trimmed = task.error[:200].replace("\n", " ")
            error_snippet = f"\n```{trimmed}```"

        await self.post(
            f"{icon} `seq={task.sequence_num:02}` *{task.agent_role}* FAILED"
            f"{suffix} — _{task.title[:60]}_{error_snippet}"
        )

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
