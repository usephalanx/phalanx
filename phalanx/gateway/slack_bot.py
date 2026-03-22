"""
Phalanx Slack Gateway — single entry point for all human commands.

Architecture:
  - Uses Slack Socket Mode (no public webhook URL needed for MVP).
  - Listens for /phalanx slash commands and app_mention events.
  - Validates commands via CommandParser, writes WorkOrder to Postgres,
    then dispatches to Commander via Celery.
  - AP-001: This is the ONLY human entry point. No REST API commands for MVP.

Slack Socket Mode docs:
  https://api.slack.com/apis/connections/socket
  https://slack.dev/bolt-python/concepts#socket-mode

Run as: python -m phalanx.gateway.slack_bot
Or via Docker: command: python -m phalanx.gateway.slack_bot
"""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from phalanx.config.settings import get_settings
from phalanx.gateway.command_parser import HELP_TEXT, CommandType, parse_command
from phalanx.gateway.health import GatewayHealthServer
from phalanx.observability.logging import configure_logging

configure_logging()
log = structlog.get_logger(__name__)
settings = get_settings()


def _build_app(token: str) -> AsyncApp:
    """Create and configure the Slack AsyncApp with all handlers registered."""
    _app = AsyncApp(token=token)

    # ── /phalanx slash command handler ──────────────────────────────────────────

    @_app.command("/phalanx")
    async def handle_forge_command(ack, command, say, respond, client):
        """
        Handle /phalanx slash command.
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
            await _handle_build(parsed, user_id=user_id, channel_id=channel_id, respond=respond, client=client)
            return

        if parsed.command_type == CommandType.STATUS:
            await _handle_status(parsed, respond=respond)
            return

        if parsed.command_type == CommandType.CANCEL:
            await _handle_cancel(parsed, user_id=user_id, respond=respond)
            return

        await respond("⚠️ Unknown command. Try `/phalanx help`.")

    # ── App mention handler ────────────────────────────────────────────────────

    @_app.event("app_mention")
    async def handle_mention(event, say):
        """Respond to @phalanx mentions with guidance."""
        await say("Use `/phalanx help` to see available commands.")

    # ── Approval button handlers ───────────────────────────────────────────────

    @_app.action("phalanx_approve")
    async def handle_approve(ack, body, client):
        """Handle the Approve button click on an approval gate message."""
        await ack()
        await _handle_approval_action(body, client, decision="APPROVED")

    @_app.action("phalanx_reject")
    async def handle_reject(ack, body, client):
        """Handle the Reject button click on an approval gate message."""
        await ack()
        await _handle_approval_action(body, client, decision="REJECTED")

    return _app


async def _handle_build(parsed, user_id: str, channel_id: str, respond, client=None) -> None:
    """Create a WorkOrder in Postgres and dispatch to Commander."""
    try:
        from sqlalchemy import select, update  # noqa: PLC0415

        from phalanx.db.models import Channel, WorkOrder  # noqa: PLC0415
        from phalanx.db.session import get_db  # noqa: PLC0415
        from phalanx.queue.celery_app import celery_app  # noqa: PLC0415
        from phalanx.runtime.task_router import TaskRouter  # noqa: PLC0415

        async with get_db() as session:
            # Resolve Channel row for this Slack channel
            stmt = select(Channel).where(
                Channel.platform == "slack",
                Channel.channel_id == channel_id,
            )
            result = await session.execute(stmt)
            channel = result.scalar_one_or_none()

            if channel is None:
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
                raw_command=f"/phalanx {parsed.raw_text}",
                requested_by=user_id,
                priority=parsed.priority,
                status="OPEN",
            )
            session.add(wo)
            await session.commit()
            await session.refresh(wo)

            # ── Post acknowledgment and anchor the Slack thread ──────────────
            # Uses chat_postMessage (not respond/response_url) so we get a real
            # message ts back — this becomes the thread anchor for all progress
            # updates posted by Commander and Orchestrator throughout the run.
            # Stored on WorkOrder BEFORE dispatching Commander so it's always
            # visible when Commander loads the WorkOrder from DB.
            thread_ts: str | None = None
            if client is not None:
                try:
                    resp = await client.chat_postMessage(
                        channel=channel_id,
                        text=f"🏗️ Got it! Building *{wo.title}* — I'll keep you posted here.",
                    )
                    thread_ts = resp.get("ts")
                except Exception as slack_exc:
                    log.warning("slack_gateway.ack_post_failed", error=str(slack_exc))

                if thread_ts:
                    _wo_cols = {c.key for c in WorkOrder.__table__.columns}
                    if "slack_thread_ts" in _wo_cols:
                        await session.execute(
                            update(WorkOrder)
                            .where(WorkOrder.id == wo.id)
                            .values(slack_thread_ts=thread_ts)
                        )
                        await session.commit()
                        log.info(
                            "slack_gateway.thread_ts_stored",
                            work_order_id=wo.id,
                            thread_ts=thread_ts,
                        )

        log.info("slack_gateway.work_order_created", work_order_id=wo.id, title=wo.title)

        # Dispatch to Commander queue — always after session close and after
        # thread_ts is committed so Commander sees it on first WorkOrder load.
        router = TaskRouter(celery_app)
        router.dispatch(
            agent_role="commander",
            task_id=wo.id,
            run_id=wo.id,
            payload={"work_order_id": wo.id, "project_id": wo.project_id},
        )

        # Only fall back to respond() when we have no client or postMessage failed.
        # When thread_ts is set the channel message already serves as the ack.
        if not thread_ts:
            await respond(
                f"🏗️ Building *{wo.title}* — I'll post updates here as the run progresses."
            )

    except Exception as exc:
        log.exception("slack_gateway.build_failed", error=str(exc))
        await respond("❌ Something went wrong — our team has been notified.")


_STATUS_EMOJI: dict[str, str] = {
    "INTAKE": "📥",
    "RESEARCHING": "🔍",
    "PLANNING": "📝",
    "AWAITING_PLAN_APPROVAL": "⏳",
    "EXECUTING": "⚙️",
    "VERIFYING": "🔬",
    "AWAITING_SHIP_APPROVAL": "⏳",
    "READY_TO_MERGE": "🔀",
    "MERGED": "✅",
    "RELEASE_PREP": "📦",
    "AWAITING_RELEASE_APPROVAL": "⏳",
    "SHIPPED": "🚀",
    "FAILED": "❌",
    "BLOCKED": "🚫",
    "PAUSED": "⏸️",
    "CANCELLED": "🛑",
}


def _duration_label(created_at) -> str:
    """Human-readable elapsed time since run started."""
    from datetime import UTC, datetime  # noqa: PLC0415

    if created_at is None:
        return "—"
    now = datetime.now(UTC)
    # Handle both tz-aware and tz-naive datetimes from DB
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    secs = int((now - created_at).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


async def _handle_status(parsed, respond) -> None:
    """Return status of active runs (or a specific run) with rich Block Kit cards."""
    try:
        from sqlalchemy import func, select  # noqa: PLC0415

        from phalanx.db.models import Run, Task, WorkOrder  # noqa: PLC0415
        from phalanx.db.session import get_db  # noqa: PLC0415

        async with get_db() as session:
            if parsed.run_id:
                # ── Single run detail ─────────────────────────────────────────
                stmt = select(Run).where(Run.id == parsed.run_id)
                result = await session.execute(stmt)
                run = result.scalar_one_or_none()
                if run is None:
                    await respond(f"⚠️ Run `{parsed.run_id}` not found.")
                    return

                # Task counts for this run
                counts_stmt = (
                    select(Task.status, func.count().label("n"))
                    .where(Task.run_id == run.id)
                    .group_by(Task.status)
                )
                counts_result = await session.execute(counts_stmt)
                task_counts = {row.status: row.n for row in counts_result.all()}
                total_tasks = sum(task_counts.values())
                done_tasks = task_counts.get("COMPLETED", 0)
                active_task = task_counts.get("IN_PROGRESS", 0)

                emoji = _STATUS_EMOJI.get(run.status, "❓")
                dur = _duration_label(run.created_at)
                cost_val = run.estimated_cost_usd
                cost = f"${float(cost_val):.2f}" if isinstance(cost_val, (int, float)) else "—"

                text = f"Run `{run.id}`: *{run.status}*"
                blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{emoji} Run {run.id[:8]}… — {run.status}",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Status*\n{emoji} `{run.status}`"},
                            {"type": "mrkdwn", "text": f"*Duration*\n⏱ {dur}"},
                            {
                                "type": "mrkdwn",
                                "text": f"*Tasks*\n{done_tasks}/{total_tasks} done"
                                + (f", {active_task} running" if active_task else ""),
                            },
                            {"type": "mrkdwn", "text": f"*Cost*\n💰 {cost}"},
                        ],
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Run ID*\n`{run.id}`"},
                            {"type": "mrkdwn", "text": f"*Branch*\n`{run.active_branch or '—'}`"},
                        ],
                    },
                ]
                if run.pr_url:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"🔗 *PR:* <{run.pr_url}|View Pull Request>",
                            },
                        }
                    )
                if run.error_message:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"❌ *Error:* {run.error_message[:300]}",
                            },
                        }
                    )
                await respond(text, blocks=blocks)

            else:
                # ── Active runs list ──────────────────────────────────────────
                from phalanx.workflow.state_machine import TERMINAL_STATES  # noqa: PLC0415

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

                # Build text fallback (also used by unit tests)
                lines = [f"*Active runs ({len(rows)}):*"]
                for run, title in rows:
                    emoji = _STATUS_EMOJI.get(run.status, "❓")
                    dur = _duration_label(run.created_at)
                    lines.append(f"• {emoji} `{run.id[:8]}…` {run.status} _{title}_ ⏱{dur}")
                text = "\n".join(lines)

                # Build Block Kit — one section per run
                blocks: list[dict] = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"🔥 FORGE — {len(rows)} active run{'s' if len(rows) != 1 else ''}",
                        },
                    },
                    {"type": "divider"},
                ]
                for run, title in rows:
                    emoji = _STATUS_EMOJI.get(run.status, "❓")
                    dur = _duration_label(run.created_at)
                    cost_val = run.estimated_cost_usd
                    cost = f"${float(cost_val):.2f}" if isinstance(cost_val, (int, float)) else "—"

                    # Per-run task counts (single query per run — at most 10)
                    counts_stmt = (
                        select(Task.status, func.count().label("n"))
                        .where(Task.run_id == run.id)
                        .group_by(Task.status)
                    )
                    counts_result = await session.execute(counts_stmt)
                    task_counts = {r.status: r.n for r in counts_result.all()}
                    total = sum(task_counts.values())
                    done = task_counts.get("COMPLETED", 0)
                    progress = f"{done}/{total} tasks" if total else "queued"

                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"{emoji} *{title}*"},
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Status*\n`{run.status}`"},
                                {"type": "mrkdwn", "text": f"*Progress*\n{progress}"},
                                {"type": "mrkdwn", "text": f"*Time*\n⏱ {dur}"},
                                {"type": "mrkdwn", "text": f"*Cost*\n💰 {cost}"},
                            ],
                        }
                    )
                    blocks.append(
                        {
                            "type": "context",
                            "elements": [
                                {"type": "mrkdwn", "text": f"Run ID: `{run.id}`"},
                            ],
                        }
                    )
                    blocks.append({"type": "divider"})

                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/phalanx status <run-id>` for details  •  `/phalanx cancel <run-id>` to stop",
                            },
                        ],
                    }
                )

                await respond(text, blocks=blocks)

    except Exception as exc:
        log.exception("slack_gateway.status_failed", error=str(exc))
        await respond(f"❌ Error fetching status: `{exc}`")


async def _handle_cancel(parsed, user_id: str, respond) -> None:
    """Cancel an active run."""
    try:
        from datetime import UTC, datetime  # noqa: PLC0415

        from sqlalchemy import select, update  # noqa: PLC0415

        from phalanx.db.models import Run  # noqa: PLC0415
        from phalanx.db.session import get_db  # noqa: PLC0415
        from phalanx.workflow.state_machine import RunStatus, validate_transition  # noqa: PLC0415

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


async def _handle_approval_action(body: dict, client, decision: str) -> None:
    """
    Common handler for phalanx_approve / phalanx_reject button actions.

    Updates the Approval row in Postgres and replaces the interactive
    Slack message with a decision receipt so the buttons can't be clicked twice.
    """
    try:
        from datetime import UTC, datetime  # noqa: PLC0415

        from sqlalchemy import select, update  # noqa: PLC0415

        from phalanx.db.models import Approval  # noqa: PLC0415
        from phalanx.db.session import get_db  # noqa: PLC0415

        action = body["actions"][0]
        approval_id: str = action["value"]
        user_id: str = body["user"]["id"]
        channel_id: str = body["container"]["channel_id"]
        message_ts: str = body["container"]["message_ts"]

        async with get_db() as session:
            result = await session.execute(select(Approval).where(Approval.id == approval_id))
            approval = result.scalar_one_or_none()

            if approval is None:
                log.warning("approval_action.not_found", approval_id=approval_id)
                return

            if approval.status != "PENDING":
                # Already decided — update the message to show current state
                emoji = "✅" if approval.status == "APPROVED" else "❌"
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"{emoji} Already {approval.status} by <@{approval.decided_by}>",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"{emoji} *{approval.gate_type.upper()} gate already "
                                    f"{approval.status}* by <@{approval.decided_by}>"
                                ),
                            },
                        }
                    ],
                )
                return

            await session.execute(
                update(Approval)
                .where(Approval.id == approval_id)
                .values(
                    status=decision,
                    decided_by=user_id,
                    decided_at=datetime.now(UTC),
                )
            )
            await session.commit()

        emoji = "✅" if decision == "APPROVED" else "❌"
        label = "APPROVED" if decision == "APPROVED" else "REJECTED"
        log.info(
            "approval_action.decided",
            approval_id=approval_id,
            decision=decision,
            decided_by=user_id,
        )

        # Replace the interactive message with a decision receipt
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"{emoji} {label} by <@{user_id}>",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *{approval.gate_type.upper()} gate {label}* "
                            f"by <@{user_id}>\n"
                            f"Run: `{approval.run_id}`"
                        ),
                    },
                }
            ],
        )

    except Exception as exc:
        log.exception("approval_action.failed", error=str(exc))


# ── Main entrypoint ───────────────────────────────────────────────────────────


async def main() -> None:  # pragma: no cover
    if not settings.slack_bot_token:
        log.error("slack_gateway.missing_config", missing="SLACK_BOT_TOKEN")
        sys.exit(1)
    if not settings.slack_app_token:
        log.error("slack_gateway.missing_config", missing="SLACK_APP_TOKEN")
        sys.exit(1)

    app = _build_app(settings.slack_bot_token)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)

    # Start the HTTP health server (does NOT crash the gateway on failure).
    health_server = GatewayHealthServer()
    await health_server.start()

    log.info("slack_gateway.starting", socket_mode=True)

    loop = asyncio.get_event_loop()

    def _shutdown(sig, _frame):
        log.info("slack_gateway.shutdown", signal=sig)
        loop.create_task(health_server.stop())
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
