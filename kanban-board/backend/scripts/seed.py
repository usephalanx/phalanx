#!/usr/bin/env python3
"""Seed the database with demo data.

Creates:
  - 1 demo user  (demo@phalanx.dev / demo1234)
  - 1 workspace  ("Demo Workspace")
  - 2 boards     ("Product Roadmap", "Sprint Board")
  - Default columns per board (Backlog, To Do, In Progress, Done)
  - Sample cards distributed across columns

Usage:
    # From the repo root (kanban-board/):
    python -m backend.scripts.seed

    # Or inside the backend container:
    python -m backend.scripts.seed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure the backend directory is on sys.path so `app.*` imports resolve
# regardless of how the script is invoked (direct, -m, or imported by tests).
_backend_dir = str(Path(__file__).resolve().parent.parent)
_resolved_paths = {str(Path(p).resolve()) for p in sys.path}
if _backend_dir not in _resolved_paths:
    sys.path.insert(0, _backend_dir)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth import hash_password
from app.database import async_session_factory, engine
from app.models import Base, Board, Card, Column, User, Workspace, WorkspaceMember

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEMO_EMAIL = "demo@phalanx.dev"
DEMO_PASSWORD = "demo1234"
WORKSPACE_NAME = "Demo Workspace"

DEFAULT_COLUMNS = ["Backlog", "To Do", "In Progress", "Done"]

BOARDS: list[dict[str, object]] = [
    {
        "title": "Product Roadmap",
        "cards": {
            "Backlog": [
                ("Dark mode support", "Add a dark / light theme toggle"),
                ("Export to CSV", "Allow users to export board data as CSV"),
                ("Activity log", "Show a history of card moves and edits"),
            ],
            "To Do": [
                ("User avatars", "Upload and display user profile pictures"),
                ("Board templates", "Pre-built column layouts for common workflows"),
            ],
            "In Progress": [
                ("Drag-and-drop cards", "Implement card reordering via dnd-kit"),
            ],
            "Done": [
                ("User registration", "Email + password auth with JWT tokens"),
                ("Workspace CRUD", "Create, read, update, delete workspaces"),
            ],
        },
    },
    {
        "title": "Sprint Board",
        "cards": {
            "Backlog": [
                ("Write API tests", "Cover boards, columns, and cards endpoints"),
                ("CI pipeline", "Set up GitHub Actions for lint + test"),
            ],
            "To Do": [
                ("Seed script", "Create demo data for local development"),
            ],
            "In Progress": [
                ("Docker Compose", "Postgres + backend + frontend containers"),
            ],
            "Done": [
                ("Project scaffolding", "FastAPI backend + Vite React frontend"),
            ],
        },
    },
]


async def seed(session: AsyncSession) -> None:
    """Insert demo data into the database.

    This function is idempotent — if the demo user already exists the
    script prints a message and exits cleanly.
    """
    # Check if demo user already exists
    result = await session.execute(select(User).where(User.email == DEMO_EMAIL))
    existing = result.scalar_one_or_none()
    if existing is not None:
        print(f"✓ Demo user '{DEMO_EMAIL}' already exists — skipping seed.")
        return

    # 1. Create demo user
    user = User(
        email=DEMO_EMAIL,
        hashed_password=hash_password(DEMO_PASSWORD),
    )
    session.add(user)
    await session.flush()
    print(f"  Created user: {DEMO_EMAIL}")

    # 2. Create workspace
    workspace = Workspace(
        name=WORKSPACE_NAME,
        slug="demo-workspace",
        owner_id=user.id,
    )
    session.add(workspace)
    await session.flush()
    print(f"  Created workspace: {WORKSPACE_NAME}")

    # 3. Add user as owner member
    membership = WorkspaceMember(
        user_id=user.id,
        workspace_id=workspace.id,
        role="owner",
    )
    session.add(membership)
    await session.flush()

    # 4. Create boards, columns, and cards
    for board_def in BOARDS:
        board = Board(
            name=str(board_def["title"]),
            workspace_id=workspace.id,
        )
        session.add(board)
        await session.flush()
        print(f"  Created board: {board.name}")

        cards_by_column: dict[str, list[tuple[str, str]]] = board_def.get("cards", {})  # type: ignore[assignment]

        for col_index, col_title in enumerate(DEFAULT_COLUMNS):
            position = (col_index + 1) * 1024.0
            column = Column(
                name=col_title,
                board_id=board.id,
                position=position,
            )
            session.add(column)
            await session.flush()

            # Insert cards for this column
            card_defs = cards_by_column.get(col_title, [])
            for card_index, (card_title, card_desc) in enumerate(card_defs):
                card_position = (card_index + 1) * 1024.0
                card = Card(
                    title=card_title,
                    description=card_desc,
                    column_id=column.id,
                    position=card_position,
                    assignee_id=user.id,
                )
                session.add(card)

            await session.flush()
            card_count = len(card_defs)
            if card_count:
                print(f"    {col_title}: {card_count} card(s)")

    await session.commit()
    print("\n✓ Seed complete.")


async def main() -> None:
    """Entry point — create tables if needed and run the seed."""
    # Ensure tables exist (useful when running outside Alembic)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        await seed(session)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
