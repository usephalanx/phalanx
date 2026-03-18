"""
FORGE Slack Gateway — single entry point for all human commands.

Architecture (evidence in EXECUTION_PLAN.md §B, AD-005):
  - Uses Slack Socket Mode (no public webhook URL needed for MVP).
  - Listens for /forge slash commands and app_mention events.
  - Validates commands via CommandParser, writes WorkOrder to Postgres,
    then dispatches to Commander via Celery.
  - AP-001: This is the ONLY human entry point. No REST API commands for MVP.

Slack Socket Mode docs:
  https://api.slack.com/apis/connections/socket
  https://slack.dev/bolt-python/concepts#socket-mode

Run as: python -m forge.gateway.slack_bot
Or via Docker: command: python -m forge.gateway.slack_bot
"""
from __future__ import annotations

import asyncio
import signal
import sys

import structlog
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from forge.config.settings import get_settings
from forge.gateway.command_parser import CommandType, HELP_TEXT, parse_command
from forge.observability.logging import configure_logging

configure_logging()
log = structlog.get_logger(__name__)
settings = get_settings()


def _build_app(token: str) -> AsyncApp:
    """Create and configure the Slack AsyncApp with all handlers registered."""
    _app = AsyncApp(token=token)

    # ── /forge slash command handler ──────────────────────────────────────────

    @_app.command("/forge")
    async def handle_forge_command(ack, command, say, respond):
        """
        Handle /forge slash command.
        Acknowledges immediately (Slack requires < 3s), then processes async.
        """
        await ack()  # acknowledge within 3 seconds

        user_id = command.get("user_id", "unknown")
        channel_id = command.get("channel_id", "unknown")
        text = command.get("text", "").strip()

        _log = log.bind(user_id=user_id, channel_id=channel_id, command_text=text)
        _log.info("slack_gateway.command_received")

        parsed = parse_command(text)

        if not parsed.is_valid:
            await respond(f"⚠️ {parsed.parse_error}\n\n{HELP_TEXT}")
            return

        if parsed.command_type == CommandType.HELP:
            await respond(HELP_TEXT)
            return

        if parsed.command_type == CommandType.BUILD:
            await _handle_build(parsed, user_id=user_id, channel_id=channel_id, respond=respond)
            return

        if parsed.command_type == CommandType.STATUS:
            await _handle_status(parsed, respond=respond)
            return

        if parsed.command_type == CommandType.CANCEL:
            await _handle_cancel(parsed, user_id=user_id, respond=respond)
            return

        await respond("⚠️ Unknown command. Try `/forge help`.")

    # ── App mention handler ────────────────────────────────────────────────────

    @_app.event("app_mention")
    async def handle_mention(event, say):
        """Respond to @forge mentions with guidance."""
        await say("Use `/forge help` to see available commands.")

    return _app


async def _handle_build(parsed, user_id: str, channel_id: str, respond) -> None:
    """Create a WorkOrder in Postgres and dispatch to Commander."""
    try:
        from forge.db.session import get_db  # noqa: PLC0415
        from forge.db.models import WorkOrder, Channel  # noqa: PLC0415
        from forge.queue.celery_app import celery_app  # noqa: PLC0415
        from forge.runtime.task_router import TaskRouter  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        async with get_db() as session:
            # Resolve or create Channel row for this Slack channel
            stmt = select(Channel).where(
                Channel.platform == "slack",
                Channel.channel_id == channel_id,
            )
            result = await session.execute(stmt)
            channel = result.scalar_one_or_none()

            if channel is None:
                # Channel not registered — need a project association.
                # For MVP, we respond with an onboarding prompt.
                await respond(
                    "⚠️ This Slack channel is not linked to a FORGE project.\n"
                    "Ask your tech lead to run `scripts/seed_team.py` and configure "
                    "this channel in the FORGE admin."
                )
                return

            if channel.project_id is None:
                await respond("⚠️ Channel has no associated project. Contact your FORGE admin.")
                return

            # Create WorkOrder
            wo = WorkOrder(
                project_id=channel.project_id,
                channel_id=channel.id,
                title=parsed.title,
                description=parsed.description,
                raw_command=f"/forge {parsed.raw_text}",
                requested_by=user_id,
                priority=parsed.priority,
                status="OPEN",
            )
            session.add(wo)
            await session.commit()
            await session.refresh(wo)

        log.info("slack_gateway.work_order_created", work_order_id=wo.id, title=wo.title)

        # Dispatch to Commander queue
        router = TaskRouter(celery_app)
        celery_task_id = router.dispatch(
            agent_role="commander",
            task_id=wo.id,  # work_order_id as task_id for the commander
            run_id=wo.id,
            payload={"work_order_id": wo.id, "project_id": wo.project_id},
        )

        priority_label = {90: "P0", 75: "P1", 50: "P2", 25: "P3", 10: "P4"}.get(
            parsed.priority, f"priority={parsed.priority}"
        )

        await respond(
            f"✅ Work order created ({priority_label}): *{parsed.title}*\n"
            f"ID: `{wo.id}`\n"
            f"Commander dispatched. I'll update you here as the run progresses."
        )

    except Exception as exc:
        log.exception("slack_gateway.build_failed", error=str(exc))
        await respond(
            "❌ Failed to create work order. The error has been logged.\n"
            f"Error: `{exc}`"
        )


async def _handle_status(parsed, respond) -> None:
    """Return status of active runs (or a specific run)."""
    try:
        from forge.db.session import get_db  # noqa: PLC0415
        from forge.db.models import Run, WorkOrder  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        async with get_db() as session:
            if parsed.run_id:
                stmt = select(Run).where(Run.id == parsed.run_id)
                result = await session.execute(stmt)
                run = result.scalar_one_or_none()
                if run is None:
                    await respond(f"⚠️ Run `{parsed.run_id}` not found.")
                    return
                await respond(f"Run `{run.id}`: *{run.status}*")
            else:
                # List active (non-terminal) runs
                from forge.workflow.state_machine import TERMINAL_STATES  # noqa: PLC0415
                terminal = [s.value for s in TERMINAL_STATES]
                stmt = (
                    select(Run, WorkOrder.title)
                    .join(WorkOrder, WorkOrder.id == Run.work_order_id)
                    .where(Run.status.notin_(terminal))
                    .order_by(Run.created_at.desc())
                    .limit(10)
                )
                result = await session.execute(stmt)
                rows = result.all()
                if not rows:
                    await respond("No active runs.")
                    return
                lines = ["*Active runs:*"]
                for run, title in rows:
                    lines.append(f"• `{run.id[:8]}…` {run.status} — _{title}_")
                await respond("\n".join(lines))

    except Exception as exc:
        log.exception("slack_gateway.status_failed", error=str(exc))
        await respond(f"❌ Error fetching status: `{exc}`")


async def _handle_cancel(parsed, user_id: str, respond) -> None:
    """Cancel an active run."""
    try:
        from forge.db.session import get_db  # noqa: PLC0415
        from forge.db.models import Run  # noqa: PLC0415
        from forge.workflow.state_machine import RunStatus, validate_transition  # noqa: PLC0415
        from sqlalchemy import select, update  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(select(Run).where(Run.id == parsed.run_id))
            run = result.scalar_one_or_none()

            if run is None:
                await respond(f"⚠️ Run `{parsed.run_id}` not found.")
                return

            try:
                validate_transition(RunStatus(run.status), RunStatus.CANCELLED)
            except Exception:
                await respond(
                    f"⚠️ Run `{run.id}` is in status `{run.status}` and cannot be cancelled."
                )
                return

            await session.execute(
                update(Run)
                .where(Run.id == run.id)
                .values(
                    status="CANCELLED",
                    updated_at=datetime.now(UTC),
                    error_message=f"Cancelled by {user_id} via Slack",
                )
            )
            await session.commit()

        log.info("slack_gateway.run_cancelled", run_id=run.id, cancelled_by=user_id)
        await respond(f"🛑 Run `{run.id}` cancelled.")

    except Exception as exc:
        log.exception("slack_gateway.cancel_failed", error=str(exc))
        await respond(f"❌ Error cancelling run: `{exc}`")


# ── Main entrypoint ───────────────────────────────────────────────────────────

async def main() -> None:
    if not settings.slack_bot_token:
        log.error("slack_gateway.missing_config", missing="SLACK_BOT_TOKEN")
        sys.exit(1)
    if not settings.slack_app_token:
        log.error("slack_gateway.missing_config", missing="SLACK_APP_TOKEN")
        sys.exit(1)

    app = _build_app(settings.slack_bot_token)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    log.info("slack_gateway.starting", socket_mode=True)

    loop = asyncio.get_event_loop()

    def _shutdown(sig, _frame):
        log.info("slack_gateway.shutdown", signal=sig)
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
