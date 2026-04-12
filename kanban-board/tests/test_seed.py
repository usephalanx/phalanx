"""Tests for the seed script."""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure backend app is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.models.board import Board  # noqa: E402
from app.models.card import Card  # noqa: E402
from app.models.column import Column  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
from app.models.workspace_member import WorkspaceMember  # noqa: E402


def _load_seed_module() -> ModuleType:
    """Load the seed module, handling sys.path so 'app' resolves correctly."""
    backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
    abs_backend = os.path.abspath(backend_dir)
    if abs_backend not in sys.path:
        sys.path.insert(0, abs_backend)

    # If already cached with a broken import, remove it
    for key in list(sys.modules.keys()):
        if key.startswith("backend.scripts.seed") or key == "scripts.seed":
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        "seed",
        os.path.join(abs_backend, "scripts", "seed.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_seed_mod = _load_seed_module()
seed = _seed_mod.seed
DEMO_EMAIL: str = _seed_mod.DEMO_EMAIL
DEMO_PASSWORD: str = _seed_mod.DEMO_PASSWORD

pytestmark = pytest.mark.asyncio


async def test_seed_creates_demo_user(db_session: AsyncSession) -> None:
    """Seed should create the demo user with the expected email."""
    await seed(db_session)

    result = await db_session.execute(select(User).where(User.email == DEMO_EMAIL))
    user = result.scalar_one_or_none()
    assert user is not None
    assert user.email == DEMO_EMAIL


async def test_seed_creates_workspace(db_session: AsyncSession) -> None:
    """Seed should create one workspace named 'Demo Workspace'."""
    await seed(db_session)

    result = await db_session.execute(select(Workspace))
    workspaces = result.scalars().all()
    assert len(workspaces) == 1
    assert workspaces[0].name == "Demo Workspace"


async def test_seed_creates_workspace_membership(db_session: AsyncSession) -> None:
    """Seed should create an admin membership linking user to workspace."""
    await seed(db_session)

    result = await db_session.execute(select(WorkspaceMember))
    members = result.scalars().all()
    assert len(members) == 1
    assert members[0].role == "admin"


async def test_seed_creates_two_boards(db_session: AsyncSession) -> None:
    """Seed should create exactly 2 boards."""
    await seed(db_session)

    result = await db_session.execute(select(Board))
    boards = result.scalars().all()
    assert len(boards) == 2
    titles = {b.title for b in boards}
    assert "Product Roadmap" in titles
    assert "Sprint Board" in titles


async def test_seed_creates_default_columns(db_session: AsyncSession) -> None:
    """Each board should have 4 default columns."""
    await seed(db_session)

    result = await db_session.execute(select(Column))
    columns = result.scalars().all()
    # 2 boards x 4 columns = 8
    assert len(columns) == 8

    expected_titles = {"Backlog", "To Do", "In Progress", "Done"}
    actual_titles = {c.title for c in columns}
    assert actual_titles == expected_titles


async def test_seed_creates_sample_cards(db_session: AsyncSession) -> None:
    """Seed should create multiple sample cards across boards."""
    await seed(db_session)

    result = await db_session.execute(select(Card))
    cards = result.scalars().all()
    # Product Roadmap: 3 + 2 + 1 + 2 = 8
    # Sprint Board: 2 + 1 + 1 + 1 = 5
    assert len(cards) == 13


async def test_seed_column_positions_are_ordered(db_session: AsyncSession) -> None:
    """Column positions should be ascending within each board."""
    await seed(db_session)

    result = await db_session.execute(select(Board))
    boards = result.scalars().all()

    for board in boards:
        col_result = await db_session.execute(
            select(Column).where(Column.board_id == board.id).order_by(Column.position)
        )
        columns = col_result.scalars().all()
        positions = [c.position for c in columns]
        assert positions == sorted(positions)
        assert len(set(positions)) == len(positions), "positions must be unique"


async def test_seed_card_positions_are_ordered(db_session: AsyncSession) -> None:
    """Card positions should be ascending within each column."""
    await seed(db_session)

    result = await db_session.execute(select(Column))
    columns = result.scalars().all()

    for col in columns:
        card_result = await db_session.execute(
            select(Card).where(Card.column_id == col.id).order_by(Card.position)
        )
        cards = card_result.scalars().all()
        positions = [c.position for c in cards]
        assert positions == sorted(positions)


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    """Running seed twice should not duplicate data."""
    await seed(db_session)
    await seed(db_session)

    result = await db_session.execute(select(User).where(User.email == DEMO_EMAIL))
    users = result.scalars().all()
    assert len(users) == 1

    result = await db_session.execute(select(Board))
    boards = result.scalars().all()
    assert len(boards) == 2


async def test_seed_cards_assigned_to_demo_user(db_session: AsyncSession) -> None:
    """All seeded cards should be assigned to the demo user."""
    await seed(db_session)

    user_result = await db_session.execute(
        select(User).where(User.email == DEMO_EMAIL)
    )
    user = user_result.scalar_one()

    card_result = await db_session.execute(select(Card))
    cards = card_result.scalars().all()

    for card in cards:
        assert card.assignee_id == user.id
