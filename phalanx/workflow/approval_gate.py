"""
Approval Gate — creates and polls human approval requests.

Design (evidence in EXECUTION_PLAN.md §B):
  - Approval rows are Postgres-first: written immediately, polled via DB query.
  - Gate types: plan | ship | release | production_deploy | public_post |
                destructive | guardrail_override
  - NEVER bypassable in code — only a DB row with status='APPROVED' unblocks.
  - Gate posts a Slack message to the original channel with approve/reject buttons.
    (In M3 MVP: posts text only. Interactive buttons are M4+.)

AP-002: All state transitions go through the state machine — never direct writes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select

from phalanx.config.settings import get_settings
from phalanx.db.models import Approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# How often to poll the DB for an approval decision (seconds)
_POLL_INTERVAL_SECONDS = 30

# Maximum wait before escalating (24h default, overridden by workflow.yaml)
_DEFAULT_TIMEOUT_SECONDS = 86400


class ApprovalTimeoutError(RuntimeError):
    """Raised when a gate times out without a human decision."""


class ApprovalRejectedError(RuntimeError):
    """Raised when a human explicitly rejects the gate."""

    def __init__(self, gate_type: str, note: str | None = None) -> None:
        super().__init__(
            f"Approval gate '{gate_type}' was REJECTED. Note: {note or 'no note provided'}"
        )
        self.gate_type = gate_type
        self.note = note


class ApprovalGate:
    """
    Creates an Approval row and waits for a human decision.

    Usage:
        gate = ApprovalGate(session, run_id="uuid", project_id="uuid")
        await gate.request_and_wait(
            gate_type="plan",
            gate_phase="planning",
            context_snapshot={"plan_summary": "..."},
        )
        # Raises ApprovalRejectedError if rejected
        # Raises ApprovalTimeoutError if timed out
        # Returns normally if approved
    """

    def __init__(
        self,
        session: AsyncSession,
        run_id: str,
        slack_notify: bool = True,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self.run_id = run_id
        self.slack_notify = slack_notify
        self.timeout_seconds = timeout_seconds
        self._log = log.bind(run_id=run_id)

    async def request_and_wait(
        self,
        gate_type: str,
        gate_phase: str,
        context_snapshot: dict | None = None,
        required_approver_level: str = "ic6",
    ) -> Approval:
        """
        Create an Approval row and poll until approved/rejected/timed out.

        Returns the Approval row on success (status=APPROVED).
        Raises ApprovalRejectedError or ApprovalTimeoutError otherwise.
        """
        # Create the approval row
        approval = Approval(
            run_id=self.run_id,
            gate_type=gate_type,
            gate_phase=gate_phase,
            status="PENDING",
            context_snapshot=context_snapshot or {},
            required_approver_level=required_approver_level,
        )
        self._session.add(approval)
        await self._session.commit()
        await self._session.refresh(approval)

        self._log.info(
            "approval_gate.requested",
            approval_id=approval.id,
            gate_type=gate_type,
            gate_phase=gate_phase,
        )

        if self.slack_notify:
            await self._notify_slack(approval, context_snapshot)

        # Poll for decision
        return await self._poll(approval.id)

    async def _poll(self, approval_id: str) -> Approval:
        """Poll Postgres until the Approval is decided or timeout is reached."""
        elapsed = 0
        while elapsed < self.timeout_seconds:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS

            result = await self._session.execute(select(Approval).where(Approval.id == approval_id))
            # expire + refresh to avoid stale cache
            self._session.expire_all()
            result = await self._session.execute(select(Approval).where(Approval.id == approval_id))
            approval = result.scalar_one()

            if approval.status == "APPROVED":
                self._log.info(
                    "approval_gate.approved",
                    approval_id=approval_id,
                    decided_by=approval.decided_by,
                    elapsed_s=elapsed,
                )
                return approval

            if approval.status == "REJECTED":
                self._log.warning(
                    "approval_gate.rejected",
                    approval_id=approval_id,
                    decided_by=approval.decided_by,
                    note=approval.decision_note,
                )
                raise ApprovalRejectedError(
                    gate_type=approval.gate_type,
                    note=approval.decision_note,
                )

            self._log.debug(
                "approval_gate.polling",
                approval_id=approval_id,
                elapsed_s=elapsed,
                timeout_s=self.timeout_seconds,
            )

        raise ApprovalTimeoutError(
            f"Approval gate '{approval_id}' timed out after {elapsed}s without a human decision."
        )

    async def _notify_slack(self, approval: Approval, context: dict | None) -> None:
        """
        Post an interactive approval request to the run's originating Slack channel.
        Posts Block Kit message with Approve / Reject buttons.
        """
        try:
            from phalanx.db.models import Channel, Run, WorkOrder  # noqa: PLC0415

            settings = get_settings()
            if not settings.slack_bot_token:
                return

            # Fetch channel_id + thread_ts from run → work_order → channel.
            # slack_thread_ts is set by the gateway when it posts the ack message —
            # NULL for old runs or non-Slack paths; safe to skip threading in that case.
            result = await self._session.execute(
                select(Channel.channel_id, WorkOrder.slack_thread_ts)
                .join(WorkOrder, WorkOrder.channel_id == Channel.id)
                .join(Run, Run.work_order_id == WorkOrder.id)
                .where(Run.id == self.run_id)
            )
            row = result.one_or_none()
            if not row:
                return
            slack_channel_id, thread_ts = row
            if not slack_channel_id:
                return

            summary = context.get("plan_summary", "") if context else ""
            summary_block = (
                [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:* {summary}"}}]
                if summary
                else []
            )

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🔔 Approval Required: {approval.gate_type.upper()} gate",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Run:*\n`{self.run_id}`"},
                        {"type": "mrkdwn", "text": f"*Phase:*\n`{approval.gate_phase}`"},
                        {"type": "mrkdwn", "text": f"*Gate:*\n`{approval.gate_type}`"},
                        {"type": "mrkdwn", "text": f"*Approval ID:*\n`{approval.id}`"},
                    ],
                },
                *summary_block,
                {"type": "divider"},
                {
                    "type": "actions",
                    "block_id": f"approval_{approval.id}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Approve"},
                            "style": "primary",
                            "action_id": "phalanx_approve",
                            "value": approval.id,
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Approve this gate?"},
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"Approving the *{approval.gate_type}* gate for run `{self.run_id[:8]}…`. This will unblock the pipeline.",
                                },
                                "confirm": {"type": "plain_text", "text": "Approve"},
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            },
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ Reject"},
                            "style": "danger",
                            "action_id": "phalanx_reject",
                            "value": approval.id,
                        },
                    ],
                },
            ]

            client = AsyncWebClient(token=settings.slack_bot_token)
            post_kwargs: dict = {
                "channel": slack_channel_id,
                "text": f"🔔 Approval required: `{approval.gate_type}` gate for run `{self.run_id[:8]}…`",
                "blocks": blocks,
            }
            if thread_ts:
                post_kwargs["thread_ts"] = thread_ts
            await client.chat_postMessage(**post_kwargs)
            self._log.info(
                "approval_gate.slack_notified",
                approval_id=approval.id,
                channel=slack_channel_id,
            )
        except Exception as exc:
            self._log.warning("approval_gate.slack_notify_failed", error=str(exc))
