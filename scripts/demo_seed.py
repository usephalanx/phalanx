"""
FORGE — VC Demo Seed Script

Sets up a clean, realistic demo environment in minutes. Idempotent — safe to
re-run. Use --reset to wipe demo data and start fresh.

What it creates:
  1. Project  — "Teamworks Web App" (slug: teamworks-web)
  2. Channel  — links your Slack channel to the project
  3. (Optional) A demo WorkOrder ready to trigger with /forge build

Usage:
  python scripts/demo_seed.py --slack-channel-id C01234ABCDE
  python scripts/demo_seed.py --slack-channel-id C01234ABCDE --reset
  python scripts/demo_seed.py --slack-channel-id C01234ABCDE --with-work-order

  Find your Slack channel ID: right-click channel → View channel details → bottom of About tab.

After running:
  In Slack: /forge build Add user authentication with JWT login
"""
from __future__ import annotations

import asyncio
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── ANSI colours for terminal output ─────────────────────────────────────────

GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg: str)   -> None: print(f"{GREEN}  ✓ {msg}{RESET}")
def info(msg: str) -> None: print(f"{CYAN}  → {msg}{RESET}")
def warn(msg: str) -> None: print(f"{YELLOW}  ⚠ {msg}{RESET}")
def hdr(msg: str)  -> None: print(f"\n{BOLD}{msg}{RESET}")


# ── Demo constants ────────────────────────────────────────────────────────────

DEMO_PROJECT_SLUG = "teamworks-web"
DEMO_PROJECT_NAME = "Teamworks Web App"
DEMO_REPO_URL     = "https://github.com/your-org/teamworks"  # update before demo

DEMO_WORK_ORDERS = [
    {
        "title": "Add user authentication with JWT login",
        "description": (
            "Implement a complete JWT-based authentication system. "
            "Include: login endpoint, token refresh, logout, protected route middleware, "
            "and a basic user profile page. Follow existing patterns in the codebase."
        ),
        "priority": 75,  # P1
    },
    {
        "title": "Fix mobile layout on the dashboard page",
        "description": (
            "The dashboard grid breaks on viewports below 768px. "
            "Cards overlap and the nav collapses incorrectly. "
            "Fix using Tailwind responsive classes — no new dependencies."
        ),
        "priority": 50,  # P2
    },
]


# ── Core seed logic ───────────────────────────────────────────────────────────

async def reset_demo(session, project_id: str | None) -> None:
    """Wipe all demo data tied to the demo project."""
    from sqlalchemy import delete, select
    from forge.db.models import (
        WorkOrder, Run, Task, Approval, Artifact, Channel,
        AuditLog, Project,
    )

    if project_id is None:
        warn("No demo project found — nothing to reset.")
        return

    # Order matters: FK children first
    for model, label in [
        (AuditLog,  "audit logs"),
        (Artifact,  "artifacts"),
        (Approval,  "approvals"),
        (Task,      "tasks"),
        (Run,       "runs"),
        (WorkOrder, "work orders"),
        (Channel,   "channels"),
    ]:
        result = await session.execute(
            delete(model).where(
                getattr(model, "project_id", None) is not None
                and model.project_id == project_id  # type: ignore[union-attr]
            )
            if hasattr(model, "project_id")
            else delete(model).where(model.id == model.id)  # no-op for Channel
        )
        # Channel has no project_id — delete by project FK lookup
        if model is Channel:
            await session.execute(delete(Channel).where(Channel.project_id == project_id))

    await session.execute(delete(Project).where(Project.id == project_id))
    ok("Demo data wiped.")


async def seed(
    slack_channel_id: str,
    reset: bool,
    with_work_order: bool,
    requested_by: str,
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import select

    from forge.config.settings import get_settings
    from forge.db.models import Project, Channel, WorkOrder

    settings = get_settings()
    info(f"env={settings.forge_env}  db={settings.database_url!r}")

    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionFactory() as session:
        async with session.begin():

            # ── Optional reset ────────────────────────────────────────────────
            if reset:
                hdr("Resetting demo data…")
                existing = await session.execute(
                    select(Project.id).where(Project.slug == DEMO_PROJECT_SLUG)
                )
                project_id = existing.scalar_one_or_none()
                await reset_demo(session, project_id)

            # ── 1. Upsert Project ─────────────────────────────────────────────
            hdr("1. Project")
            stmt = pg_insert(Project).values(
                slug=DEMO_PROJECT_SLUG,
                name=DEMO_PROJECT_NAME,
                repo_url=DEMO_REPO_URL,
                domain="web",
                config={
                    "stack": {"language": "typescript", "framework": "nextjs"},
                    "branches": {"main": "main", "feature_prefix": "feat/"},
                },
                onboarding_status="complete",
            ).on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "name": DEMO_PROJECT_NAME,
                    "repo_url": DEMO_REPO_URL,
                    "onboarding_status": "complete",
                },
            ).returning(Project.id)

            result = await session.execute(stmt)
            project_id: str = result.scalar_one()
            ok(f"Project  id={project_id}  slug={DEMO_PROJECT_SLUG!r}")

            # ── 2. Upsert Slack Channel ───────────────────────────────────────
            hdr("2. Slack Channel")
            existing_ch = await session.execute(
                select(Channel).where(
                    Channel.platform == "slack",
                    Channel.channel_id == slack_channel_id,
                )
            )
            channel = existing_ch.scalar_one_or_none()

            if channel is None:
                channel = Channel(
                    project_id=project_id,
                    platform="slack",
                    channel_id=slack_channel_id,
                    display_name=f"#forge-demo ({slack_channel_id})",
                )
                session.add(channel)
                await session.flush()
                ok(f"Channel  id={channel.id}  slack_channel={slack_channel_id}")
            else:
                # Ensure it's linked to the right project
                channel.project_id = project_id
                ok(f"Channel  already exists, linked to project  id={channel.id}")

            # ── 3. Optional: seed a demo WorkOrder ────────────────────────────
            if with_work_order:
                hdr("3. Demo Work Orders")
                for wo_data in DEMO_WORK_ORDERS:
                    stmt = pg_insert(WorkOrder).values(
                        project_id=project_id,
                        channel_id=channel.id,
                        title=wo_data["title"],
                        description=wo_data["description"],
                        raw_command=f"/forge build {wo_data['title']}",
                        status="OPEN",
                        priority=wo_data["priority"],
                        requested_by=requested_by,
                    ).on_conflict_do_nothing()
                    await session.execute(stmt)
                    ok(f"WorkOrder  {wo_data['title']!r}")

    await engine.dispose()

    # ── Summary + demo instructions ───────────────────────────────────────────
    hdr("✅ Demo seed complete")
    print()
    print(f"{BOLD}  Project:      {DEMO_PROJECT_NAME} (slug: {DEMO_PROJECT_SLUG}){RESET}")
    print(f"{BOLD}  Slack channel:{RESET} {slack_channel_id}  →  linked to project")
    print()
    print(f"{BOLD}Demo flow for VCs:{RESET}")
    print()
    print(f"  {CYAN}Step 1 — Trigger a build{RESET}")
    print(f"    /forge build Add user authentication with JWT login")
    print()
    print(f"  {CYAN}Step 2 — Watch Commander decompose the work{RESET}")
    print(f"    FORGE posts task plan in Slack. You'll see tasks for:")
    print(f"    Planner → Builder → Reviewer → Security → Release")
    print()
    print(f"  {CYAN}Step 3 — Approve the plan{RESET}")
    print(f"    Click ✅ Approve Plan in the Slack message")
    print()
    print(f"  {CYAN}Step 4 — Watch agents execute{RESET}")
    print(f"    /forge status  ← run this anytime to see live progress")
    print()
    print(f"  {CYAN}Step 5 — Ship approval gate{RESET}")
    print(f"    Click ✅ Approve Ship to release")
    print()
    print(f"  {CYAN}Step 6 — Cancel if needed{RESET}")
    print(f"    /forge cancel <run-id>")
    print()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed FORGE demo environment for VC presentation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--slack-channel-id",
        required=True,
        metavar="C01234ABCDE",
        help="Slack channel ID to link to the demo project (right-click channel → View details)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe existing demo data before seeding (fresh start)",
    )
    parser.add_argument(
        "--with-work-order",
        action="store_true",
        help="Pre-create demo work orders in the DB (can also trigger live with /forge build)",
    )
    parser.add_argument(
        "--requested-by",
        default="U_DEMO",
        metavar="SLACK_USER_ID",
        help="Slack user ID to attribute demo work orders to",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}╔══════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║  FORGE — VC Demo Seed                    ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════╝{RESET}")

    asyncio.run(seed(
        slack_channel_id=args.slack_channel_id,
        reset=args.reset,
        with_work_order=args.with_work_order,
        requested_by=args.requested_by,
    ))


if __name__ == "__main__":
    main()
